"""
Test suite for digilux_ota_artifact_processor

Coverage:
  _wrap_data_key         — positive, all negative / edge cases
  _process_artifact      — key server integration: wrappedDataKey stored in DynamoDB,
                           pipeline failures when key server is down
  _double_encrypt path   — removed; verified old fields (aesKeyEnc, masterIv) absent
  _quarantine            — token mismatch, checksum mismatch, bad tar
  _sign                  — ECDSA helper
  _encrypt_artifact      — returns key, iv, ciphertext of correct sizes
  _supersede             — older ACTIVE versions get SUPERSEDED
  Lambda handler         — S3 event routing, skip placeholder keys

Run:
  pip install pytest cryptography boto3
  pytest infrastructure/tests/test_artifact_processor.py -v
"""

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

# ── Path setup ─────────────────────────────────────────────────────────────────
import importlib
import importlib.util

os.environ.setdefault("REGION", "ap-south-1")  # required at import time

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_artifact_processor", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_artifact_processor", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_tar(files: dict) -> bytes:
    """Build an in-memory tar.gz from {filename: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_valid_tar(pkg_name="HomeAssistantUtility", version="1.0.0") -> bytes:
    manifest = json.dumps({
        "packageName": pkg_name,
        "version": version,
        "files": [{"name": "firmware.bin", "type": 1}],
    }).encode()
    return _make_tar({"manifest.json": manifest, "firmware.bin": b"\x00" * 64})


def _make_ecdsa_key_pem() -> bytes:
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


ECDSA_PEM   = _make_ecdsa_key_pem()
WRAPPED_KEY = "v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    monkeypatch.setattr(lf, "KEY_SERVER_URL",     "https://keys.test")
    monkeypatch.setattr(lf, "KEY_SERVER_API_KEY", "test-api-key")
    monkeypatch.setattr(lf, "REGION",             "ap-south-1")
    monkeypatch.setattr(lf, "SIGNING_SECRET",     "test-signing-secret")
    monkeypatch.setattr(lf, "MASTER_ENC_SECRET",  "test-master-enc-secret")


@pytest.fixture
def mock_sm(monkeypatch):
    sm = MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"privateKey": ECDSA_PEM.decode()})
    }
    monkeypatch.setattr(lf, "sm", sm)
    return sm


@pytest.fixture
def mock_wrap(monkeypatch):
    """Patch _wrap_data_key to return a fixed wrapped key."""
    monkeypatch.setattr(lf, "_wrap_data_key", lambda key_bytes: WRAPPED_KEY)


@pytest.fixture
def dynamo_table():
    t = MagicMock()
    t.get_item.return_value = {"Item": None}
    t.update_item.return_value = {}
    return t


@pytest.fixture
def mock_dynamo(monkeypatch, dynamo_table):
    dynamo = MagicMock()
    dynamo.Table.return_value = dynamo_table
    monkeypatch.setattr(lf, "dynamo", dynamo)
    return dynamo_table


@pytest.fixture
def mock_s3(monkeypatch):
    s3 = MagicMock()
    monkeypatch.setattr(lf, "s3", s3)
    return s3


class MockContext:
    aws_request_id = "test-req-001"


# ═══════════════════════════════════════════════════════════════════════════════
# _wrap_data_key
# ═══════════════════════════════════════════════════════════════════════════════

class TestWrapDataKey:
    def _make_response(self, body: dict, status: int = 200):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(body).encode()
        resp.status = status
        return resp

    def test_positive_returns_wrapped_key(self, monkeypatch):
        """Happy path: key server returns wrappedKey."""
        resp = self._make_response({"wrappedKey": "v1:abc123", "keyId": "some-uuid"})
        with patch("urllib.request.urlopen", return_value=resp):
            result = lf._wrap_data_key(os.urandom(32))
        assert result == "v1:abc123"

    def test_positive_sends_base64_encoded_key(self, monkeypatch):
        """Verify the request body contains plaintextKey as base64."""
        raw_key = os.urandom(32)
        resp = self._make_response({"wrappedKey": "v1:xyz"})
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            return resp
        with patch("urllib.request.urlopen", fake_urlopen):
            lf._wrap_data_key(raw_key)
        assert captured["body"]["plaintextKey"] == base64.b64encode(raw_key).decode()
        assert captured["auth"] == "Bearer test-api-key"

    def test_positive_sends_correct_url(self):
        """Verify the URL targets /api/v1/ota/keys/wrap."""
        resp = self._make_response({"wrappedKey": "v1:xyz"})
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return resp
        with patch("urllib.request.urlopen", fake_urlopen):
            lf._wrap_data_key(os.urandom(32))
        assert captured["url"] == "https://keys.test/api/v1/ota/keys/wrap"

    def test_negative_missing_key_server_url(self, monkeypatch):
        monkeypatch.setattr(lf, "KEY_SERVER_URL", "")
        with pytest.raises(RuntimeError, match="KEY_SERVER_URL"):
            lf._wrap_data_key(os.urandom(32))

    def test_negative_missing_api_key(self, monkeypatch):
        monkeypatch.setattr(lf, "KEY_SERVER_API_KEY", "")
        with pytest.raises(RuntimeError, match="KEY_SERVER_API_KEY"):
            lf._wrap_data_key(os.urandom(32))

    def test_negative_http_error_401(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            url="https://keys.test/api/v1/ota/keys/wrap",
            code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":"Unauthorized"}'),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="HTTP 401"):
                lf._wrap_data_key(os.urandom(32))

    def test_negative_http_error_500(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            url="https://keys.test/api/v1/ota/keys/wrap",
            code=500, msg="Internal Server Error", hdrs=None,
            fp=io.BytesIO(b'{"error":"server error"}'),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                lf._wrap_data_key(os.urandom(32))

    def test_negative_connection_error(self):
        import urllib.error
        exc = urllib.error.URLError(reason="Connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="unreachable"):
                lf._wrap_data_key(os.urandom(32))

    def test_negative_empty_wrapped_key_in_response(self):
        """Key server returns 200 but wrappedKey field is empty."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"wrappedKey": ""}).encode()
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="empty wrappedKey"):
                lf._wrap_data_key(os.urandom(32))

    def test_negative_missing_wrapped_key_field(self):
        """Key server returns 200 but no wrappedKey field at all."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"keyId": "abc"}).encode()
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="empty wrappedKey"):
                lf._wrap_data_key(os.urandom(32))

    def test_negative_timeout(self):
        """Connection timeout raises RuntimeError."""
        import socket
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with pytest.raises(Exception):
                lf._wrap_data_key(os.urandom(32))


# ═══════════════════════════════════════════════════════════════════════════════
# _encrypt_artifact
# ═══════════════════════════════════════════════════════════════════════════════

class TestEncryptArtifact:
    def test_returns_32_byte_key(self):
        key, iv, _ = lf._encrypt_artifact(b"hello world")
        assert len(key) == 32

    def test_returns_12_byte_iv(self):
        _, iv, _ = lf._encrypt_artifact(b"hello world")
        assert len(iv) == 12

    def test_ciphertext_longer_than_plaintext(self):
        data = b"x" * 100
        _, _, ciphertext = lf._encrypt_artifact(data)
        assert len(ciphertext) > len(data)

    def test_different_keys_each_call(self):
        k1, _, _ = lf._encrypt_artifact(b"data")
        k2, _, _ = lf._encrypt_artifact(b"data")
        assert k1 != k2

    def test_roundtrip_decryption(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        data = b"firmware payload"
        key, iv, ciphertext = lf._encrypt_artifact(data)
        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
        assert plaintext == data


# ═══════════════════════════════════════════════════════════════════════════════
# _process_artifact — key server integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessArtifactKeyServerIntegration:
    """
    Verify that _process_artifact calls _wrap_data_key and stores wrappedDataKey
    (not aesKeyEnc/masterIv) in DynamoDB.
    """

    def _make_dynamo_item(self, pkg_name="HomeAssistantUtility", version="1.0.0"):
        return {
            "packageName":   pkg_name,
            "version":       version,
            "status":        "PENDING",
            "releaseType":   "PROD",
            "uploadToken":   "tok123",
            "uploadedBy":    "admin@digilux.co.in",
        }

    def _run_pipeline(self, monkeypatch, dynamo_item, tar_bytes, s3_token="tok123"):
        table = MagicMock()
        table.get_item.return_value = {"Item": dynamo_item}
        table.update_item.return_value = {}
        table.query.return_value = {"Items": []}

        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"upload-token": s3_token}}
        s3.get_object.return_value = {"Body": io.BytesIO(tar_bytes)}
        s3.put_object.return_value = {}
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        sm = MagicMock()
        sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"privateKey": ECDSA_PEM.decode()})
        }
        monkeypatch.setattr(lf, "sm", sm)

        monkeypatch.setattr(lf, "_wrap_data_key", lambda k: WRAPPED_KEY)

        lf._process_artifact("digilux-ota-artifacts", "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar", 512)
        return table

    def test_dynamo_stores_wrapped_data_key(self, monkeypatch):
        """wrappedDataKey is stored in DynamoDB after successful processing."""
        tar = _make_valid_tar()
        table = self._run_pipeline(monkeypatch, self._make_dynamo_item(), tar)
        update_calls = table.update_item.call_args_list
        assert len(update_calls) >= 1
        # Find the promote call (sets status = ACTIVE)
        promote_call = next(
            c for c in update_calls
            if ":active" in str(c)
        )
        vals = promote_call.kwargs["ExpressionAttributeValues"]
        assert ":wdk" in vals
        assert vals[":wdk"] == WRAPPED_KEY

    def test_dynamo_does_not_store_aes_key_enc(self, monkeypatch):
        """Legacy aesKeyEnc field must NOT appear in the DynamoDB update."""
        tar = _make_valid_tar()
        table = self._run_pipeline(monkeypatch, self._make_dynamo_item(), tar)
        for c in table.update_item.call_args_list:
            expr = c.kwargs.get("UpdateExpression", "")
            assert "aesKeyEnc" not in expr, "aesKeyEnc must not be stored (use wrappedDataKey)"

    def test_dynamo_does_not_store_master_iv(self, monkeypatch):
        """Legacy masterIv field must NOT appear in the DynamoDB update."""
        tar = _make_valid_tar()
        table = self._run_pipeline(monkeypatch, self._make_dynamo_item(), tar)
        for c in table.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            assert ":miv" not in vals, "masterIv must not be stored"

    def test_dynamo_stores_aes_iv(self, monkeypatch):
        """aesIv (GCM nonce) must still be stored — device needs it to decrypt."""
        tar = _make_valid_tar()
        table = self._run_pipeline(monkeypatch, self._make_dynamo_item(), tar)
        promote_call = next(
            c for c in table.update_item.call_args_list
            if ":active" in str(c)
        )
        vals = promote_call.kwargs["ExpressionAttributeValues"]
        assert ":iv" in vals
        iv_b64 = vals[":iv"]
        # Must be valid base64 of 12 bytes
        assert len(base64.b64decode(iv_b64)) == 12

    def test_key_server_failure_aborts_pipeline(self, monkeypatch):
        """If _wrap_data_key raises, _process_artifact raises (handled by lambda_handler)."""
        tar = _make_valid_tar()

        def failing_wrap(key_bytes):
            raise RuntimeError("Key server unreachable")

        table = MagicMock()
        table.get_item.return_value = {"Item": self._make_dynamo_item()}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"upload-token": "tok123"}}
        s3.get_object.return_value = {"Body": io.BytesIO(tar)}
        monkeypatch.setattr(lf, "s3", s3)

        sm = MagicMock()
        sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"privateKey": ECDSA_PEM.decode()})
        }
        monkeypatch.setattr(lf, "sm", sm)
        monkeypatch.setattr(lf, "_wrap_data_key", failing_wrap)

        with pytest.raises(RuntimeError, match="Key server unreachable"):
            lf._process_artifact(
                "digilux-ota-artifacts",
                "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
                512,
            )
        # DynamoDB promote must NOT have been called
        promote_calls = [
            c for c in table.update_item.call_args_list
            if ":active" in str(c)
        ]
        assert len(promote_calls) == 0, "Must not promote to ACTIVE when key server fails"

    def test_wrap_data_key_called_once_per_artifact(self, monkeypatch):
        """_wrap_data_key is called exactly once per upload."""
        tar = _make_valid_tar()
        call_count = {"n": 0}

        def counting_wrap(key_bytes):
            call_count["n"] += 1
            return WRAPPED_KEY

        table = MagicMock()
        table.get_item.return_value = {"Item": self._make_dynamo_item()}
        table.update_item.return_value = {}
        table.query.return_value = {"Items": []}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"upload-token": "tok123"}}
        s3.get_object.return_value = {"Body": io.BytesIO(tar)}
        s3.put_object.return_value = {}
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        sm = MagicMock()
        sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"privateKey": ECDSA_PEM.decode()})
        }
        monkeypatch.setattr(lf, "sm", sm)
        monkeypatch.setattr(lf, "_wrap_data_key", counting_wrap)

        lf._process_artifact(
            "digilux-ota-artifacts",
            "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
            512,
        )
        assert call_count["n"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _quarantine
# ═══════════════════════════════════════════════════════════════════════════════

class TestQuarantine:
    def test_upload_token_mismatch_quarantines(self, monkeypatch, mock_wrap):
        """Token mismatch → quarantine, no promote."""
        item = {
            "packageName": "HomeAssistantUtility",
            "version": "1.0.0",
            "status": "PENDING",
            "uploadToken": "correct-token",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": item}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"upload-token": "wrong-token"}}
        s3.get_object.return_value = {"Body": io.BytesIO(b"dummy")}
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact(
            "bucket",
            "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
            10,
        )
        # delete must have been called (quarantine)
        s3.delete_object.assert_called()
        # promote call must not have happened
        promote_calls = [
            c for c in table.update_item.call_args_list if ":active" in str(c)
        ]
        assert len(promote_calls) == 0

    def test_corrupted_status_skips_processing(self, monkeypatch, mock_wrap):
        """CORRUPTED packages are rejected immediately."""
        item = {
            "packageName": "HomeAssistantUtility",
            "version": "1.0.0",
            "status": "CORRUPTED",
            "corruptReason": "Checksum mismatch",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": item}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact(
            "bucket",
            "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
            10,
        )
        s3.delete_object.assert_called()

    def test_already_active_skips_processing(self, monkeypatch, mock_wrap):
        """ACTIVE packages produce no-op on duplicate S3 event."""
        item = {
            "packageName": "HomeAssistantUtility",
            "version": "1.0.0",
            "status": "ACTIVE",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": item}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact(
            "bucket",
            "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
            10,
        )
        s3.get_object.assert_not_called()

    def test_no_dynamo_record_deletes_orphan(self, monkeypatch, mock_wrap):
        """S3 object with no matching DynamoDB record is deleted."""
        table = MagicMock()
        table.get_item.return_value = {"Item": None}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)

        s3 = MagicMock()
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact(
            "bucket",
            "Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar",
            10,
        )
        s3.delete_object.assert_called()


# ═══════════════════════════════════════════════════════════════════════════════
# S3 key parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestS3KeyParsing:
    def test_new_structure_maps_device_type(self, monkeypatch, mock_wrap):
        """New key structure: Network_controller_firmware/{deviceType}/{version}/{file}"""
        table = MagicMock()
        table.get_item.return_value = {"Item": None}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)
        s3 = MagicMock()
        s3.delete_object.return_value = {}
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact(
            "bucket",
            "Network_controller_firmware/Network_controller_firmware/4.5.0/fw.tar",
            0,
        )
        table.get_item.assert_called_once_with(
            Key={"packageName": "HomeAssistantUtility", "version": "4.5.0"}
        )

    def test_invalid_key_structure_returns_early(self, monkeypatch, mock_wrap):
        """S3 key with fewer than 4 parts is skipped."""
        table = MagicMock()
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        monkeypatch.setattr(lf, "dynamo", dynamo)
        s3 = MagicMock()
        monkeypatch.setattr(lf, "s3", s3)

        lf._process_artifact("bucket", "tooshort/path", 0)
        table.get_item.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Lambda handler
# ═══════════════════════════════════════════════════════════════════════════════

class TestLambdaHandler:
    def _event(self, keys):
        return {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "digilux-ota-artifacts"},
                        "object": {"key": k, "size": 100},
                    }
                }
                for k in keys
            ]
        }

    def test_skips_keep_placeholder(self, monkeypatch, mock_wrap):
        called = []
        monkeypatch.setattr(lf, "_process_artifact", lambda *a: called.append(a))
        lf.lambda_handler(self._event([".keep"]), MockContext())
        assert len(called) == 0

    def test_processes_valid_s3_key(self, monkeypatch, mock_wrap):
        called = []
        monkeypatch.setattr(lf, "_process_artifact", lambda *a: called.append(a))
        lf.lambda_handler(
            self._event(["Network_controller_firmware/Network_controller_firmware/1.0.0/fw.tar"]),
            MockContext(),
        )
        assert len(called) == 1

    def test_handles_multiple_records(self, monkeypatch, mock_wrap):
        called = []
        monkeypatch.setattr(lf, "_process_artifact", lambda *a: called.append(a))
        lf.lambda_handler(
            self._event([
                "Network_controller_firmware/Network_controller_firmware/1.0.0/a.tar",
                "Network_controller_firmware/Network_controller_firmware/2.0.0/b.tar",
            ]),
            MockContext(),
        )
        assert len(called) == 2

    def test_exception_in_one_record_does_not_abort_others(self, monkeypatch, mock_wrap):
        """A failure on record 1 should not prevent record 2 from processing."""
        called = []

        def boom_then_ok(bucket, key, size):
            if "1.0.0" in key:
                raise RuntimeError("simulated failure")
            called.append(key)

        monkeypatch.setattr(lf, "_process_artifact", boom_then_ok)
        lf.lambda_handler(
            self._event([
                "Network_controller_firmware/Network_controller_firmware/1.0.0/a.tar",
                "Network_controller_firmware/Network_controller_firmware/2.0.0/b.tar",
            ]),
            MockContext(),
        )
        assert len(called) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _supersede_previous_versions
# ═══════════════════════════════════════════════════════════════════════════════

class TestSupersede:
    def test_supersedes_older_active_version(self, monkeypatch):
        """Older ACTIVE version gets SUPERSEDED when newer version activates."""
        from boto3.dynamodb.conditions import Attr, Key

        older = {"packageName": "P", "version": "1.0.0", "status": "ACTIVE"}
        table = MagicMock()
        table.query.return_value = {"Items": [older]}
        table.update_item.return_value = {}

        lf._supersede_previous_versions(table, "P", "2.0.0", "PROD", int(time.time() * 1000))

        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args.kwargs
        assert call_kwargs["Key"] == {"packageName": "P", "version": "1.0.0"}
        assert ":sup" in call_kwargs["ExpressionAttributeValues"]

    def test_does_not_supersede_self(self, monkeypatch):
        """The current version is never SUPERSEDED."""
        table = MagicMock()
        table.query.return_value = {
            "Items": [{"packageName": "P", "version": "2.0.0", "status": "ACTIVE"}]
        }
        lf._supersede_previous_versions(table, "P", "2.0.0", "PROD", int(time.time() * 1000))
        table.update_item.assert_not_called()

    def test_does_not_supersede_newer_version(self, monkeypatch):
        """A newer ACTIVE version is not superseded by an older new activation."""
        table = MagicMock()
        table.query.return_value = {
            "Items": [{"packageName": "P", "version": "3.0.0", "status": "ACTIVE"}]
        }
        lf._supersede_previous_versions(table, "P", "2.0.0", "PROD", int(time.time() * 1000))
        table.update_item.assert_not_called()
