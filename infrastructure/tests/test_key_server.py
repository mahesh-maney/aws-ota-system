"""
Test suite for digilux_ota_key_server

Coverage:
  Authentication        — valid/invalid/missing API keys
  Health check          — no auth required, returns status
  Wrap                  — positive paths, all negative/edge cases
  Unwrap                — positive paths, tamper detection, format errors
  Routing               — unknown paths, wrong HTTP methods
  Error handling        — Secrets Manager failures, bad secret formats
  Crypto properties     — uniqueness, roundtrip fidelity, GCM authentication
  Audit events          — every operation emits a correctly structured audit record

Run:
  pip install pytest cryptography boto3
  pytest infrastructure/tests/test_key_server.py -v
"""

import base64
import json
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "06_lambdas", "digilux_ota_key_server"),
)
import lambda_function as lf


# ── Test constants ─────────────────────────────────────────────────────────────
MASTER_KEY      = os.urandom(32)   # valid 32-byte AES-256 master key
VALID_AES_KEY   = os.urandom(32)   # a key we'll wrap in tests
VALID_API_KEY   = "test-api-key-honeywell-001"
CALLER_INFO     = {"callerId": "honeywell-artifact-processor", "description": "Test caller"}
API_KEYS_MAP    = {VALID_API_KEY: CALLER_INFO}


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_caches():
    """Reset module-level caches before and after every test."""
    lf._master_key_cache = None
    lf._api_keys_cache   = None
    yield
    lf._master_key_cache = None
    lf._api_keys_cache   = None


@pytest.fixture
def mock_secrets(monkeypatch):
    """
    Patch Secrets Manager to return MASTER_KEY and API_KEYS_MAP.
    Tests that need different values can override via monkeypatch.
    """
    def _get_secret(SecretId):
        if SecretId == lf.MASTER_KEY_SECRET:
            return {"SecretString": base64.b64encode(MASTER_KEY).decode()}
        if SecretId == lf.API_KEYS_SECRET:
            return {"SecretString": json.dumps(API_KEYS_MAP)}
        raise Exception(f"Unknown secret: {SecretId}")

    monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _get_secret)


class MockContext:
    aws_request_id = "test-request-id-001"


# ── Event builder ──────────────────────────────────────────────────────────────

def _event(method: str, path: str, body=None, api_key: str = None) -> dict:
    headers = {}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    return {
        "httpMethod":     method,
        "path":           path,
        "headers":        headers,
        "body":           json.dumps(body) if body is not None else None,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "identity":  {"sourceIp": "10.0.0.1"},
        },
    }


def _invoke(method, path, body=None, api_key=VALID_API_KEY):
    """Convenience: invoke lambda_handler and return (status, body_dict)."""
    resp   = lf.lambda_handler(_event(method, path, body, api_key), MockContext())
    status = resp["statusCode"]
    data   = json.loads(resp["body"])
    return status, data


# ══════════════════════════════════════════════════════════════════════════════
# T01–T05  Authentication
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthentication:

    def test_T01_missing_authorization_header_returns_401(self, mock_secrets):
        """No Authorization header at all → 401."""
        status, body = _invoke("POST", "/wrap", {"plaintextKey": "x"}, api_key=None)
        assert status == 401
        assert "error" in body

    def test_T02_authorization_without_bearer_scheme_returns_401(self, mock_secrets, monkeypatch):
        """Authorization header present but not a Bearer token → 401."""
        event = _event("POST", "/wrap", {"plaintextKey": "x"})
        event["headers"]["Authorization"] = "Basic dXNlcjpwYXNz"
        resp  = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 401

    def test_T03_bearer_with_empty_value_returns_401(self, mock_secrets, monkeypatch):
        """Authorization: Bearer <empty> → 401."""
        event = _event("POST", "/wrap", {"plaintextKey": "x"})
        event["headers"]["Authorization"] = "Bearer "
        resp  = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 401

    def test_T04_invalid_api_key_returns_401(self, mock_secrets):
        """Wrong API key → 401."""
        status, body = _invoke("POST", "/wrap",
                               {"plaintextKey": base64.b64encode(VALID_AES_KEY).decode()},
                               api_key="totally-wrong-key")
        assert status == 401

    def test_T05_valid_api_key_reaches_handler(self, mock_secrets):
        """Valid API key → handler runs (not 401)."""
        status, _ = _invoke("POST", "/wrap",
                            {"plaintextKey": base64.b64encode(VALID_AES_KEY).decode()})
        assert status != 401


# ══════════════════════════════════════════════════════════════════════════════
# T06–T07  Health check
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:

    def test_T06_health_returns_200_without_auth(self):
        """GET /health requires no auth and returns healthy."""
        status, body = _invoke("GET", "/health", api_key=None)
        assert status == 200
        assert body["status"] == "healthy"
        assert "version" in body
        assert "ts" in body

    def test_T07_health_on_full_path_also_works(self):
        """GET /api/v1/ota/keys/health also returns healthy."""
        status, body = _invoke("GET", "/api/v1/ota/keys/health", api_key=None)
        assert status == 200
        assert body["status"] == "healthy"


# ══════════════════════════════════════════════════════════════════════════════
# T08–T19  Wrap — positive cases
# ══════════════════════════════════════════════════════════════════════════════

class TestWrapPositive:

    def test_T08_wrap_valid_32_byte_key_returns_200(self, mock_secrets):
        """Wrapping a valid 32-byte AES key → 200 with wrappedKey."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert status == 200
        assert "wrappedKey" in body
        assert "keyId" in body
        assert "wrappedAt" in body
        assert body["algorithm"] == "AES-256-GCM"

    def test_T09_wrapped_key_has_version_prefix(self, mock_secrets):
        """wrappedKey must start with 'v1:'."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert body["wrappedKey"].startswith("v1:")

    def test_T10_key_id_is_valid_uuid(self, mock_secrets):
        """keyId in response must be a valid UUID."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        key_id = body["keyId"]
        uuid.UUID(key_id)  # raises ValueError if not a valid UUID

    def test_T11_two_wraps_of_same_key_produce_different_wrapped_keys(self, mock_secrets):
        """Random nonce means wrapping the same key twice gives different output."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body1 = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        _, body2 = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert body1["wrappedKey"] != body2["wrappedKey"]

    def test_T12_two_wraps_produce_different_key_ids(self, mock_secrets):
        """Each wrap generates a unique keyId."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body1 = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        _, body2 = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert body1["keyId"] != body2["keyId"]

    def test_T13_wrap_with_context_metadata_returns_200(self, mock_secrets):
        """Context metadata field is accepted and does not affect outcome."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, body = _invoke("POST", "/wrap", {
            "plaintextKey": b64_key,
            "context": {
                "packageName": "HomeAssistantUtility",
                "version":     "4.6.0",
                "artifactId":  str(uuid.uuid4()),
            },
        })
        assert status == 200
        assert "wrappedKey" in body

    def test_T14_wrap_on_full_path_also_works(self, mock_secrets):
        """POST /api/v1/ota/keys/wrap is an accepted alias."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, _ = _invoke("POST", "/api/v1/ota/keys/wrap", {"plaintextKey": b64_key})
        assert status == 200

    def test_T15_wrap_ignores_unknown_body_fields(self, mock_secrets):
        """Extra unknown fields in the request body are silently ignored."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, _ = _invoke("POST", "/wrap", {
            "plaintextKey": b64_key,
            "unknownField": "some-value",
            "anotherExtra": 42,
        })
        assert status == 200


# ══════════════════════════════════════════════════════════════════════════════
# T16–T22  Wrap — negative cases
# ══════════════════════════════════════════════════════════════════════════════

class TestWrapNegative:

    def test_T16_wrap_missing_plaintext_key_field_returns_400(self, mock_secrets):
        """Body without plaintextKey → 400."""
        status, body = _invoke("POST", "/wrap", {"context": {}})
        assert status == 400
        assert "plaintextKey" in body["error"].lower() or "required" in body["error"].lower()

    def test_T17_wrap_empty_plaintext_key_returns_400(self, mock_secrets):
        """plaintextKey = '' → 400."""
        status, body = _invoke("POST", "/wrap", {"plaintextKey": ""})
        assert status == 400

    def test_T18_wrap_invalid_base64_returns_400(self, mock_secrets):
        """plaintextKey that is not valid base64 → 400."""
        status, body = _invoke("POST", "/wrap", {"plaintextKey": "not-valid-base64!!!"})
        assert status == 400
        assert "base64" in body["error"].lower()

    def test_T19_wrap_key_too_short_returns_400(self, mock_secrets):
        """16-byte key (AES-128) is not accepted — must be 32 bytes."""
        short_key = base64.b64encode(os.urandom(16)).decode()
        status, body = _invoke("POST", "/wrap", {"plaintextKey": short_key})
        assert status == 400
        assert "32" in body["error"]

    def test_T20_wrap_key_too_long_returns_400(self, mock_secrets):
        """64-byte key is not accepted — must be exactly 32 bytes."""
        long_key = base64.b64encode(os.urandom(64)).decode()
        status, body = _invoke("POST", "/wrap", {"plaintextKey": long_key})
        assert status == 400
        assert "32" in body["error"]

    def test_T21_wrap_zero_byte_key_returns_400(self, mock_secrets):
        """Empty decoded key (0 bytes) → 400."""
        status, body = _invoke("POST", "/wrap", {"plaintextKey": base64.b64encode(b"").decode()})
        assert status == 400

    def test_T22_wrap_without_auth_returns_401(self, mock_secrets):
        """Wrap request with no auth → 401 before any crypto."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, _ = _invoke("POST", "/wrap", {"plaintextKey": b64_key}, api_key=None)
        assert status == 401


# ══════════════════════════════════════════════════════════════════════════════
# T23–T30  Unwrap — positive cases
# ══════════════════════════════════════════════════════════════════════════════

class TestUnwrapPositive:

    def _do_wrap(self, mock_secrets_fixture):
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        return body["wrappedKey"], body["keyId"]

    def test_T23_unwrap_freshly_wrapped_key_returns_200(self, mock_secrets):
        """Unwrapping a just-wrapped key → 200."""
        wrapped, key_id = self._do_wrap(mock_secrets)
        status, body = _invoke("POST", "/unwrap", {
            "wrappedKey": wrapped,
            "keyId":      key_id,
        })
        assert status == 200
        assert "plaintextKey" in body

    def test_T24_unwrap_roundtrip_plaintext_matches_original(self, mock_secrets):
        """Wrap then Unwrap must return exactly the original AES key."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, wrap_body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        _, unwrap_body = _invoke("POST", "/unwrap", {
            "wrappedKey": wrap_body["wrappedKey"],
            "keyId":      wrap_body["keyId"],
        })
        recovered = base64.b64decode(unwrap_body["plaintextKey"])
        assert recovered == VALID_AES_KEY

    def test_T25_unwrap_without_key_id_still_works(self, mock_secrets):
        """keyId is optional in unwrap — omitting it still succeeds."""
        wrapped, _ = self._do_wrap(mock_secrets)
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": wrapped})
        assert status == 200
        assert "plaintextKey" in body

    def test_T26_unwrap_with_context_metadata_returns_200(self, mock_secrets):
        """Context metadata in unwrap request is accepted."""
        wrapped, key_id = self._do_wrap(mock_secrets)
        status, _ = _invoke("POST", "/unwrap", {
            "wrappedKey": wrapped,
            "keyId":      key_id,
            "context": {
                "deviceId":    str(uuid.uuid4()),
                "packageName": "HomeAssistantUtility",
                "version":     "4.6.0",
                "consentId":   str(uuid.uuid4()),
            },
        })
        assert status == 200

    def test_T27_unwrap_response_contains_unwrapped_at_timestamp(self, mock_secrets):
        """Unwrap response must include unwrappedAt timestamp."""
        wrapped, _ = self._do_wrap(mock_secrets)
        _, body = _invoke("POST", "/unwrap", {"wrappedKey": wrapped})
        assert "unwrappedAt" in body
        assert body["unwrappedAt"].endswith("Z")

    def test_T28_unwrap_on_full_path_also_works(self, mock_secrets):
        """POST /api/v1/ota/keys/unwrap is an accepted alias."""
        wrapped, _ = self._do_wrap(mock_secrets)
        status, _ = _invoke("POST", "/api/v1/ota/keys/unwrap", {"wrappedKey": wrapped})
        assert status == 200

    def test_T29_multiple_different_keys_all_unwrap_correctly(self, mock_secrets):
        """Each wrapped key unwraps to its own distinct plaintext."""
        keys = [os.urandom(32) for _ in range(3)]
        wrapped_keys = []
        for k in keys:
            _, body = _invoke("POST", "/wrap",
                              {"plaintextKey": base64.b64encode(k).decode()})
            wrapped_keys.append(body["wrappedKey"])

        for original, wrapped in zip(keys, wrapped_keys):
            _, body = _invoke("POST", "/unwrap", {"wrappedKey": wrapped})
            recovered = base64.b64decode(body["plaintextKey"])
            assert recovered == original


# ══════════════════════════════════════════════════════════════════════════════
# T30–T38  Unwrap — negative cases
# ══════════════════════════════════════════════════════════════════════════════

class TestUnwrapNegative:

    def _wrap_one(self):
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        return body["wrappedKey"]

    def test_T30_unwrap_missing_wrapped_key_field_returns_400(self, mock_secrets):
        """Body without wrappedKey → 400."""
        status, body = _invoke("POST", "/unwrap", {"keyId": "some-id"})
        assert status == 400
        assert "wrappedKey" in body["error"].lower() or "required" in body["error"].lower()

    def test_T31_unwrap_empty_wrapped_key_returns_400(self, mock_secrets):
        """wrappedKey = '' → 400."""
        status, _ = _invoke("POST", "/unwrap", {"wrappedKey": ""})
        assert status == 400

    def test_T32_unwrap_missing_version_prefix_returns_400(self, mock_secrets):
        """Wrapped key with no 'v1:' prefix → 400."""
        raw_b64 = base64.urlsafe_b64encode(os.urandom(60)).decode()
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": raw_b64})
        assert status == 400
        assert "version" in body["error"].lower() or "invalid" in body["error"].lower()

    def test_T33_unwrap_unsupported_version_returns_400(self, mock_secrets):
        """Wrapped key with version 'v99:' → 400."""
        raw_b64 = base64.urlsafe_b64encode(os.urandom(60)).decode()
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": f"v99:{raw_b64}"})
        assert status == 400
        assert "v99" in body["error"] or "unsupported" in body["error"].lower()

    def test_T34_unwrap_invalid_base64_returns_400(self, mock_secrets):
        """Invalid base64url in the payload → 400."""
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": "v1:!!not-valid-base64!!"})
        assert status == 400
        assert "base64" in body["error"].lower() or "invalid" in body["error"].lower()

    def test_T35_unwrap_truncated_payload_returns_400(self, mock_secrets):
        """Payload shorter than 60 bytes → 400 (wrong binary length)."""
        short = base64.urlsafe_b64encode(os.urandom(20)).decode()
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": f"v1:{short}"})
        assert status == 400

    def test_T36_unwrap_tampered_ciphertext_returns_400(self, mock_secrets):
        """Flipping a byte in the ciphertext → GCM tag fails → 400."""
        wrapped = self._wrap_one()
        version, encoded = wrapped.split(":", 1)
        raw = bytearray(base64.urlsafe_b64decode(encoded + "=="))
        # Flip a byte in the middle of the ciphertext (bytes 12–44)
        raw[20] ^= 0xFF
        tampered = f"{version}:{base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')}"

        status, body = _invoke("POST", "/unwrap", {"wrappedKey": tampered})
        assert status == 400
        assert "authentication failed" in body["error"].lower() or "tampered" in body["error"].lower()

    def test_T37_unwrap_tampered_tag_returns_400(self, mock_secrets):
        """Flipping a byte in the GCM tag → authentication fails → 400."""
        wrapped = self._wrap_one()
        version, encoded = wrapped.split(":", 1)
        raw = bytearray(base64.urlsafe_b64decode(encoded + "=="))
        # Flip a byte in the tag (last 16 bytes, index 44–59)
        raw[-1] ^= 0x01
        tampered = f"{version}:{base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')}"

        status, body = _invoke("POST", "/unwrap", {"wrappedKey": tampered})
        assert status == 400

    def test_T38_unwrap_with_wrong_master_key_returns_400(self, mock_secrets, monkeypatch):
        """Key wrapped with master_key_A cannot be unwrapped with master_key_B."""
        wrapped = self._wrap_one()

        # Swap out master key before unwrapping
        different_master = os.urandom(32)
        lf._master_key_cache = different_master

        status, body = _invoke("POST", "/unwrap", {"wrappedKey": wrapped})
        assert status == 400
        assert "authentication failed" in body["error"].lower()

    def test_T39_unwrap_without_auth_returns_401(self, mock_secrets):
        """Unwrap request with no auth → 401."""
        wrapped = self._wrap_one()
        status, _ = _invoke("POST", "/unwrap", {"wrappedKey": wrapped}, api_key=None)
        assert status == 401


# ══════════════════════════════════════════════════════════════════════════════
# T40–T43  Routing
# ══════════════════════════════════════════════════════════════════════════════

class TestRouting:

    def test_T40_unknown_path_returns_404(self, mock_secrets):
        """POST to an unknown path → 404."""
        status, body = _invoke("POST", "/unknown/path", {"foo": "bar"})
        assert status == 404

    def test_T41_get_on_wrap_returns_404(self, mock_secrets):
        """GET /wrap (wrong method) → 404."""
        status, _ = _invoke("GET", "/wrap", api_key=VALID_API_KEY)
        assert status == 404

    def test_T42_post_on_health_with_auth_returns_404(self, mock_secrets):
        """POST /health with valid auth → 404 (health is GET only).
        Auth check runs before routing, so a valid key is needed to reach the 404."""
        status, _ = _invoke("POST", "/health", {})
        assert status == 404

    def test_T43_empty_path_returns_404(self, mock_secrets):
        """Empty path → 404."""
        status, _ = _invoke("POST", "", {})
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# T44–T46  Request body validation
# ══════════════════════════════════════════════════════════════════════════════

class TestBodyValidation:

    def test_T44_invalid_json_body_returns_400(self, mock_secrets, monkeypatch):
        """Non-JSON body → 400."""
        event = _event("POST", "/wrap", api_key=VALID_API_KEY)
        event["body"] = "this is not json {{"
        resp  = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 400
        assert "json" in json.loads(resp["body"])["error"].lower()

    def test_T45_json_array_body_returns_400(self, mock_secrets):
        """JSON array as body → 400 (must be object)."""
        event = _event("POST", "/wrap", api_key=VALID_API_KEY)
        event["body"] = json.dumps([1, 2, 3])
        resp  = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 400

    def test_T46_null_body_treated_as_empty_object(self, mock_secrets):
        """Null/absent body does not crash — treated as empty JSON object."""
        event = _event("POST", "/wrap", api_key=VALID_API_KEY)
        event["body"] = None
        resp  = lf.lambda_handler(event, MockContext())
        # Missing plaintextKey → 400, but no 500 crash
        assert resp["statusCode"] == 400


# ══════════════════════════════════════════════════════════════════════════════
# T47–T51  Error handling — Secrets Manager failures
# ══════════════════════════════════════════════════════════════════════════════

class TestSecretsManagerFailures:

    def _sm_error(self):
        from botocore.exceptions import ClientError
        return ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Secret not found"}},
            "GetSecretValue",
        )

    def test_T47_master_key_unavailable_on_wrap_returns_500(self, monkeypatch):
        """If Secrets Manager fails during wrap → 500 (not a crash)."""
        def _fail(SecretId):
            if SecretId == lf.MASTER_KEY_SECRET:
                raise self._sm_error()
            return {"SecretString": json.dumps(API_KEYS_MAP)}

        monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _fail)
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert status == 500
        assert "unavailable" in body["error"].lower()

    def test_T48_master_key_unavailable_on_unwrap_returns_500(self, monkeypatch):
        """If Secrets Manager fails during unwrap → 500 (not a crash)."""
        def _fail(SecretId):
            if SecretId == lf.MASTER_KEY_SECRET:
                raise self._sm_error()
            return {"SecretString": json.dumps(API_KEYS_MAP)}

        monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _fail)
        status, body = _invoke("POST", "/unwrap", {"wrappedKey": "v1:abc"})
        assert status == 500

    def test_T49_api_keys_secret_unavailable_returns_401(self, monkeypatch):
        """If API keys secret is unavailable → 401 (treat as auth failure, not 500)."""
        def _fail(SecretId):
            raise self._sm_error()

        monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _fail)
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, _ = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert status == 401

    def test_T50_master_key_wrong_size_raises_on_load(self, monkeypatch):
        """Master key that is not 32 bytes → load raises ValueError, wrap returns 500."""
        def _bad_secret(SecretId):
            if SecretId == lf.MASTER_KEY_SECRET:
                # 16-byte key — wrong size
                return {"SecretString": base64.b64encode(os.urandom(16)).decode()}
            return {"SecretString": json.dumps(API_KEYS_MAP)}

        monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _bad_secret)
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert status == 500

    def test_T51_api_keys_secret_invalid_json_returns_401(self, monkeypatch):
        """API keys secret contains non-JSON string → auth fails → 401."""
        def _bad_secret(SecretId):
            if SecretId == lf.API_KEYS_SECRET:
                return {"SecretString": "not-json{{"}
            return {"SecretString": base64.b64encode(MASTER_KEY).decode()}

        monkeypatch.setattr(lf.secretsmanager, "get_secret_value", _bad_secret)
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        status, _ = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        assert status == 401


# ══════════════════════════════════════════════════════════════════════════════
# T52–T55  Crypto properties
# ══════════════════════════════════════════════════════════════════════════════

class TestCryptoProperties:

    def test_T52_wrapped_binary_is_exactly_60_bytes(self, mock_secrets):
        """The base64url-decoded wrapped payload must be exactly 60 bytes."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, body  = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        _, encoded = body["wrappedKey"].split(":", 1)
        raw = base64.urlsafe_b64decode(encoded + "==")
        assert len(raw) == lf.WRAPPED_BINARY_SIZE  # 60

    def test_T53_roundtrip_preserves_all_32_bytes(self, mock_secrets):
        """Every byte of the original key is recovered after wrap + unwrap."""
        # Test with an all-zero key, all-FF key, and a random key
        for key in [b"\x00" * 32, b"\xff" * 32, os.urandom(32)]:
            lf._master_key_cache = None
            _, wrap_body = _invoke("POST", "/wrap",
                                   {"plaintextKey": base64.b64encode(key).decode()})
            _, unwrap_body = _invoke("POST", "/unwrap",
                                     {"wrappedKey": wrap_body["wrappedKey"]})
            assert base64.b64decode(unwrap_body["plaintextKey"]) == key

    def test_T54_direct_wrap_unwrap_via_crypto_functions(self):
        """Unit-test _wrap_key/_unwrap_key directly without going through Lambda."""
        master = os.urandom(32)
        key    = os.urandom(32)
        wrapped  = lf._wrap_key(key, master)
        recovered = lf._unwrap_key(wrapped, master)
        assert recovered == key

    def test_T55_unwrap_key_raises_on_wrong_master_key(self):
        """_unwrap_key must raise (not return wrong bytes) if wrong master key is used."""
        master_a = os.urandom(32)
        master_b = os.urandom(32)
        key      = os.urandom(32)
        wrapped  = lf._wrap_key(key, master_a)
        with pytest.raises(Exception):
            lf._unwrap_key(wrapped, master_b)


# ══════════════════════════════════════════════════════════════════════════════
# T56–T58  Audit events
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditEvents:

    def test_T56_successful_wrap_emits_audit_record(self, mock_secrets, capsys):
        """A successful wrap must emit an audit record with result=SUCCESS."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.strip().splitlines() if line.strip()]
        audit = [r for r in records if r.get("audit") and r.get("action") == "wrap"]
        assert len(audit) >= 1
        assert audit[-1]["result"] == "SUCCESS"
        assert audit[-1]["callerId"] == CALLER_INFO["callerId"]

    def test_T57_rejected_wrap_emits_audit_record_with_reason(self, mock_secrets, capsys):
        """A rejected wrap (wrong key size) must emit an audit REJECTED record."""
        short_key = base64.b64encode(os.urandom(16)).decode()
        _invoke("POST", "/wrap", {"plaintextKey": short_key})
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.strip().splitlines() if line.strip()]
        audit = [r for r in records if r.get("audit") and r.get("action") == "wrap"]
        assert len(audit) >= 1
        assert audit[-1]["result"] == "REJECTED"
        assert "reason" in audit[-1]

    def test_T58_successful_unwrap_emits_audit_record(self, mock_secrets, capsys):
        """A successful unwrap must emit an audit record with result=SUCCESS."""
        b64_key = base64.b64encode(VALID_AES_KEY).decode()
        _, wrap_body = _invoke("POST", "/wrap", {"plaintextKey": b64_key})
        capsys.readouterr()  # clear wrap output

        _invoke("POST", "/unwrap", {
            "wrappedKey": wrap_body["wrappedKey"],
            "keyId":      wrap_body["keyId"],
            "context":    {"deviceId": str(uuid.uuid4())},
        })
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.strip().splitlines() if line.strip()]
        audit = [r for r in records if r.get("audit") and r.get("action") == "unwrap"]
        assert len(audit) >= 1
        assert audit[-1]["result"] == "SUCCESS"
        assert audit[-1]["callerId"] == CALLER_INFO["callerId"]
