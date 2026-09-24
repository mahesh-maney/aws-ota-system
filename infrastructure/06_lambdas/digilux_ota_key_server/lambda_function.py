"""
digilux_ota_key_server
=======================
Digilux-hosted key management service for the OTA system.

Implements envelope encryption: per-artifact AES keys are wrapped
(encrypted) by a master key that never leaves this service.
Honeywell's OTA Lambdas call this service to wrap keys at upload
time and unwrap them at consent time.

Endpoints:
  POST /wrap     — wrap a 32-byte plaintext AES key
  POST /unwrap   — unwrap a previously wrapped AES key
  GET  /health   — health check (no auth required)

Authentication:
  Authorization: Bearer <api-key>
  Each caller (e.g. honeywell-artifact-processor, honeywell-user-consent)
  has a unique API key provisioned by Digilux and stored in Secrets Manager.

Wrap format:
  "v1:<base64url(nonce[12] | ciphertext[32] | tag[16])>"
  The version prefix ("v1") enables future master key rotation without
  breaking in-flight wrapped keys.

Master key:
  32-byte AES-256 key, base64-encoded, stored in AWS Secrets Manager.
  Secret name set via MASTER_KEY_SECRET_NAME env var.

API key registry:
  JSON object stored in Secrets Manager:
  { "<apiKey>": { "callerId": "...", "description": "..." }, ... }
  Secret name set via API_KEYS_SECRET_NAME env var.

Security properties:
  - Master key never leaves this Lambda's memory
  - Wrapped keys stored on Honeywell's side are useless without this service
  - Every wrap/unwrap is audit-logged with callerId + context
  - GCM tag ensures wrapped key integrity — tampering raises authentication error
"""

import base64
import datetime
import json
import logging
import os
import uuid
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION               = os.environ.get("REGION",                "ap-south-1")
MASTER_KEY_SECRET    = os.environ.get("MASTER_KEY_SECRET_NAME", "digilux-ota-key-server-master-key")
API_KEYS_SECRET      = os.environ.get("API_KEYS_SECRET_NAME",   "digilux-ota-key-server-api-keys")
SERVICE_VERSION      = os.environ.get("SERVICE_VERSION",        "1.0.0")

KEY_VERSION         = "v1"
AES_KEY_SIZE        = 32   # bytes — AES-256
NONCE_SIZE          = 12   # bytes — GCM nonce
TAG_SIZE            = 16   # bytes — GCM tag
WRAPPED_BINARY_SIZE = NONCE_SIZE + AES_KEY_SIZE + TAG_SIZE  # 60 bytes

secretsmanager = boto3.client("secretsmanager", region_name=REGION)

# Module-level caches — survive across warm Lambda invocations
_master_key_cache: Optional[bytes] = None
_api_keys_cache:   Optional[dict]  = None


# ── Logging helpers ────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _log(level: str, msg: str, **fields) -> None:
    record = {"msg": msg, "ts": _now(), **fields}
    getattr(log, level)(json.dumps(record, default=str))


def _audit(event: str, caller_id: str, action: str, result: str, **extra) -> None:
    """
    Emit a structured audit record to stdout (captured by CloudWatch Logs).
    Every wrap and unwrap operation is audited regardless of success or failure.
    """
    print(json.dumps({
        "audit":    True,
        "event":    event,
        "ts":       _now(),
        "callerId": caller_id,
        "action":   action,
        "result":   result,
        **extra,
    }, default=str))


# ── Response helpers ───────────────────────────────────────────────────────────

def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }


def _err(code: int, message: str, **extra) -> dict:
    return _resp(code, {"error": message, **extra})


# ── Secrets Manager ────────────────────────────────────────────────────────────

def _get_master_key() -> bytes:
    """
    Load master AES-256 key from Secrets Manager.
    Cached in Lambda memory after first successful load.
    Raises on any error — callers must handle.
    """
    global _master_key_cache
    if _master_key_cache is not None:
        _log("debug", "master_key_cache_hit")
        return _master_key_cache

    _log("debug", "master_key_fetch_start", secret=MASTER_KEY_SECRET)
    try:
        resp = secretsmanager.get_secret_value(SecretId=MASTER_KEY_SECRET)
        raw  = resp.get("SecretString") or base64.b64decode(resp["SecretBinary"]).decode()
        key_bytes = base64.b64decode(raw.strip())
        if len(key_bytes) != AES_KEY_SIZE:
            raise ValueError(
                f"Master key must be {AES_KEY_SIZE} bytes, got {len(key_bytes)}"
            )
        _master_key_cache = key_bytes
        _log("info", "master_key_loaded", keyLenBytes=len(key_bytes))
        return _master_key_cache
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        _log("error", "master_key_fetch_failed", awsError=code, awsMessage=msg)
        raise
    except ValueError as exc:
        _log("error", "master_key_invalid", detail=str(exc))
        raise


def _get_api_keys() -> dict:
    """
    Load API key registry from Secrets Manager.
    Cached in Lambda memory after first successful load.
    Returns: { "<rawApiKey>": { "callerId": "...", "description": "..." }, ... }
    """
    global _api_keys_cache
    if _api_keys_cache is not None:
        _log("debug", "api_keys_cache_hit")
        return _api_keys_cache

    _log("debug", "api_keys_fetch_start", secret=API_KEYS_SECRET)
    try:
        resp  = secretsmanager.get_secret_value(SecretId=API_KEYS_SECRET)
        raw   = resp.get("SecretString") or base64.b64decode(resp["SecretBinary"]).decode()
        keys  = json.loads(raw)
        if not isinstance(keys, dict):
            raise ValueError("API keys secret must be a JSON object")
        _api_keys_cache = keys
        _log("info", "api_keys_loaded", keyCount=len(keys))
        return _api_keys_cache
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        _log("error", "api_keys_fetch_failed", awsError=code, awsMessage=msg)
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        _log("error", "api_keys_invalid", detail=str(exc))
        raise


# ── Authentication ─────────────────────────────────────────────────────────────

def _authenticate(headers: dict) -> Optional[dict]:
    """
    Validate Bearer token from Authorization header against the API key registry.
    Returns caller info dict on success, None on any auth failure.
    Never raises — auth failures are non-exceptional.
    """
    auth = (headers or {}).get("Authorization") or (headers or {}).get("authorization", "")
    if not auth.startswith("Bearer "):
        _log("warning", "auth_missing_bearer_scheme", headerPresent=bool(auth))
        return None

    api_key = auth[7:].strip()
    if not api_key:
        _log("warning", "auth_empty_bearer_token")
        return None

    try:
        registry = _get_api_keys()
    except Exception as exc:
        _log("error", "auth_registry_unavailable", error=str(exc))
        return None

    caller = registry.get(api_key)
    if not caller:
        # Log only a prefix — never log full API key
        _log("warning", "auth_invalid_api_key",
             keyPrefix=api_key[:4] + "****" if len(api_key) >= 4 else "****")
        return None

    _log("debug", "auth_success", callerId=caller.get("callerId"))
    return caller


# ── Crypto ─────────────────────────────────────────────────────────────────────

def _wrap_key(plaintext_key: bytes, master_key: bytes) -> str:
    """
    Wrap plaintext_key using AES-256-GCM with master_key.

    Format: "v1:<base64url(nonce[12] || ciphertext[32] || tag[16])>"
    Total wrapped binary: 60 bytes. Each call produces a unique
    wrappedKey due to a fresh random nonce.

    Raises: ValueError if key sizes are wrong.
    """
    if len(master_key) != AES_KEY_SIZE:
        raise ValueError(f"master_key must be {AES_KEY_SIZE} bytes")
    if len(plaintext_key) != AES_KEY_SIZE:
        raise ValueError(f"plaintext_key must be {AES_KEY_SIZE} bytes")

    nonce            = os.urandom(NONCE_SIZE)
    aesgcm           = AESGCM(master_key)
    ciphertext_tag   = aesgcm.encrypt(nonce, plaintext_key, None)
    # AESGCM.encrypt returns ciphertext + 16-byte GCM tag concatenated
    raw              = nonce + ciphertext_tag
    encoded          = base64.urlsafe_b64encode(raw).decode()
    return f"{KEY_VERSION}:{encoded}"


def _unwrap_key(wrapped_key: str, master_key: bytes) -> bytes:
    """
    Unwrap a wrapped key produced by _wrap_key.

    Raises:
      ValueError                             — malformed input (bad format, bad base64, wrong version)
      cryptography.exceptions.InvalidTag     — GCM authentication failed (tampered or wrong master key)
    """
    if not wrapped_key or ":" not in wrapped_key:
        raise ValueError("Wrapped key missing version prefix")

    version, encoded = wrapped_key.split(":", 1)

    if version != KEY_VERSION:
        raise ValueError(f"Unsupported key version: {version!r} (expected {KEY_VERSION!r})")

    try:
        # Add padding in case it was stripped
        raw = base64.urlsafe_b64decode(encoded + "==")
    except Exception:
        raise ValueError("Wrapped key contains invalid base64url data")

    if len(raw) != WRAPPED_BINARY_SIZE:
        raise ValueError(
            f"Wrapped key binary length {len(raw)} != expected {WRAPPED_BINARY_SIZE}"
        )

    nonce          = raw[:NONCE_SIZE]
    ciphertext_tag = raw[NONCE_SIZE:]

    aesgcm    = AESGCM(master_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext_tag, None)
    # Raises cryptography.exceptions.InvalidTag if GCM tag fails
    return plaintext


# ── Route handlers ─────────────────────────────────────────────────────────────

def _handle_health() -> dict:
    _log("debug", "health_check_ok", version=SERVICE_VERSION)
    return _resp(200, {"status": "healthy", "version": SERVICE_VERSION, "ts": _now()})


def _handle_wrap(body: dict, caller: dict, request_id: str) -> dict:
    """
    POST /wrap

    Required body fields:
      plaintextKey  — base64-encoded 32-byte AES key to wrap

    Optional body fields:
      context       — dict of metadata (packageName, version, artifactId, etc.)
                      logged in audit records, not used cryptographically
    """
    caller_id = caller.get("callerId", "unknown")
    context   = body.get("context") or {}

    _log("info", "wrap_request",
         callerId=caller_id, requestId=request_id,
         hasContext=bool(context), contextKeys=list(context.keys()))

    # ── Validate plaintextKey ─────────────────────────────────────────────────
    plaintext_b64 = (body.get("plaintextKey") or "").strip()
    if not plaintext_b64:
        _log("warning", "wrap_missing_plaintext_key", callerId=caller_id, requestId=request_id)
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="missing_plaintext_key", requestId=request_id)
        return _err(400, "plaintextKey is required")

    try:
        plaintext_key = base64.b64decode(plaintext_b64)
    except Exception:
        _log("warning", "wrap_invalid_base64", callerId=caller_id, requestId=request_id)
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="invalid_base64", requestId=request_id)
        return _err(400, "plaintextKey must be valid base64-encoded bytes")

    if len(plaintext_key) != AES_KEY_SIZE:
        _log("warning", "wrap_wrong_key_size",
             callerId=caller_id, sizeBytes=len(plaintext_key), requestId=request_id)
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="wrong_key_size", sizeBytes=len(plaintext_key), requestId=request_id)
        return _err(400,
            f"plaintextKey must be exactly {AES_KEY_SIZE} bytes (AES-256), "
            f"got {len(plaintext_key)}")

    # ── Load master key ───────────────────────────────────────────────────────
    try:
        master_key = _get_master_key()
    except Exception as exc:
        _log("error", "wrap_master_key_unavailable",
             callerId=caller_id, error=str(exc), requestId=request_id)
        _audit("KEY_WRAP", caller_id, "wrap", "ERROR",
               reason="master_key_unavailable", requestId=request_id)
        return _err(500, "Key service temporarily unavailable")

    # ── Wrap ──────────────────────────────────────────────────────────────────
    key_id     = str(uuid.uuid4())
    wrapped    = _wrap_key(plaintext_key, master_key)
    wrapped_at = _now()

    _log("info", "wrap_success",
         callerId=caller_id, keyId=key_id, requestId=request_id,
         algorithm="AES-256-GCM", keyVersion=KEY_VERSION)
    _audit("KEY_WRAP", caller_id, "wrap", "SUCCESS",
           keyId=key_id, wrappedAt=wrapped_at,
           algorithm="AES-256-GCM", keyVersion=KEY_VERSION,
           context=context, requestId=request_id)

    return _resp(200, {
        "wrappedKey": wrapped,
        "keyId":      key_id,
        "wrappedAt":  wrapped_at,
        "algorithm":  "AES-256-GCM",
    })


def _handle_unwrap(body: dict, caller: dict, request_id: str) -> dict:
    """
    POST /unwrap

    Required body fields:
      wrappedKey    — string produced by a previous /wrap call

    Optional body fields:
      keyId         — UUID from the original /wrap response (for audit correlation)
      context       — dict of metadata (deviceId, packageName, version, consentId, etc.)
    """
    caller_id = caller.get("callerId", "unknown")
    context   = body.get("context") or {}
    key_id    = (body.get("keyId") or "").strip()

    _log("info", "unwrap_request",
         callerId=caller_id, keyId=key_id or "(none)", requestId=request_id,
         hasContext=bool(context), contextKeys=list(context.keys()))

    # ── Validate wrappedKey ───────────────────────────────────────────────────
    wrapped_key = (body.get("wrappedKey") or "").strip()
    if not wrapped_key:
        _log("warning", "unwrap_missing_wrapped_key",
             callerId=caller_id, requestId=request_id)
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="missing_wrapped_key", keyId=key_id, requestId=request_id)
        return _err(400, "wrappedKey is required")

    # ── Load master key ───────────────────────────────────────────────────────
    try:
        master_key = _get_master_key()
    except Exception as exc:
        _log("error", "unwrap_master_key_unavailable",
             callerId=caller_id, error=str(exc), requestId=request_id)
        _audit("KEY_UNWRAP", caller_id, "unwrap", "ERROR",
               reason="master_key_unavailable", keyId=key_id, requestId=request_id)
        return _err(500, "Key service temporarily unavailable")

    # ── Unwrap ────────────────────────────────────────────────────────────────
    try:
        plaintext_key = _unwrap_key(wrapped_key, master_key)
    except ValueError as exc:
        _log("warning", "unwrap_invalid_format",
             callerId=caller_id, keyId=key_id, error=str(exc), requestId=request_id)
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="invalid_wrapped_key_format", detail=str(exc),
               keyId=key_id, requestId=request_id)
        return _err(400, f"Invalid wrappedKey: {exc}")
    except Exception as exc:
        # Catches cryptography.exceptions.InvalidTag (GCM auth failure) and anything else
        _log("warning", "unwrap_authentication_failed",
             callerId=caller_id, keyId=key_id,
             errorType=type(exc).__name__, requestId=request_id)
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="authentication_failed",
               detail="GCM tag verification failed — key may be tampered or encrypted with a different master key",
               keyId=key_id, context=context, requestId=request_id)
        return _err(400, "Wrapped key authentication failed — key is invalid or has been tampered with")

    unwrapped_at = _now()

    _log("info", "unwrap_success",
         callerId=caller_id, keyId=key_id, requestId=request_id,
         plaintextKeyBytes=len(plaintext_key))
    _audit("KEY_UNWRAP", caller_id, "unwrap", "SUCCESS",
           keyId=key_id, unwrappedAt=unwrapped_at,
           context=context, requestId=request_id)

    return _resp(200, {
        "plaintextKey": base64.b64encode(plaintext_key).decode(),
        "keyId":        key_id,
        "unwrappedAt":  unwrapped_at,
    })


# ── Lambda handler ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    method     = (event.get("httpMethod") or "").upper()
    path       = (event.get("path") or "").rstrip("/")
    headers    = event.get("headers") or {}
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    _log("debug", "request_received",
         method=method, path=path, requestId=request_id, sourceIp=source_ip)

    # ── Health — no auth required ─────────────────────────────────────────────
    if method == "GET" and path in ("/health", "/api/v1/ota/keys/health"):
        return _handle_health()

    # ── All other routes require auth ─────────────────────────────────────────
    caller = _authenticate(headers)
    if caller is None:
        _audit("AUTH_FAILURE", "unknown", f"{method} {path}", "REJECTED",
               reason="invalid_or_missing_api_key",
               requestId=request_id, sourceIp=source_ip)
        return _err(401, "Unauthorized — valid API key required")

    caller_id = caller.get("callerId", "unknown")
    _log("info", "request_authenticated",
         callerId=caller_id, method=method, path=path, requestId=request_id)

    # ── Parse body ────────────────────────────────────────────────────────────
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        _log("warning", "invalid_json_body",
             callerId=caller_id, requestId=request_id)
        return _err(400, "Request body must be valid JSON")

    if not isinstance(body, dict):
        _log("warning", "non_object_json_body",
             callerId=caller_id, bodyType=type(body).__name__, requestId=request_id)
        return _err(400, "Request body must be a JSON object")

    # ── Route ─────────────────────────────────────────────────────────────────
    if method == "POST" and path in ("/wrap", "/api/v1/ota/keys/wrap"):
        return _handle_wrap(body, caller, request_id)

    if method == "POST" and path in ("/unwrap", "/api/v1/ota/keys/unwrap"):
        return _handle_unwrap(body, caller, request_id)

    _log("warning", "route_not_found",
         method=method, path=path, callerId=caller_id, requestId=request_id)
    return _err(404, f"No route for {method} {path}")
