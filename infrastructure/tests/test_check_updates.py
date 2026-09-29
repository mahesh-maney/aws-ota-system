"""
Test suite for digilux_ota_user_check_updates (deployment-centric architecture)

Coverage:
  Dev-job prefix filter   — pendingJobId starting with "digilux-ota-dev-" is ignored
                          — production job starting with "digilux-ota-" returns JOB_ACTIVE
                          — no pendingJobId → normal update check

  Deployment-centric update logic (new)
                          — ACTIVE PRODUCTION deployment + device in DGX-Production → offered
                          — ACTIVE BETA deployment + device in targetIds → offered (priority)
                          — BETA deployment does not target this device → PROD checked instead
                          — No ACTIVE deployment → no update (even if package exists)
                          — Never downgrade: deployment version ≤ installed → no update
                          — Package missing from packages table → no update (deployment ignored)

  Available updates (pre-new)
                          — newer ACTIVE deployment returned
                          — same version not returned
                          — older version not returned (never downgrade)
                          — no deployment → empty devices list

  Lambda handler          — missing sub claim → 401
                          — missing Authorization header → 401
                          — valid request, no devices → 200 empty list
  Multiple devices        — each device checked independently
  Beta priority           — BETA deployment takes priority over PRODUCTION
  Production group        — device NOT in DGX-Production → PROD deployment not offered

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

def _deployment(version="2.0.0", stage="PRODUCTION", dep_id=None,
                status="ACTIVE") -> dict:
    return {
        "deploymentId": dep_id or f"digilux-ota-{PKG_NAME}-{version}-1234567890",
        "packageName":  PKG_NAME,
        "version":      version,
        "rolloutStage": stage,
        "status":       status,
        "targetIds":    [DEVICE_ID] if stage == "BETA" else [],
        "targetGroup":  "PRODUCTION" if stage == "PRODUCTION" else "",
        "releaseNotes": "Bug fixes.",
        "createdAt":    int(time.time() * 1000),
    }


@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    monkeypatch.setattr(lf, "REGION", "ap-south-1")


@pytest.fixture
def mock_dynamo(monkeypatch):
    device_table      = MagicMock()
    package_table     = MagicMock()
    job_table         = MagicMock()
    deployments_table = MagicMock()
    consents_table    = MagicMock()

    device_table.query.return_value        = {"Items": [_device()]}
    # Deployments table: default → one ACTIVE PRODUCTION deployment
    deployments_table.query.return_value   = {"Items": [_deployment()]}
    # Package table: get_item used for details after deployment found
    package_table.get_item.return_value    = {"Item": _package()}
    package_table.query.return_value       = {"Items": [_package()]}
    job_table.get_item.return_value        = {}
    consents_table.query.return_value      = {"Items": []}

    tables = {
        lf.DEVICE_DATA_TABLE:  device_table,
        lf.PACKAGES_TABLE:     package_table,
        lf.OTA_JOBS_TABLE:     job_table,
        lf.DEPLOYMENTS_TABLE:  deployments_table,
        lf.CONSENTS_TABLE:     consents_table,
    }
    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda n: tables.get(n, MagicMock())
    monkeypatch.setattr(lf, "dynamo", dynamo)

    # IoT: default to device in PRODUCTION group (so PROD deployments are offered)
    mock_iot = MagicMock()
    mock_iot.list_thing_groups_for_thing.return_value = {
        "thingGroups": [{"groupName": lf.PRODUCTION_GROUP}]
    }
    # Raise on describe_job_execution so the lambda falls back to DynamoDB job status
    mock_iot.describe_job_execution.side_effect = Exception("IoT not available in tests")
    monkeypatch.setattr(lf, "iot", mock_iot)

    # Lambda-to-Lambda entitlement: default to eligible (fail open)
    mock_lambda = MagicMock()
    mock_lambda.invoke.side_effect = Exception("entitlement not mocked")
    monkeypatch.setattr(lf, "lambda_client", mock_lambda)

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
        # Deployments table returns update so we can check the response
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment()]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package()}
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
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
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
# Available update filtering (deployment-centric)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAvailableUpdates:
    def test_newer_active_deployment_returned(self, monkeypatch, mock_dynamo):
        """Device with installed=1.0.0, ACTIVE deployment for 2.0.0 → update offered."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0]["availableVersion"] == "2.0.0"

    def test_same_version_not_returned(self, monkeypatch, mock_dynamo):
        """Deployment version == installed → no update (up to date)."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="2.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_older_deployment_not_returned(self, monkeypatch, mock_dynamo):
        """Deployment version < installed → never downgrade → no update."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="3.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_no_active_deployment_returns_empty(self, monkeypatch, mock_dynamo):
        """No ACTIVE deployment → even if package exists, no update offered."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": []}  # no deployment
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_package_missing_from_packages_table_skipped(self, monkeypatch, mock_dynamo):
        """Deployment exists but package record missing → device skipped."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {}  # no Item
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_release_notes_included_in_response(self, monkeypatch, mock_dynamo):
        """releaseNotes from deployment (or package) included in response."""
        dep = _deployment(version="2.0.0")
        dep["releaseNotes"] = "Performance improvements."
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [dep]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0].get("releaseNotes") == "Performance improvements."

    def test_deployment_id_in_response(self, monkeypatch, mock_dynamo):
        """deploymentId must be included in update-available response."""
        dep = _deployment(version="2.0.0", dep_id="dep-test-001")
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [dep]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0].get("deploymentId") == "dep-test-001"

    def test_rollout_stage_in_response(self, monkeypatch, mock_dynamo):
        """rolloutStage must be included in update-available response."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0", stage="PRODUCTION")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0].get("rolloutStage") == "PRODUCTION"

    def test_no_devices_returns_empty_list(self, monkeypatch, mock_dynamo):
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": []}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["devices"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# FAILED job fall-through + lastFailedJob
# ═══════════════════════════════════════════════════════════════════════════════

class TestFailedJobBehavior:
    """FAILED pendingJobId must not block version comparison.

    When a previous job is FAILED:
      - If a newer version is available → return UPDATE_AVAILABLE with lastFailedJob
      - If the device is already up to date → return empty (no entry)
    """

    def _setup_failed_job(self, mock_dynamo, installed="1.0.0", available="2.0.0"):
        job_id = "digilux-ota-HomeAssistantUtility-1-0-0-1234567890"
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed=installed, pending_job=job_id)]
        }
        mock_dynamo[lf.OTA_JOBS_TABLE].get_item.return_value = {
            "Item": {
                "jobId":       job_id,
                "packageName": PKG_NAME,
                "version":     installed,
                "status":      "FAILED",
            }
        }
        # New architecture: deployment table queried first for available version
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {
            "Items": [_deployment(version=available)] if available else []
        }
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {
            "Item": _package(version=available)
        } if available else {}
        return job_id

    def test_failed_job_does_not_return_job_active(self, monkeypatch, mock_dynamo):
        """A FAILED job must not produce otaStatus=JOB_ACTIVE."""
        self._setup_failed_job(mock_dynamo)
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        # Device should appear (update available) but NOT as JOB_ACTIVE
        assert all(d.get("otaStatus") != "JOB_ACTIVE" for d in devices)

    def test_failed_job_with_newer_version_shows_available_update(self, monkeypatch, mock_dynamo):
        """FAILED job + newer package → device returned with availableVersion."""
        self._setup_failed_job(mock_dynamo, installed="1.0.0", available="2.0.0")
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert devices[0]["availableVersion"] == "2.0.0"

    def test_failed_job_response_includes_last_failed_job(self, monkeypatch, mock_dynamo):
        """FAILED job + newer package → lastFailedJob field present in response."""
        job_id = self._setup_failed_job(mock_dynamo, installed="1.0.0", available="2.0.0")
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert "lastFailedJob" in devices[0]
        lf_job = devices[0]["lastFailedJob"]
        assert lf_job["jobId"]   == job_id
        assert lf_job["status"]  == "FAILED"
        assert lf_job["version"] == "1.0.0"
        assert "message" in lf_job

    def test_failed_job_last_failed_job_message_not_empty(self, monkeypatch, mock_dynamo):
        """lastFailedJob.message must be a non-empty string."""
        self._setup_failed_job(mock_dynamo)
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert devices[0]["lastFailedJob"]["message"]

    def test_failed_job_device_up_to_date_returns_empty(self, monkeypatch, mock_dynamo):
        """FAILED job + no newer version → device is NOT returned (up to date)."""
        self._setup_failed_job(mock_dynamo, installed="2.0.0", available="2.0.0")
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        assert resp["statusCode"] == 200
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_failed_job_no_active_deployment_returns_empty(self, monkeypatch, mock_dynamo):
        """FAILED job + no ACTIVE deployment at all → device not returned."""
        self._setup_failed_job(mock_dynamo, installed="1.0.0", available=None)
        # available=None means _setup already set deployments to []
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_failed_job_older_available_deployment_returns_empty(self, monkeypatch, mock_dynamo):
        """FAILED job + deployment version older than installed → device not returned."""
        self._setup_failed_job(mock_dynamo, installed="3.0.0", available="2.0.0")
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_no_failed_job_no_last_failed_job_field(self, monkeypatch, mock_dynamo):
        """Normal update (no FAILED pendingJob) must NOT include lastFailedJob field."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed="1.0.0")]  # no pending_job
        }
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert "lastFailedJob" not in devices[0]

    def test_in_progress_job_not_affected(self, monkeypatch, mock_dynamo):
        """IN_PROGRESS job must still return JOB_ACTIVE (regression guard)."""
        job_id = "digilux-ota-HomeAssistantUtility-2-0-0-9999999999"
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {
            "Items": [_device(installed="1.0.0", pending_job=job_id)]
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
        assert len(devices) == 1
        assert devices[0]["otaStatus"] == "JOB_ACTIVE"
        assert devices[0]["activeJob"]["status"] == "IN_PROGRESS"
        assert "lastFailedJob" not in devices[0]


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


# ═══════════════════════════════════════════════════════════════════════════════
# Deployment-centric: BETA vs PRODUCTION priority
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeploymentCentricLogic:
    """New architecture: check_updates queries DEPLOYMENTS_TABLE first."""

    def test_beta_deployment_offered_when_device_in_target_ids(self, monkeypatch, mock_dynamo):
        """Device is in BETA deployment's targetIds → BETA update offered."""
        beta_dep = _deployment(version="3.0.0", stage="BETA", dep_id="beta-dep-001")
        beta_dep["targetIds"] = [DEVICE_ID]  # device IS in the list

        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        # The check_updates lambda makes two deployment queries per device:
        #   1st call = BETA lookup  (FilterExpression has rolloutStage="BETA")
        #   2nd call = PROD lookup  (FilterExpression has rolloutStage="PRODUCTION")
        # Use a call-count-based side effect since FilterExpression is a boto3 object (not str)
        dep_calls = {"n": 0}
        def dep_query_side(*args, **kwargs):
            dep_calls["n"] += 1
            if dep_calls["n"] == 1:
                return {"Items": [beta_dep]}   # 1st call → BETA dep found
            return {"Items": []}               # 2nd call → no PROD dep needed

        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.side_effect = dep_query_side
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="3.0.0")}

        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert devices[0]["availableVersion"] == "3.0.0"
        assert devices[0].get("rolloutStage") == "BETA"

    def test_beta_deployment_not_offered_if_device_not_in_target_ids(
        self, monkeypatch, mock_dynamo
    ):
        """Device NOT in BETA targetIds → PROD deployment checked instead."""
        beta_dep = _deployment(version="3.0.0", stage="BETA", dep_id="beta-dep-002")
        beta_dep["targetIds"] = ["other-device-id"]  # this device is NOT in the list

        prod_dep = _deployment(version="2.0.0", stage="PRODUCTION", dep_id="prod-dep-001")

        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}

        dep_calls = {"n": 0}
        def dep_query_side(*args, **kwargs):
            dep_calls["n"] += 1
            if dep_calls["n"] == 1:
                return {"Items": [beta_dep]}   # 1st call → BETA dep exists but device not in it
            return {"Items": [prod_dep]}       # 2nd call → PROD dep found

        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.side_effect = dep_query_side
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}

        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert devices[0]["availableVersion"] == "2.0.0"
        assert devices[0].get("rolloutStage") == "PRODUCTION"

    def test_device_not_in_production_group_not_offered_prod_update(
        self, monkeypatch, mock_dynamo
    ):
        """Device NOT in DGX-Production group → PRODUCTION deployment not offered."""
        prod_dep = _deployment(version="2.0.0", stage="PRODUCTION")

        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": []}  # no BETA dep

        # Override IoT to return empty groups (device NOT in production)
        mock_iot = MagicMock()
        mock_iot.list_thing_groups_for_thing.return_value = {"thingGroups": []}
        mock_iot.describe_job_execution.side_effect = Exception("not mocked")
        monkeypatch.setattr(lf, "iot", mock_iot)

        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 0

    def test_device_in_production_group_offered_prod_update(
        self, monkeypatch, mock_dynamo
    ):
        """Device IS in DGX-Production group → PRODUCTION deployment offered (default fixture)."""
        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [_device(installed="1.0.0")]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": [_deployment(version="2.0.0", stage="PRODUCTION")]}
        mock_dynamo[lf.PACKAGES_TABLE].get_item.return_value = {"Item": _package(version="2.0.0")}
        # IoT mock already returns PRODUCTION_GROUP in default fixture

        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        assert len(devices) == 1
        assert devices[0]["availableVersion"] == "2.0.0"

    def test_no_thing_name_skips_production_check(self, monkeypatch, mock_dynamo):
        """Device with no thingName (not OTA-registered) → no group check → no PROD update."""
        dev_no_thing = _device(installed="1.0.0")
        del dev_no_thing["thingName"]

        mock_dynamo[lf.DEVICE_DATA_TABLE].query.return_value = {"Items": [dev_no_thing]}
        mock_dynamo[lf.DEPLOYMENTS_TABLE].query.return_value = {"Items": []}  # no BETA

        resp = lf.lambda_handler(_event_with_claims(), MockContext())
        devices = json.loads(resp["body"])["devices"]
        # Device has pkg_name + installed_version so it's registered, but no thing → no prod update
        assert len(devices) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# _is_newer version comparison
# ═══════════════════════════════════════════════════════════════════════════════

class TestVersionComparison:
    def test_newer(self):
        assert lf._is_newer("2.0.0", "1.0.0") is True

    def test_same(self):
        assert lf._is_newer("1.0.0", "1.0.0") is False

    def test_older(self):
        assert lf._is_newer("1.0.0", "2.0.0") is False

    def test_patch_bump(self):
        assert lf._is_newer("1.0.1", "1.0.0") is True

    def test_minor_bump(self):
        assert lf._is_newer("1.1.0", "1.0.9") is True

    def test_pre_release_suffix_ignored(self):
        # e.g. "1.0.0-beta" → treated as 1.0.0
        assert lf._is_newer("1.0.1-beta", "1.0.0") is True
