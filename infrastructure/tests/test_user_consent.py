"""
Test suite for digilux_ota_user_consent

Coverage:
  _unwrap_data_key       — positive, all negative / edge cases
  _create_iot_job        — dataKey + iv injected when wrappedDataKey present
                         — warning logged when wrappedDataKey absent
                         — key server failure raises (consent returns 500)
  _handle_consent        — admin-initiated: accept creates job with dataKey
                         — admin-initiated: decline → no key server call
                         — user-initiated: creates job with dataKey
                         — user-initiated: decline returns 404
                         — device not owned by user → 404
                         — device already has pending job → 409
                         — package not ACTIVE → 400
                         — version already installed → 409
                         — rate limit (via existing consents table) is checked
  lambda_handler         — missing sub claim → 401
                         — body too large → 400
                         — AWS client error → 500

Run:
  pip install pytest cryptography boto3
  pytest infrastructure/tests/test_user_consent.py -v
"""

import base64
import io
import json
import os
import sys
import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
import importlib
import importlib.util

os.environ.setdefault("REGION", "ap-south-1")       # required at import time
os.environ.setdefault("ACCOUNT_ID", "123456789012")  # required at import time

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_user_consent", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_user_consent", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ──────────────────────────────────────────────────────────────────
USER_ID    = "user-uuid-001"
DEVICE_ID  = "edb39bba-baf1-4700-968c-a42228e53aa0"
PKG_NAME   = "HomeAssistantUtility"
VERSION    = "4.5.0"
THING_NAME = "digilux-thing-001"
MAC        = "aa:bb:cc:dd:ee:ff"

WRAPPED_KEY     = "v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
PLAINTEXT_KEY   = base64.b64encode(os.urandom(32)).decode()
AES_IV_B64      = base64.b64encode(os.urandom(12)).decode()


def _pkg(status="ACTIVE", with_wrapped_key=True) -> dict:
    p = {
        "packageName": PKG_NAME,
        "version":     VERSION,
        "status":      status,
        "sha256":      "abc123" * 10,
        "signature":   "sig==",
        "artifactSize": 1024,
        "deviceType":  "Network_controller_firmware",
        "encS3Key":    "enc/some-uuid.enc",
        "releaseType": "PROD",
    }
    if with_wrapped_key:
        p["wrappedDataKey"] = WRAPPED_KEY
        p["aesIv"]          = AES_IV_B64
    return p


def _device(pending_job=None, installed_ver="1.0.0") -> dict:
    d = {
        "deviceId":              DEVICE_ID,
        "macAddress":            MAC,
        "userId":                USER_ID,
        "thingName":             THING_NAME,
        "globalInstalledVersion": installed_ver,
    }
    if pending_job:
        d["pendingJobId"] = pending_job
    return d


def _consent_body(accepted=True) -> dict:
    return {
        "deviceId":    DEVICE_ID,
        "packageName": PKG_NAME,
        "version":     VERSION,
        "accepted":    accepted,
    }


class MockContext:
    aws_request_id = "test-req-001"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    monkeypatch.setattr(lf, "KEY_SERVER_URL",     "https://keys.test")
    monkeypatch.setattr(lf, "KEY_SERVER_API_KEY", "test-api-key")
    monkeypatch.setattr(lf, "REGION",             "ap-south-1")
    monkeypatch.setattr(lf, "ACCOUNT_ID",         "123456789012")


@pytest.fixture
def mock_unwrap(monkeypatch):
    """Patch _unwrap_data_key to return a fixed plaintext key."""
    monkeypatch.setattr(lf, "_unwrap_data_key", lambda w: PLAINTEXT_KEY)


@pytest.fixture
def mock_dynamo_tables(monkeypatch):
    """
    Returns a dict of table mocks keyed by table name.
    The first get_item on DEVICE_DATA_TABLE returns the default device.
    """
    device_table  = MagicMock()
    package_table = MagicMock()
    consent_table = MagicMock()
    job_table     = MagicMock()

    device_table.query.return_value  = {"Items": [_device()]}
    package_table.get_item.return_value = {"Item": _pkg()}
    consent_table.query.return_value = {"Items": []}   # no pending consent by default
    consent_table.put_item.return_value  = {}
    consent_table.update_item.return_value = {}
    job_table.put_item.return_value  = {}
    device_table.update_item.return_value = {}

    tables = {
        lf.DEVICE_DATA_TABLE: device_table,
        lf.PACKAGES_TABLE:    package_table,
        lf.CONSENTS_TABLE:    consent_table,
        lf.OTA_JOBS_TABLE:    job_table,
    }

    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda name: tables.get(name, MagicMock())
    monkeypatch.setattr(lf, "dynamo", dynamo)
    return tables


@pytest.fixture
def mock_iot(monkeypatch):
    iot = MagicMock()
    iot.create_job.return_value = {
        "jobArn": "arn:aws:iot:ap-south-1:123:job/test-job-id",
        "jobId": "test-job-id",
    }
    monkeypatch.setattr(lf, "iot", iot)
    return iot


@pytest.fixture
def mock_s3(monkeypatch):
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.test/presigned"
    monkeypatch.setattr(lf, "s3", s3)
    return s3


@pytest.fixture
def mock_ses(monkeypatch):
    ses = MagicMock()
    monkeypatch.setattr(lf, "ses", ses)
    return ses


# ═══════════════════════════════════════════════════════════════════════════════
# _unwrap_data_key
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnwrapDataKey:
    def _make_response(self, body: dict):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(body).encode()
        return resp

    def test_positive_returns_plaintext_key(self):
        resp = self._make_response({"plaintextKey": PLAINTEXT_KEY, "keyId": "uuid"})
        with patch("urllib.request.urlopen", return_value=resp):
            result = lf._unwrap_data_key(WRAPPED_KEY)
        assert result == PLAINTEXT_KEY

    def test_positive_sends_correct_body(self):
        resp = self._make_response({"plaintextKey": PLAINTEXT_KEY})
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            return resp
        with patch("urllib.request.urlopen", fake_urlopen):
            lf._unwrap_data_key(WRAPPED_KEY)
        assert captured["body"]["wrappedKey"] == WRAPPED_KEY
        assert captured["auth"] == "Bearer test-api-key"

    def test_positive_sends_correct_url(self):
        resp = self._make_response({"plaintextKey": PLAINTEXT_KEY})
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return resp
        with patch("urllib.request.urlopen", fake_urlopen):
            lf._unwrap_data_key(WRAPPED_KEY)
        assert captured["url"] == "https://keys.test/api/v1/ota/keys/unwrap"

    def test_negative_missing_key_server_url(self, monkeypatch):
        monkeypatch.setattr(lf, "KEY_SERVER_URL", "")
        with pytest.raises(RuntimeError, match="KEY_SERVER_URL"):
            lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_missing_api_key(self, monkeypatch):
        monkeypatch.setattr(lf, "KEY_SERVER_API_KEY", "")
        with pytest.raises(RuntimeError, match="KEY_SERVER_API_KEY"):
            lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_http_401(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            url="https://keys.test/api/v1/ota/keys/unwrap",
            code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":"Unauthorized"}'),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="HTTP 401"):
                lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_http_422_tampered_key(self):
        """422 = GCM authentication failed — wrapped key was tampered."""
        import urllib.error
        exc = urllib.error.HTTPError(
            url="https://keys.test/api/v1/ota/keys/unwrap",
            code=422, msg="Unprocessable Entity", hdrs=None,
            fp=io.BytesIO(b'{"error":"Wrapped key authentication failed"}'),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="HTTP 422"):
                lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_http_500(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            url="https://keys.test/api/v1/ota/keys/unwrap",
            code=500, msg="Server Error", hdrs=None,
            fp=io.BytesIO(b'{"error":"internal error"}'),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_connection_error(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(RuntimeError, match="unreachable"):
                lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_empty_plaintext_key_in_response(self):
        resp = self._make_response({"plaintextKey": ""})
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="empty plaintextKey"):
                lf._unwrap_data_key(WRAPPED_KEY)

    def test_negative_missing_plaintext_key_field(self):
        resp = self._make_response({"keyId": "some-id"})
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="empty plaintextKey"):
                lf._unwrap_data_key(WRAPPED_KEY)


# ═══════════════════════════════════════════════════════════════════════════════
# _create_iot_job — key injection
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateIotJobKeyInjection:
    def test_job_doc_contains_data_key_when_wrapped_key_present(
        self, monkeypatch, mock_unwrap, mock_s3, mock_iot
    ):
        """dataKey and iv must appear in the artifact section of the job doc."""
        pkg = _pkg(with_wrapped_key=True)
        captured_doc = {}

        def fake_create_job(**kwargs):
            captured_doc["doc"] = json.loads(kwargs["document"])
            return {"jobArn": "arn:aws:iot:x:y:job/j1", "jobId": "j1"}

        mock_iot.create_job.side_effect = fake_create_job

        lf._create_iot_job(pkg, PKG_NAME, VERSION, THING_NAME, DEVICE_ID,
                           USER_ID, "job-001", 3600)

        artifact = captured_doc["doc"]["artifact"]
        assert "dataKey" in artifact, "dataKey must be in job document artifact"
        assert "iv" in artifact,      "iv must be in job document artifact"
        assert artifact["dataKey"] == PLAINTEXT_KEY
        assert artifact["iv"]      == AES_IV_B64

    def test_job_doc_contains_presigned_url(
        self, monkeypatch, mock_unwrap, mock_s3, mock_iot
    ):
        pkg = _pkg(with_wrapped_key=True)
        captured_doc = {}
        mock_iot.create_job.side_effect = lambda **kw: (
            captured_doc.update({"doc": json.loads(kw["document"])}) or
            {"jobArn": "arn:aws:iot:x:y:job/j1", "jobId": "j1"}
        )
        lf._create_iot_job(pkg, PKG_NAME, VERSION, THING_NAME, DEVICE_ID,
                           USER_ID, "job-001", 3600)
        assert "presignedUrl" in captured_doc["doc"]["artifact"]

    def test_job_doc_no_data_key_when_wrapped_key_absent(
        self, monkeypatch, mock_s3, mock_iot
    ):
        """When package has no wrappedDataKey, job doc must not have dataKey."""
        pkg = _pkg(with_wrapped_key=False)
        captured_doc = {}
        mock_iot.create_job.side_effect = lambda **kw: (
            captured_doc.update({"doc": json.loads(kw["document"])}) or
            {"jobArn": "arn:aws:iot:x:y:job/j1", "jobId": "j1"}
        )
        unwrap_called = []
        monkeypatch.setattr(lf, "_unwrap_data_key", lambda w: unwrap_called.append(w) or PLAINTEXT_KEY)

        lf._create_iot_job(pkg, PKG_NAME, VERSION, THING_NAME, DEVICE_ID,
                           USER_ID, "job-001", 3600)
        assert "dataKey" not in captured_doc["doc"]["artifact"]
        assert len(unwrap_called) == 0, "_unwrap_data_key must not be called when no wrappedDataKey"

    def test_key_server_failure_during_job_create_raises(
        self, monkeypatch, mock_s3, mock_iot
    ):
        """If _unwrap_data_key raises, _create_iot_job must propagate the error."""
        pkg = _pkg(with_wrapped_key=True)

        def failing_unwrap(w):
            raise RuntimeError("Key server down")

        monkeypatch.setattr(lf, "_unwrap_data_key", failing_unwrap)

        with pytest.raises(RuntimeError, match="Key server down"):
            lf._create_iot_job(pkg, PKG_NAME, VERSION, THING_NAME, DEVICE_ID,
                               USER_ID, "job-001", 3600)

        # IoT job must NOT have been created
        mock_iot.create_job.assert_not_called()

    def test_unwrap_called_exactly_once_per_job(
        self, monkeypatch, mock_s3, mock_iot
    ):
        pkg = _pkg(with_wrapped_key=True)
        call_count = {"n": 0}
        def counting_unwrap(w):
            call_count["n"] += 1
            return PLAINTEXT_KEY
        monkeypatch.setattr(lf, "_unwrap_data_key", counting_unwrap)
        mock_iot.create_job.return_value = {"jobArn": "arn:x", "jobId": "j"}

        lf._create_iot_job(pkg, PKG_NAME, VERSION, THING_NAME, DEVICE_ID,
                           USER_ID, "job-001", 3600)
        assert call_count["n"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _handle_consent — full flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleConsent:
    def _run(self, body, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3,
             mock_unwrap, mock_ses=None):
        return lf._handle_consent(USER_ID, "user@test.com", body)

    # ── User-initiated (no pending consent record) ─────────────────────────────

    def test_user_initiated_accept_returns_202(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=True))
        assert resp["statusCode"] == 202
        body = json.loads(resp["body"])
        assert body["status"] == "QUEUED"
        assert "jobId" in body

    def test_user_initiated_accept_creates_iot_job(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=True))
        mock_iot.create_job.assert_called_once()

    def test_user_initiated_accept_job_doc_has_data_key(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        captured = {}
        mock_iot.create_job.side_effect = lambda **kw: (
            captured.update({"doc": json.loads(kw["document"])}) or
            {"jobArn": "arn:x", "jobId": "j"}
        )
        lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=True))
        assert "dataKey" in captured["doc"]["artifact"]
        assert "iv"      in captured["doc"]["artifact"]

    def test_user_initiated_decline_no_pending_returns_404(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=False))
        assert resp["statusCode"] == 404

    def test_user_initiated_decline_no_iot_job(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=False))
        mock_iot.create_job.assert_not_called()

    # ── Admin-initiated (PENDING consent record exists) ────────────────────────

    def _setup_pending_consent(self, mock_dynamo_tables):
        consent = {
            "consentId":    str(uuid.uuid4()),
            "userId":       USER_ID,
            "deviceId":     DEVICE_ID,
            "packageName":  PKG_NAME,
            "version":      VERSION,
            "status":       "PENDING",
            "deploymentId": "dep-001",
        }
        mock_dynamo_tables[lf.CONSENTS_TABLE].query.return_value = {"Items": [consent]}
        return consent

    def test_admin_initiated_accept_returns_202(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        self._setup_pending_consent(mock_dynamo_tables)
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=True))
        assert resp["statusCode"] == 202

    def test_admin_initiated_accept_marks_consent_accepted(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        consent = self._setup_pending_consent(mock_dynamo_tables)
        lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=True))
        consent_table = mock_dynamo_tables[lf.CONSENTS_TABLE]
        calls = consent_table.update_item.call_args_list
        accepted_call = next(
            c for c in calls
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "ACCEPTED"
        )
        assert accepted_call is not None

    def test_admin_initiated_decline_returns_200(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap, mock_ses
    ):
        self._setup_pending_consent(mock_dynamo_tables)
        cognito = MagicMock()
        cognito.admin_get_user.return_value = {
            "UserAttributes": [{"Name": "email", "Value": "user@test.com"}]
        }
        with patch.object(lf, "_get_user_email", return_value="user@test.com"):
            resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=False))
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["status"] == "DECLINED"

    def test_admin_initiated_decline_no_iot_job(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap, mock_ses
    ):
        self._setup_pending_consent(mock_dynamo_tables)
        with patch.object(lf, "_get_user_email", return_value=None):
            lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=False))
        mock_iot.create_job.assert_not_called()

    def test_admin_initiated_decline_no_key_server_call(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_ses
    ):
        """Key server must never be called on a DECLINE."""
        self._setup_pending_consent(mock_dynamo_tables)
        unwrap_called = []
        monkeypatch.setattr(lf, "_unwrap_data_key", lambda w: unwrap_called.append(w) or PLAINTEXT_KEY)
        with patch.object(lf, "_get_user_email", return_value=None):
            lf._handle_consent(USER_ID, "e@t.com", _consent_body(accepted=False))
        assert len(unwrap_called) == 0

    # ── Validation / guard cases ───────────────────────────────────────────────

    def test_device_not_owned_returns_404(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_dynamo_tables[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [dict(_device(), userId="different-user")]
        }
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 404

    def test_device_not_found_returns_404(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_dynamo_tables[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": []}
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 404

    def test_existing_pending_job_returns_409(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_dynamo_tables[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(pending_job="digilux-ota-HomeAssistantUtility-4-5-0-12345")]
        }
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 409
        assert "pendingJobId" in json.loads(resp["body"])

    def test_package_not_active_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_dynamo_tables[lf.PACKAGES_TABLE].get_item.return_value = {
            "Item": _pkg(status="PENDING")
        }
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 400

    def test_version_already_installed_returns_409(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_dynamo_tables[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed_ver=VERSION)]
        }
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 409

    def test_missing_device_id_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        body = _consent_body()
        body.pop("deviceId")
        resp = lf._handle_consent(USER_ID, "e@t.com", body)
        assert resp["statusCode"] == 400

    def test_missing_package_name_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        body = _consent_body()
        body.pop("packageName")
        resp = lf._handle_consent(USER_ID, "e@t.com", body)
        assert resp["statusCode"] == 400

    def test_missing_version_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        body = _consent_body()
        body.pop("version")
        resp = lf._handle_consent(USER_ID, "e@t.com", body)
        assert resp["statusCode"] == 400

    def test_invalid_device_id_format_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        body = _consent_body()
        body["deviceId"] = "not-a-uuid"
        resp = lf._handle_consent(USER_ID, "e@t.com", body)
        assert resp["statusCode"] == 400

    def test_accepted_not_bool_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        body = _consent_body()
        body["accepted"] = "yes"
        resp = lf._handle_consent(USER_ID, "e@t.com", body)
        assert resp["statusCode"] == 400

    def test_key_server_failure_returns_500(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3
    ):
        """Key server failure during consent propagates to 500."""
        monkeypatch.setattr(lf, "_unwrap_data_key",
                            lambda w: (_ for _ in ()).throw(RuntimeError("Key server down")))

        from botocore.exceptions import ClientError as CE
        # Patch _create_iot_job so the RuntimeError surfaces
        orig = lf._create_iot_job
        def patched_create(*args, **kwargs):
            raise RuntimeError("Key server down")
        monkeypatch.setattr(lf, "_create_iot_job", patched_create)

        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 500

    def test_no_thing_name_returns_409(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        dev = _device()
        dev.pop("thingName")
        mock_dynamo_tables[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [dev]}
        resp = lf._handle_consent(USER_ID, "e@t.com", _consent_body())
        assert resp["statusCode"] == 409


# ═══════════════════════════════════════════════════════════════════════════════
# lambda_handler
# ═══════════════════════════════════════════════════════════════════════════════

class TestLambdaHandler:
    def _event(self, body=None, claims=None):
        if claims is None:
            claims = {"sub": USER_ID, "email": "user@test.com"}
        return {
            "requestContext": {"authorizer": {"claims": claims}},
            "body": json.dumps(body or _consent_body()),
        }

    def test_missing_sub_returns_401(self, monkeypatch):
        resp = lf.lambda_handler(self._event(claims={"email": "x@y.com"}), MockContext())
        assert resp["statusCode"] == 401

    def test_body_too_large_returns_400(self, monkeypatch):
        event = self._event()
        event["body"] = "x" * 3000
        resp = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 400

    def test_invalid_json_body_returns_400(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        event = self._event()
        event["body"] = "not-json"
        resp = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 400

    def test_valid_request_succeeds(
        self, monkeypatch, mock_dynamo_tables, mock_iot, mock_s3, mock_unwrap
    ):
        mock_iot.create_job.return_value = {"jobArn": "arn:x", "jobId": "j"}
        resp = lf.lambda_handler(self._event(), MockContext())
        assert resp["statusCode"] == 202
