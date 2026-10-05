"""
digilux_ota_key_server
=======================
Digilux-hosted key management service for the OTA system.

Implements envelope encryption: per-artifact AES keys are wrapped
(encrypted) by a master key that never leaves this service.
Honeywell's OTA Lambdas call this service to wrap keys at upload
time and unwrap them at consent time.

Machine-to-machine API endpoints (Bearer API key required):
  POST /wrap     — wrap a 32-byte plaintext AES key
  POST /unwrap   — unwrap a previously wrapped AES key
  GET  /health   — health check (no auth required)

Browser endpoints (session cookie required):
  GET  /            — login page  (no auth required)
  POST /login       — authenticate and set session cookie
  GET  /dashboard   — admin dashboard
  POST /logout      — clear session

Authentication:
  API (machine):  Authorization: Bearer <api-key>
  Browser:        Session cookie (HMAC-SHA256 signed, 8-hour expiry)

Admin credentials:
  Stored in Secrets Manager: ADMIN_CREDS_SECRET_NAME
  Format: {"username": "...", "password": "..."}

Session secret:
  Stored in Secrets Manager: SESSION_SECRET_NAME
  Format: a random string used to sign session tokens

Wrap format:
  "v1:<base64url(nonce[12] | ciphertext[32] | tag[16])>"
"""

import base64
import datetime
import hashlib
import hmac as _hmac
import json
import logging
import os
import time
import uuid
from typing import Optional
import urllib.parse

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION                = os.environ.get("REGION",                  "ap-south-1")
MASTER_KEY_SECRET     = os.environ.get("MASTER_KEY_SECRET_NAME",  "digilux/ota/key-server/master-key")
API_KEYS_SECRET       = os.environ.get("API_KEYS_SECRET_NAME",    "digilux/ota/key-server/api-keys")
SESSION_SECRET_NAME   = os.environ.get("SESSION_SECRET_NAME",     "digilux/ota/key-server/session-secret")
ADMIN_CREDS_SECRET    = os.environ.get("ADMIN_CREDS_SECRET_NAME", "digilux/ota/key-server/admin-credentials")
SERVICE_VERSION       = os.environ.get("SERVICE_VERSION",         "1.0.0")

KEY_VERSION         = "v1"
AES_KEY_SIZE        = 32
NONCE_SIZE          = 12
TAG_SIZE            = 16
WRAPPED_BINARY_SIZE = NONCE_SIZE + AES_KEY_SIZE + TAG_SIZE  # 60 bytes

SESSION_DURATION_SEC = 8 * 3600   # 8 hours
SESSION_COOKIE_NAME  = "ks_session"

secretsmanager = boto3.client("secretsmanager", region_name=REGION)

# Module-level caches
_master_key_cache:     Optional[bytes] = None
_api_keys_cache:       Optional[dict]  = None
_session_secret_cache: Optional[bytes] = None
_admin_creds_cache:    Optional[dict]  = None


# ── Logging helpers ─────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _log(level: str, msg: str, **fields) -> None:
    record = {"msg": msg, "ts": _now(), **fields}
    getattr(log, level)(json.dumps(record, default=str))


def _audit(event: str, caller_id: str, action: str, result: str, **extra) -> None:
    print(json.dumps({
        "audit":    True,
        "event":    event,
        "ts":       _now(),
        "callerId": caller_id,
        "action":   action,
        "result":   result,
        **extra,
    }, default=str))


# ── Response helpers ─────────────────────────────────────────────────────────

def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }


def _err(code: int, message: str, **extra) -> dict:
    return _resp(code, {"error": message, **extra})


def _html_resp(code: int, html: str, extra_headers: dict = None, set_cookie: str = None) -> dict:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if extra_headers:
        headers.update(extra_headers)
    if set_cookie:
        headers["Set-Cookie"] = set_cookie
    resp = {"statusCode": code, "headers": headers, "body": html}
    if set_cookie:
        resp["cookies"] = [set_cookie]   # Function URL v2.0 format
    return resp


def _redirect(location: str, set_cookie: str = None) -> dict:
    headers = {"Location": location}
    if set_cookie:
        headers["Set-Cookie"] = set_cookie
    resp = {"statusCode": 302, "headers": headers, "body": ""}
    if set_cookie:
        resp["cookies"] = [set_cookie]
    return resp


# ── Secrets Manager ──────────────────────────────────────────────────────────

def _get_master_key() -> bytes:
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache
    _log("debug", "master_key_fetch_start", secret=MASTER_KEY_SECRET)
    try:
        resp = secretsmanager.get_secret_value(SecretId=MASTER_KEY_SECRET)
        raw  = resp.get("SecretString") or base64.b64decode(resp["SecretBinary"]).decode()
        key_bytes = base64.b64decode(raw.strip())
        if len(key_bytes) != AES_KEY_SIZE:
            raise ValueError(f"Master key must be {AES_KEY_SIZE} bytes, got {len(key_bytes)}")
        _master_key_cache = key_bytes
        _log("info", "master_key_loaded", keyLenBytes=len(key_bytes))
        return _master_key_cache
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        _log("error", "master_key_fetch_failed", awsError=code, awsMessage=msg)
        raise


def _get_api_keys() -> dict:
    global _api_keys_cache
    if _api_keys_cache is not None:
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


def _get_session_secret() -> bytes:
    global _session_secret_cache
    if _session_secret_cache is not None:
        return _session_secret_cache
    try:
        resp = secretsmanager.get_secret_value(SecretId=SESSION_SECRET_NAME)
        raw  = resp.get("SecretString") or base64.b64decode(resp["SecretBinary"]).decode()
        _session_secret_cache = raw.strip().encode()
        return _session_secret_cache
    except ClientError as exc:
        _log("error", "session_secret_fetch_failed",
             awsError=exc.response["Error"]["Code"],
             awsMessage=exc.response["Error"]["Message"])
        raise


def _get_admin_creds() -> dict:
    global _admin_creds_cache
    if _admin_creds_cache is not None:
        return _admin_creds_cache
    try:
        resp = secretsmanager.get_secret_value(SecretId=ADMIN_CREDS_SECRET)
        raw  = resp.get("SecretString") or base64.b64decode(resp["SecretBinary"]).decode()
        _admin_creds_cache = json.loads(raw)
        return _admin_creds_cache
    except ClientError as exc:
        _log("error", "admin_creds_fetch_failed",
             awsError=exc.response["Error"]["Code"],
             awsMessage=exc.response["Error"]["Message"])
        raise


# ── Session management ───────────────────────────────────────────────────────

def _make_session_token(username: str) -> str:
    secret  = _get_session_secret()
    expiry  = int(time.time()) + SESSION_DURATION_SEC
    payload = f"{username}|{expiry}"
    sig     = _hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    raw     = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _validate_session_token(token: str) -> Optional[str]:
    """Returns username if valid and not expired, else None."""
    try:
        secret  = _get_session_secret()
        # Restore padding
        padded  = token + "=" * (-len(token) % 4)
        raw     = base64.urlsafe_b64decode(padded).decode()
        # Expected format: username|expiry|sig
        last_pipe = raw.rfind("|")
        if last_pipe < 0:
            return None
        payload, sig = raw[:last_pipe], raw[last_pipe + 1:]
        expected = _hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, sig):
            return None
        parts = payload.split("|", 1)
        if len(parts) != 2:
            return None
        username, expiry = parts
        if int(time.time()) > int(expiry):
            return None
        return username
    except Exception:
        return None


def _get_session_from_event(event: dict) -> Optional[str]:
    """Extract session cookie value from any event format."""
    cookies: dict = {}
    # Function URL v2.0: cookies array
    for c in (event.get("cookies") or []):
        if "=" in c:
            k, v = c.split("=", 1)
            cookies[k.strip()] = v.strip()
    # API Gateway v1 / HTTP headers
    cookie_hdr = ""
    hdrs = event.get("headers") or {}
    for key in ("Cookie", "cookie"):
        if hdrs.get(key):
            cookie_hdr = hdrs[key]
            break
    for part in cookie_hdr.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies.get(SESSION_COOKIE_NAME)


def _build_session_cookie(token: str, clear: bool = False) -> str:
    if clear:
        return f"{SESSION_COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"
    return (f"{SESSION_COOKIE_NAME}={token}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={SESSION_DURATION_SEC}")


# ── API Authentication ───────────────────────────────────────────────────────

def _authenticate_api(headers: dict) -> Optional[dict]:
    """Validate Bearer token. Returns caller info or None."""
    auth = (headers or {}).get("Authorization") or (headers or {}).get("authorization", "")
    if not auth.startswith("Bearer "):
        _log("warning", "auth_missing_bearer_scheme", headerPresent=bool(auth))
        return None
    api_key = auth[7:].strip()
    if not api_key:
        return None
    try:
        registry = _get_api_keys()
    except Exception as exc:
        _log("error", "auth_registry_unavailable", error=str(exc))
        return None
    caller = registry.get(api_key)
    if not caller:
        _log("warning", "auth_invalid_api_key",
             keyPrefix=api_key[:4] + "****" if len(api_key) >= 4 else "****")
        return None
    return caller


# ── Crypto ───────────────────────────────────────────────────────────────────

def _wrap_key(plaintext_key: bytes, master_key: bytes) -> str:
    if len(master_key) != AES_KEY_SIZE:
        raise ValueError(f"master_key must be {AES_KEY_SIZE} bytes")
    if len(plaintext_key) != AES_KEY_SIZE:
        raise ValueError(f"plaintext_key must be {AES_KEY_SIZE} bytes")
    nonce          = os.urandom(NONCE_SIZE)
    aesgcm         = AESGCM(master_key)
    ciphertext_tag = aesgcm.encrypt(nonce, plaintext_key, None)
    raw            = nonce + ciphertext_tag
    encoded        = base64.urlsafe_b64encode(raw).decode()
    return f"{KEY_VERSION}:{encoded}"


def _unwrap_key(wrapped_key: str, master_key: bytes) -> bytes:
    if not wrapped_key or ":" not in wrapped_key:
        raise ValueError("Wrapped key missing version prefix")
    version, encoded = wrapped_key.split(":", 1)
    if version != KEY_VERSION:
        raise ValueError(f"Unsupported key version: {version!r}")
    try:
        raw = base64.urlsafe_b64decode(encoded + "==")
    except Exception:
        raise ValueError("Wrapped key contains invalid base64url data")
    if len(raw) != WRAPPED_BINARY_SIZE:
        raise ValueError(f"Wrapped key binary length {len(raw)} != expected {WRAPPED_BINARY_SIZE}")
    nonce          = raw[:NONCE_SIZE]
    ciphertext_tag = raw[NONCE_SIZE:]
    aesgcm    = AESGCM(master_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext_tag, None)
    return plaintext


# ── HTML templates ───────────────────────────────────────────────────────────

_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
          padding: 2.5rem; width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(0,0,0,.4); }
  .logo { font-size: 1.6rem; font-weight: 700; color: #38bdf8; margin-bottom: .25rem; }
  .subtitle { color: #94a3b8; font-size: .875rem; margin-bottom: 2rem; }
  label { display: block; font-size: .8125rem; font-weight: 500; color: #94a3b8;
          margin-bottom: .375rem; }
  input { width: 100%; padding: .625rem .875rem; background: #0f172a;
          border: 1px solid #334155; border-radius: 6px; color: #e2e8f0;
          font-size: .9375rem; outline: none; }
  input:focus { border-color: #38bdf8; }
  .field { margin-bottom: 1.25rem; }
  .btn { width: 100%; padding: .75rem; background: #0ea5e9; border: none;
         border-radius: 6px; color: #fff; font-size: 1rem; font-weight: 600;
         cursor: pointer; margin-top: .5rem; }
  .btn:hover { background: #0284c7; }
  .error { background: #450a0a; border: 1px solid #991b1b; border-radius: 6px;
           padding: .75rem 1rem; color: #fca5a5; font-size: .875rem;
           margin-bottom: 1rem; }
  .dash-header { display: flex; align-items: center; justify-content: space-between;
                 margin-bottom: 2rem; }
  .dash-title { font-size: 1.25rem; font-weight: 700; color: #38bdf8; }
  .logout { background: none; border: 1px solid #334155; color: #94a3b8;
            border-radius: 6px; padding: .375rem .875rem; cursor: pointer;
            font-size: .8125rem; }
  .logout:hover { border-color: #94a3b8; color: #e2e8f0; }
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
               margin-bottom: 1.5rem; }
  .stat { background: #0f172a; border: 1px solid #334155; border-radius: 8px;
          padding: 1rem 1.25rem; }
  .stat-label { font-size: .75rem; color: #64748b; text-transform: uppercase;
                letter-spacing: .05em; margin-bottom: .375rem; }
  .stat-value { font-size: 1.5rem; font-weight: 700; color: #e2e8f0; }
  .stat-value.green { color: #4ade80; }
  .section-title { font-size: .875rem; font-weight: 600; color: #94a3b8;
                   text-transform: uppercase; letter-spacing: .06em;
                   margin-bottom: .75rem; }
  .key-row { display: flex; align-items: center; justify-content: space-between;
             padding: .625rem .875rem; background: #0f172a; border: 1px solid #1e293b;
             border-radius: 6px; margin-bottom: .5rem; }
  .key-id { font-size: .875rem; color: #e2e8f0; }
  .key-desc { font-size: .75rem; color: #64748b; }
  .badge { font-size: .6875rem; padding: .1875rem .5rem; border-radius: 9999px;
           background: #134e4a; color: #5eead4; }
  .dash-card { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
               padding: 1.5rem; max-width: 700px; width: 100%; margin: 2rem auto; }
  @media (max-width: 480px) { .dash-card { margin: 1rem; } }
"""

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Digilux Key Server</title>
  <style>{css}</style>
</head>
<body>
  <div class="card">
    <div class="logo">&#128273; Key Server</div>
    <div class="subtitle">Digilux OTA — Key Management Service</div>
    {error_block}
    <form method="POST" action="/login">
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username"
               placeholder="admin" required autofocus>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password"
               autocomplete="current-password" required>
      </div>
      <button class="btn" type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Key Server Dashboard</title>
  <style>{css}</style>
</head>
<body style="align-items:flex-start; padding: 1.5rem 1rem;">
  <div class="dash-card">
    <div class="dash-header">
      <div>
        <div class="dash-title">&#128273; Key Server Dashboard</div>
        <div style="font-size:.8125rem;color:#64748b;margin-top:.25rem;">
          Signed in as <strong style="color:#94a3b8">{username}</strong>
        </div>
      </div>
      <form method="POST" action="/logout">
        <button class="logout" type="submit">Sign out</button>
      </form>
    </div>

    <div class="stat-grid">
      <div class="stat">
        <div class="stat-label">Service Status</div>
        <div class="stat-value green">&#9679; Healthy</div>
      </div>
      <div class="stat">
        <div class="stat-label">Version</div>
        <div class="stat-value">{version}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Registered API Keys</div>
        <div class="stat-value">{key_count}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Master Key</div>
        <div class="stat-value green">&#10003; Loaded</div>
      </div>
    </div>

    <div class="section-title">Registered API Keys</div>
    {keys_block}

    <div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #334155;
                font-size:.75rem;color:#475569;text-align:center;">
      Digilux OTA Key Server &middot; {ts}
    </div>
  </div>
</body>
</html>"""


# ── Web route handlers ───────────────────────────────────────────────────────

def _handle_login_page(error: str = "") -> dict:
    error_block = ""
    if error:
        error_block = f'<div class="error">{error}</div>'
    html = _LOGIN_HTML.format(css=_CSS, error_block=error_block)
    return _html_resp(200, html)


def _handle_login_post(event: dict) -> dict:
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode()
    params = dict(urllib.parse.parse_qsl(body_raw))
    username = (params.get("username") or "").strip()
    password = (params.get("password") or "").strip()

    if not username or not password:
        return _handle_login_page("Username and password are required.")

    try:
        creds = _get_admin_creds()
    except Exception:
        return _handle_login_page("Authentication service unavailable. Try again later.")

    stored_user = creds.get("username", "")
    stored_pass = creds.get("password", "")

    # Constant-time comparison to prevent timing attacks
    user_match = _hmac.compare_digest(stored_user, username)
    pass_match = _hmac.compare_digest(stored_pass, password)

    if not (user_match and pass_match):
        _audit("WEB_LOGIN", username, "login", "REJECTED", reason="invalid_credentials")
        _log("warning", "web_login_failed", username=username)
        return _handle_login_page("Invalid username or password.")

    token  = _make_session_token(username)
    cookie = _build_session_cookie(token)
    _audit("WEB_LOGIN", username, "login", "SUCCESS")
    _log("info", "web_login_success", username=username)
    return _redirect("/dashboard", set_cookie=cookie)


def _handle_dashboard(username: str) -> dict:
    # Try to load API keys list for display
    keys_block = ""
    key_count  = "—"
    master_ok  = False

    try:
        api_keys = _get_api_keys()
        key_count = str(len(api_keys))
        rows = []
        for key_val, info in api_keys.items():
            caller_id = info.get("callerId", "unknown")
            desc      = info.get("description", "")
            masked    = key_val[:4] + "••••••••" + key_val[-4:] if len(key_val) >= 8 else "••••••••"
            rows.append(
                f'<div class="key-row">'
                f'<div><div class="key-id">{caller_id}</div>'
                f'<div class="key-desc">{desc}</div></div>'
                f'<div><span class="badge">{masked}</span></div>'
                f'</div>'
            )
        keys_block = "\n".join(rows) if rows else '<div style="color:#64748b;font-size:.875rem">No API keys registered.</div>'
    except Exception:
        keys_block = '<div style="color:#f87171;font-size:.875rem">Could not load API keys.</div>'

    try:
        _get_master_key()
        master_ok = True
    except Exception:
        pass

    html = _DASHBOARD_HTML.format(
        css=_CSS,
        username=username,
        version=SERVICE_VERSION,
        key_count=key_count,
        keys_block=keys_block,
        ts=_now(),
    )
    return _html_resp(200, html)


def _handle_logout() -> dict:
    cookie = _build_session_cookie("", clear=True)
    return _redirect("/", set_cookie=cookie)


# ── API route handlers ───────────────────────────────────────────────────────

def _handle_health() -> dict:
    _log("debug", "health_check_ok", version=SERVICE_VERSION)
    return _resp(200, {"status": "healthy", "version": SERVICE_VERSION, "ts": _now()})


def _handle_wrap(body: dict, caller: dict, request_id: str) -> dict:
    caller_id = caller.get("callerId", "unknown")
    context   = body.get("context") or {}
    _log("info", "wrap_request", callerId=caller_id, requestId=request_id,
         hasContext=bool(context), contextKeys=list(context.keys()))

    plaintext_b64 = (body.get("plaintextKey") or "").strip()
    if not plaintext_b64:
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="missing_plaintext_key", requestId=request_id)
        return _err(400, "plaintextKey is required")

    try:
        plaintext_key = base64.b64decode(plaintext_b64)
    except Exception:
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="invalid_base64", requestId=request_id)
        return _err(400, "plaintextKey must be valid base64-encoded bytes")

    if len(plaintext_key) != AES_KEY_SIZE:
        _audit("KEY_WRAP", caller_id, "wrap", "REJECTED",
               reason="wrong_key_size", sizeBytes=len(plaintext_key), requestId=request_id)
        return _err(400, f"plaintextKey must be exactly {AES_KEY_SIZE} bytes (AES-256), got {len(plaintext_key)}")

    try:
        master_key = _get_master_key()
    except Exception as exc:
        _audit("KEY_WRAP", caller_id, "wrap", "ERROR",
               reason="master_key_unavailable", requestId=request_id)
        return _err(500, "Key service temporarily unavailable")

    key_id     = str(uuid.uuid4())
    wrapped    = _wrap_key(plaintext_key, master_key)
    wrapped_at = _now()

    _log("info", "wrap_success", callerId=caller_id, keyId=key_id, requestId=request_id,
         algorithm="AES-256-GCM", keyVersion=KEY_VERSION)
    _audit("KEY_WRAP", caller_id, "wrap", "SUCCESS",
           keyId=key_id, wrappedAt=wrapped_at, algorithm="AES-256-GCM",
           keyVersion=KEY_VERSION, context=context, requestId=request_id)

    return _resp(200, {
        "wrappedKey": wrapped,
        "keyId":      key_id,
        "wrappedAt":  wrapped_at,
        "algorithm":  "AES-256-GCM",
    })


def _handle_unwrap(body: dict, caller: dict, request_id: str) -> dict:
    caller_id  = caller.get("callerId", "unknown")
    context    = body.get("context") or {}
    key_id     = (body.get("keyId") or "").strip()
    _log("info", "unwrap_request", callerId=caller_id, keyId=key_id or "(none)",
         requestId=request_id, hasContext=bool(context), contextKeys=list(context.keys()))

    wrapped_key = (body.get("wrappedKey") or "").strip()
    if not wrapped_key:
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="missing_wrapped_key", keyId=key_id, requestId=request_id)
        return _err(400, "wrappedKey is required")

    try:
        master_key = _get_master_key()
    except Exception as exc:
        _audit("KEY_UNWRAP", caller_id, "unwrap", "ERROR",
               reason="master_key_unavailable", keyId=key_id, requestId=request_id)
        return _err(500, "Key service temporarily unavailable")

    try:
        plaintext_key = _unwrap_key(wrapped_key, master_key)
    except ValueError as exc:
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="invalid_wrapped_key_format", detail=str(exc),
               keyId=key_id, requestId=request_id)
        return _err(400, f"Invalid wrappedKey: {exc}")
    except Exception as exc:
        _audit("KEY_UNWRAP", caller_id, "unwrap", "REJECTED",
               reason="authentication_failed",
               detail="GCM tag verification failed",
               keyId=key_id, context=context, requestId=request_id)
        return _err(400, "Wrapped key authentication failed — key is invalid or has been tampered with")

    _log("info", "unwrap_success", callerId=caller_id, keyId=key_id,
         requestId=request_id, plaintextKeyBytes=len(plaintext_key))
    _audit("KEY_UNWRAP", caller_id, "unwrap", "SUCCESS",
           keyId=key_id, unwrappedAt=_now(), context=context, requestId=request_id)

    return _resp(200, {
        "plaintextKey": base64.b64encode(plaintext_key).decode(),
        "keyId":        key_id,
        "unwrappedAt":  _now(),
    })


# ── Lambda handler ───────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # ── EventBridge keep-warm ping — return immediately ───────────────────────
    if event.get("keepwarm") or event.get("source") == "aws.events":
        _log("debug", "keepwarm_ping")
        return {"statusCode": 200, "body": "warm"}

    request_id = getattr(context, "aws_request_id", None) or str(uuid.uuid4())

    # Support both API Gateway v1 (httpMethod/path) and Function URL v2.0 (requestContext.http)
    http_ctx = (event.get("requestContext") or {}).get("http") or {}
    method   = (event.get("httpMethod") or http_ctx.get("method") or "GET").upper()
    path     = (event.get("path") or event.get("rawPath") or "/").rstrip("/") or "/"
    headers  = event.get("headers") or {}
    source_ip = (
        (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")
        or http_ctx.get("sourceIp", "")
    )

    _log("debug", "request_received",
         method=method, path=path, requestId=request_id, sourceIp=source_ip)

    # ── Health — no auth ──────────────────────────────────────────────────────
    if method == "GET" and path in ("/health", "/api/v1/ota/keys/health"):
        return _handle_health()

    # ── Browser routes ────────────────────────────────────────────────────────
    # Login page
    if method == "GET" and path in ("", "/", "/login"):
        return _handle_login_page()

    # Login POST
    if method == "POST" and path in ("/login",):
        return _handle_login_post(event)

    # Logout
    if method == "POST" and path in ("/logout",):
        return _handle_logout()

    # Dashboard — requires session
    if method == "GET" and path in ("/dashboard",):
        token    = _get_session_from_event(event)
        username = _validate_session_token(token) if token else None
        if not username:
            return _redirect("/")
        return _handle_dashboard(username)

    # ── Machine API routes — require Bearer API key ───────────────────────────
    caller = _authenticate_api(headers)
    if caller is None:
        _audit("AUTH_FAILURE", "unknown", f"{method} {path}", "REJECTED",
               reason="invalid_or_missing_api_key",
               requestId=request_id, sourceIp=source_ip)
        return _err(401, "Unauthorized — valid API key required")

    caller_id = caller.get("callerId", "unknown")
    _log("info", "request_authenticated",
         callerId=caller_id, method=method, path=path, requestId=request_id)

    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode()
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        return _err(400, "Request body must be valid JSON")
    if not isinstance(body, dict):
        return _err(400, "Request body must be a JSON object")

    if method == "POST" and path in ("/wrap", "/api/v1/ota/keys/wrap"):
        return _handle_wrap(body, caller, request_id)

    if method == "POST" and path in ("/unwrap", "/api/v1/ota/keys/unwrap"):
        return _handle_unwrap(body, caller, request_id)

    _log("warning", "route_not_found", method=method, path=path,
         callerId=caller_id, requestId=request_id)
    return _err(404, f"No route for {method} {path}")
