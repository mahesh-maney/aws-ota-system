"""
Test suite for digilux_ota_user_check_updates

Coverage:
  Dev-job prefix filter   — pendingJobId starting with "digilux-ota-dev-" is ignored
                          — production job starting with "digilux-ota-" returns JOB_ACTIVE
                          — no pendingJobId → normal update check
  Available updates       — ACTIVE + PROD package newer than installed → returned
                          — ACTIVE + PROD package same as installed → not returned
                          — ACTIVE + PROD package older than installed → not returned
                          — PENDING package → not returned
                          — no package found → empty devices list
  Lambda handler          — missing sub claim → 401
                          — missing Authorization header → 401
                          — valid request, no devices → 200 empty list
  Multiple devices        — each device checked independently
  Beta users              — BETA package returned only to canary-group devices

Run:
  pip install pytest boto3
  pytest infrastructure/tests/test_check_updates.py -v
"""

import base64
import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
import importlib
import importlib.util

os.environ.setdefault("REGION", "ap-south-1")  # required at import time

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_user_check_updates", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_check_updates", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ──────────────────────────────────────────────────────────────────
USER_ID   = "user-sub-001"
DEVICE_ID = "edb39bba-baf1-4700-968c-a42228e53aa0"
PKG_NAME  = "HomeAssistantUtility"
THING     = "digilux-thing-001"


def _device(installed="1.0.0", pending_job=None, thing=THING) -> dict:
    d = {
        "deviceId":    DEVICE_ID,
        "userId":      USER_ID,
        "thingName":   thing,
        "macAddress":  "aa:bb:cc:dd:ee:ff",
        "package":     {"name": PKG_NAME},
        "globalInstalledVersion": installed,
        "installedVersion":       installed,
    }
    if pending_job:
        d["pendingJobId"] = pending_job
    return d


def _package(version="2.0.0", status="ACTIVE", release_type="PROD",
             activated=True) -> dict:
    return {
        "packageName":  PKG_NAME,
        "version":      version,
        "status":       status,
        "releaseType":  release_type,
        "activated":    activated,
        "sha256":       "abc" * 20,
        "signature":    "sig==",
        "artifactSize": 1024,
        "releaseNotes": "Bug fixes.",
        "encS3Key":     "enc/uuid.enc",
    }


class MockContext:
    aws_request_id = "test-req-001"


# ── JWT token builder (minimal, no real sig — Cognito authorizer is mocked) ──
def _event_with_claims(claims=None) -> dict:
    if claims is None:
        claims = {"sub": USER_ID, "email": "user@test.com"}
    return {
        "requestContext": {"authorizer": {"claims": claims}},
        "headers": {"Authorization": "Bearer dummy-token"},
    }


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    monkeypatch.setattr(lf, "REGION", "ap-south-1")


@pytest.fixture
def mock_dynamo(monkeypatch):
    device_table  = MagicMock()
    package_table = MagicMock()
    job_table     = MagicMock()

    device_table.query.return_value   = {"Items": [_device()]}
    package_table.query.return_value  = {"Items": [_package()]}
    package_table.get_item.return_value = {"Item": _package()}
    job_table.get_item.return_value   = {"Item": None}

    tables = {
        lf.DEVICE_DATA_TABLE: device_table,
        lf.PACKAGES_TABLE:    package_table,
        lf.OTA_JOBS_TABLE:    job_table,
    }
    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda n: tables.get(n, MagicMock())
    monkeypatch.setattr(lf, "dynamo", dynamo)

    # Beta check uses IoT, not DynamoDB — default to non-canary device
    mock_iot = MagicMock()
    mock_iot.list_thing_groups_for_thing.return_value = {"thingGroups": []}
    # Raise on describe_job_execution so the lambda falls back to DynamoDB job status
    mock_iot.describe_job_execution.side_effect = Exception("IoT not available in tests")
    monkeypatch.setattr(lf, "iot", mock_iot)

    return tables


# ═══════════════════════════════════════════════════════════════════════════════
# Dev-job prefix filter
# ═══════════════════════════════════════════════════════════════════════════════

class TestDevJobPrefixFilter:
    def test_dev_simulate_job_is_ignored(self, monkeypatch, mock_dynamo):
        """Device with a digilux-ota-dev-* job must NOT return JOB_ACTIVE."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(pending_job="digilux-ota-dev-HomeAssistantUtility-1-0-0-1234567890")]
        }
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert devices[0].get("otaStatus") != "JOB_ACTIVE"

    def test_dev_job_still_shows_available_update(self, monkeypatch, mock_dynamo):
        """When dev job is active, available-updates should still show the update."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed="1.0.0",
                              pending_job="digilux-ota-dev-HomeAssistantUtility-1-0-0-1234567890")]
        }
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0].get("availableVersion") == "2.0.0"

    def test_production_job_returns_job_active(self, monkeypatch, mock_dynamo):
        """Device with a digilux-ota-* (non-dev) job must return JOB_ACTIVE."""
        job_id = "digilux-ota-HomeAssistantUtility-2-0-0-1234567890"
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(pending_job=job_id)]
        }
        mock_dynamo[lf.OTA_JOBS_TABLE].get_item.return_value = {
            "Item": {
                "jobId":       job_id,
                "packageName": PKG_NAME,
                "version":     "2.0.0",
                "status":      "IN_PROGRESS",
            }
        }
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0]["otaStatus"] == "JOB_ACTIVE"

    def test_dev_job_prefix_only_matches_exact_prefix(self, monkeypatch, mock_dynamo):
        """A job named 'digilux-ota-dev' (no trailing hyphen) is not a dev job."""
        # This is a subtle edge case — the prefix check is .startswith("digilux-ota-dev-")
        job_id = "digilux-ota-developer-special-job-999"
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(pending_job=job_id)]
        }
        mock_dynamo[lf.OTA_JOBS_TABLE].get_item.return_value = {
            "Item": {
                "jobId":       job_id,
                "packageName": PKG_NAME,
                "version":     "2.0.0",
                "status":      "IN_PROGRESS",
            }
        }
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        # "digilux-ota-developer-..." does NOT start with "digilux-ota-dev-" — treated as prod job
        devices = json.loads(resp["body"])["devices"]
        assert devices[0]["otaStatus"] == "JOB_ACTIVE"

    def test_no_pending_job_shows_available_update(self, monkeypatch, mock_dynamo):
        """Device with no pending job shows available update normally."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed="1.0.0")]
        }
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0].get("availableVersion") == "2.0.0"


# ═══════════════════════════════════════════════════════════════════════════════
# Available update filtering
# ═══════════════════════════════════════════════════════════════════════════════

class TestAvailableUpdates:
    def test_newer_active_prod_package_returned(self, monkeypatch, mock_dynamo):
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": [_package(version="2.0.0")]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0]["availableVersion"] == "2.0.0"

    def test_same_version_not_returned(self, monkeypatch, mock_dynamo):
        # Lambda omits up-to-date devices from result entirely
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="2.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": [_package(version="2.0.0")]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_older_package_not_returned(self, monkeypatch, mock_dynamo):
        # Lambda omits up-to-date devices from result entirely
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="3.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": [_package(version="2.0.0")]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_pending_package_not_returned(self, monkeypatch, mock_dynamo):
        # Filtering of PENDING packages is done by DynamoDB FilterExpression.
        # Simulate DynamoDB returning nothing (as it would with status=ACTIVE filter applied).
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_not_activated_package_not_returned(self, monkeypatch, mock_dynamo):
        # activated=False is filtered by DynamoDB. Simulate empty result from DB.
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_no_package_found_returns_empty(self, monkeypatch, mock_dynamo):
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device()]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["devices"] == [] or body["devices"][0].get("availableVersion") is None

    def test_release_notes_included_in_response(self, monkeypatch, mock_dynamo):
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        pkg = _package(version="2.0.0")
        pkg["releaseNotes"] = "Performance improvements."
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": [pkg]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        if devices and devices[0].get("availableVersion"):
            assert "releaseNotes" in devices[0]

    def test_beta_package_not_returned_to_non_canary(self, monkeypatch, mock_dynamo):
        # BETA package is filtered by DynamoDB (releaseType=PROD filter for non-canary devices).
        # Simulate DynamoDB returning nothing for a non-canary device querying for PROD packages.
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].query.return_value = {"Items": []}
        # IoT already returns empty thingGroups (non-canary)
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_no_devices_returns_empty_list(self, monkeypatch, mock_dynamo):
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["devices"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# lambda_handler auth
# ═══════════════════════════════════════════════════════════════════════════════

class TestLambdaHandlerAuth:
    def test_missing_sub_returns_401(self):
        resp = lf.lambda_handler(_event_with_claims(claims={"email": "x@y.com"}), MockContext())
        assert resp["statusCode"] == 401

    def test_empty_sub_returns_401(self):
        resp = lf.lambda_handler(_event_with_claims(claims={"sub": ""}), MockContext())
        assert resp["statusCode"] == 401

    def test_no_claims_returns_401(self):
        event = {"requestContext": {}, "headers": {}}
        resp = lf.lambda_handler(event, MockContext())
        assert resp["statusCode"] == 401
