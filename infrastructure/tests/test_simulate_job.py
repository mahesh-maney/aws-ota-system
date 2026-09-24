"""
Test suite for digilux_ota_dev_simulate_job

Coverage:
  POST (create)  — success: creates IoT Job, sets pendingJobId, returns 201
                 — device not found → 404
                 — device has no thingName → 409
                 — device has no package name → 409
                 — device already has pending job → 409
                 — no active package found → 404
                 — pinned version not ACTIVE → 404
                 — pinned version not found → 404
                 — package missing encS3Key → 500
                 — missing deviceId → 400
                 — invalid JSON body → 400
  DELETE (cancel) — success: cancels dev job, clears pendingJobId, returns 200
                  — device not found → 404
                  — device has no pending job → 404
                  — active job is a production job → 409 (refuses to cancel)
                  — IoT job already gone (ResourceNotFoundException) → 200 (idempotent)
                  — iot.cancel_job fails → 500
  Job ID format  — job IDs start with "digilux-ota-dev-"
  Presign expiry — correct tier selected by artifact size

Run:
  pip install pytest boto3
  pytest infrastructure/tests/test_simulate_job.py -v
"""

import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
import importlib
import importlib.util

os.environ.setdefault("REGION", "ap-south-1")  # required at import time

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_dev_simulate_job", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_simulate_job", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ──────────────────────────────────────────────────────────────────
DEVICE_ID  = "edb39bba-baf1-4700-968c-a42228e53aa0"
PKG_NAME   = "HomeAssistantUtility"
VERSION    = "4.5.0"
THING_NAME = "digilux-thing-001"
MAC        = "irjof5RLuQcVv2tvEVdSilZbm1Wj7J4AGWw69ZJ1e0r1AN7fM4W1NQ=="

DEV_JOB_ID  = "digilux-ota-dev-HomeAssistantUtility-4-5-0-1234567890"
PROD_JOB_ID = "digilux-ota-HomeAssistantUtility-4-5-0-1234567890"


def _device(pending_job=None) -> dict:
    d = {
        "deviceId":   DEVICE_ID,
        "macAddress": MAC,
        "thingName":  THING_NAME,
        "package":    {"name": PKG_NAME},
    }
    if pending_job:
        d["pendingJobId"] = pending_job
    return d


def _package(status="ACTIVE", activated=True, enc_s3_key="enc/uuid.enc") -> dict:
    p = {
        "packageName":  PKG_NAME,
        "version":      VERSION,
        "status":       status,
        "activated":    activated,
        "sha256":       "abc" * 20,
        "signature":    "sig==",
        "artifactSize": 512,
        "deviceType":   "Network_controller_firmware",
    }
    if enc_s3_key:
        p["encS3Key"] = enc_s3_key
    return p


class MockContext:
    aws_request_id = "test-req-001"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    monkeypatch.setattr(lf, "REGION",           "ap-south-1")
    monkeypatch.setattr(lf, "ACCOUNT_ID",        "123456789012")
    monkeypatch.setattr(lf, "DEVICE_TABLE",      "digilux_device_data")
    monkeypatch.setattr(lf, "PACKAGES_TABLE",    "digilux_ota_packages")
    monkeypatch.setattr(lf, "ARTIFACT_BUCKET",   "digilux-ota-artifacts")


@pytest.fixture
def mock_dynamo(monkeypatch):
    device_table  = MagicMock()
    package_table = MagicMock()

    device_table.query.return_value    = {"Items": [_device()]}
    package_table.get_item.return_value = {"Item": None}
    package_table.query.return_value    = {"Items": [_package()]}
    device_table.update_item.return_value = {}

    tables = {
        lf.DEVICE_TABLE:   device_table,
        lf.PACKAGES_TABLE: package_table,
    }
    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda n: tables.get(n, MagicMock())
    monkeypatch.setattr(lf, "dynamodb", dynamo)
    return tables


@pytest.fixture
def mock_iot(monkeypatch):
    iot = MagicMock()
    iot.create_job.return_value = {"jobArn": "arn:aws:iot:x:y:job/test"}
    # Make ResourceNotFoundException a real exception class so except clauses work
    iot.exceptions.ResourceNotFoundException = type(
        "ResourceNotFoundException", (Exception,), {}
    )
    monkeypatch.setattr(lf, "iot", iot)
    return iot


@pytest.fixture
def mock_s3(monkeypatch):
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.test/presigned"
    monkeypatch.setattr(lf, "s3", s3)
    return s3


def _post_event(body: dict) -> dict:
    return {"httpMethod": "POST", "body": json.dumps(body)}


def _delete_event(body: dict) -> dict:
    return {"httpMethod": "DELETE", "body": json.dumps(body)}


# ═══════════════════════════════════════════════════════════════════════════════
# POST — create simulate job
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostCreateJob:
    def test_success_returns_201(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 201

    def test_success_response_contains_required_fields(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        body = json.loads(resp["body"])
        for field in ("jobId", "deviceId", "packageName", "version", "status", "presignedUrl"):
            assert field in body, f"Missing field: {field}"

    def test_job_id_starts_with_dev_prefix(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        job_id = json.loads(resp["body"])["jobId"]
        assert job_id.startswith("digilux-ota-dev-"), \
            f"Job ID must start with 'digilux-ota-dev-', got: {job_id}"

    def test_iot_create_job_called(self, mock_dynamo, mock_iot, mock_s3):
        lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        mock_iot.create_job.assert_called_once()

    def test_pending_job_id_set_in_dynamo(self, mock_dynamo, mock_iot, mock_s3):
        lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        update_calls = mock_dynamo[lf.DEVICE_TABLE].update_item.call_args_list
        assert len(update_calls) == 1
        vals = update_calls[0].kwargs["ExpressionAttributeValues"]
        assert ":j" in vals
        assert vals[":j"].startswith("digilux-ota-dev-")

    def test_pinned_version_used_when_provided(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package()}
        resp = lf.lambda_handler(
            _post_event({"deviceId": DEVICE_ID, "version": VERSION}), MockContext()
        )
        assert resp["statusCode"] == 201
        assert json.loads(resp["body"])["version"] == VERSION

    def test_presigned_url_generated(self, mock_dynamo, mock_iot, mock_s3):
        lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        mock_s3.generate_presigned_url.assert_called_once()

    def test_device_not_found_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 404

    def test_no_thing_name_returns_409(self, mock_dynamo, mock_iot, mock_s3):
        dev = _device()
        dev.pop("thingName")
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {"Items": [dev]}
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 409

    def test_no_package_name_returns_409(self, mock_dynamo, mock_iot, mock_s3):
        dev = _device()
        dev.pop("package")
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {"Items": [dev]}
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 409

    def test_existing_pending_job_returns_409(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert "pendingJobId" in body

    def test_no_active_package_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 404

    def test_pinned_version_not_active_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {
            "Item": _package(status="PENDING")
        }
        resp = lf.lambda_handler(
            _post_event({"deviceId": DEVICE_ID, "version": VERSION}), MockContext()
        )
        assert resp["statusCode"] == 404

    def test_pinned_version_not_found_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": None}
        resp = lf.lambda_handler(
            _post_event({"deviceId": DEVICE_ID, "version": "9.9.9"}), MockContext()
        )
        assert resp["statusCode"] == 404

    def test_package_missing_enc_key_returns_500(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {
            "Items": [_package(enc_s3_key=None)]
        }
        resp = lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 500

    def test_missing_device_id_returns_400(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(_post_event({}), MockContext())
        assert resp["statusCode"] == 400

    def test_invalid_json_body_returns_400(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(
            {"httpMethod": "POST", "body": "not-json"}, MockContext()
        )
        assert resp["statusCode"] == 400

    def test_empty_body_returns_400(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(
            {"httpMethod": "POST", "body": None}, MockContext()
        )
        assert resp["statusCode"] == 400

    def test_job_doc_contains_operation_type(self, mock_dynamo, mock_iot, mock_s3):
        captured = {}
        mock_iot.create_job.side_effect = lambda **kw: (
            captured.update({"doc": json.loads(kw["document"])}) or {}
        )
        lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        assert "operationType" in captured.get("doc", {})

    def test_job_doc_contains_artifact_section(self, mock_dynamo, mock_iot, mock_s3):
        captured = {}
        mock_iot.create_job.side_effect = lambda **kw: (
            captured.update({"doc": json.loads(kw["document"])}) or {}
        )
        lf.lambda_handler(_post_event({"deviceId": DEVICE_ID}), MockContext())
        doc = captured.get("doc", {})
        assert "artifact" in doc
        for field in ("presignedUrl", "sha256", "signature", "size"):
            assert field in doc["artifact"], f"Missing artifact field: {field}"


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE — cancel simulate job
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeleteCancelJob:
    def test_success_returns_200(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 200

    def test_success_response_contains_required_fields(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        body = json.loads(resp["body"])
        assert body["cancelled"] is True
        assert "jobId" in body
        assert "deviceId" in body

    def test_iot_cancel_job_called_with_force(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        mock_iot.cancel_job.assert_called_once_with(jobId=DEV_JOB_ID, force=True)

    def test_pending_job_id_cleared_in_dynamo(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        update_calls = mock_dynamo[lf.DEVICE_TABLE].update_item.call_args_list
        assert len(update_calls) == 1
        expr = update_calls[0].kwargs["UpdateExpression"]
        assert "REMOVE pendingJobId" in expr

    def test_production_job_returns_409(self, mock_dynamo, mock_iot, mock_s3):
        """Cannot cancel a production job via simulate-job endpoint."""
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=PROD_JOB_ID)]
        }
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert "pendingJobId" in body
        mock_iot.cancel_job.assert_not_called()

    def test_no_pending_job_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {"Items": [_device()]}
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 404

    def test_device_not_found_returns_404(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 404

    def test_iot_job_already_gone_still_returns_200(self, mock_dynamo, mock_iot, mock_s3):
        """If IoT Job is already gone, cancel is idempotent — still returns 200."""
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        mock_iot.cancel_job.side_effect = mock_iot.exceptions.ResourceNotFoundException()
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 200
        # pendingJobId must still be cleared
        update_calls = mock_dynamo[lf.DEVICE_TABLE].update_item.call_args_list
        assert len(update_calls) == 1

    def test_iot_cancel_fails_returns_500(self, mock_dynamo, mock_iot, mock_s3):
        mock_dynamo[lf.DEVICE_TABLE].query.return_value = {
            "Items": [_device(pending_job=DEV_JOB_ID)]
        }
        mock_iot.cancel_job.side_effect = Exception("IoT error")
        resp = lf.lambda_handler(_delete_event({"deviceId": DEVICE_ID}), MockContext())
        assert resp["statusCode"] == 500

    def test_missing_device_id_returns_400(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(_delete_event({}), MockContext())
        assert resp["statusCode"] == 400

    def test_invalid_json_returns_400(self, mock_dynamo, mock_iot, mock_s3):
        resp = lf.lambda_handler(
            {"httpMethod": "DELETE", "body": "not-json"}, MockContext()
        )
        assert resp["statusCode"] == 400


# ═══════════════════════════════════════════════════════════════════════════════
# Presign expiry tiers
# ═══════════════════════════════════════════════════════════════════════════════

class TestPresignExpiry:
    def test_small_file_tier1(self):
        assert lf._presign_expiry(10 * 1024 * 1024) == lf._TIER1_SEC

    def test_medium_file_tier2(self):
        assert lf._presign_expiry(100 * 1024 * 1024) == lf._TIER2_SEC

    def test_large_file_tier3(self):
        assert lf._presign_expiry(300 * 1024 * 1024) == lf._TIER3_SEC

    def test_very_large_file_tier4(self):
        assert lf._presign_expiry(600 * 1024 * 1024) == lf._TIER4_SEC

    def test_exactly_at_tier1_boundary(self):
        assert lf._presign_expiry(lf._TIER1_MAX_MB * 1024 * 1024) == lf._TIER1_SEC

    def test_just_over_tier1_boundary(self):
        assert lf._presign_expiry(lf._TIER1_MAX_MB * 1024 * 1024 + 1) == lf._TIER2_SEC
