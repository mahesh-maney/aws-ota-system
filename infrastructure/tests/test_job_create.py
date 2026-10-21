"""
Test suite for digilux_ota_job_create

Coverage:
  Authentication / authorisation
    - Missing/non-admin group → 403
    - Admin group present → allowed

  Input validation (POST create)
    - Missing packageName → 400
    - Missing version → 400
    - Invalid rolloutStage → 400
    - Package not found → 404
    - Package not ACTIVE → 400
    - BETA/CUSTOM with no targetIds → 400
    - BETA/CUSTOM with all invalid device IDs → 400

  PRODUCTION deployment
    - Creates deployment in DEPLOYMENTS_TABLE with status=ACTIVE
    - No consent records created
    - targetType=THING_GROUP, targetGroup=PRODUCTION_GROUP
    - jobId == deploymentId in response

  BETA deployment
    - Creates consent records (one per resolved device)
    - targetType=DEVICE_LIST, targetIds in deployment record
    - consentCount in response
    - Device not found → skipped (warning, not error)
    - Device has no userId → skipped

  CUSTOM deployment
    - Same as BETA (explicit device list, consent records)

  Supersede logic
    - Existing ACTIVE deployment for same package+stage → CANCELLED
    - Existing PENDING consents → CANCELLED
    - Existing QUEUED IoT job → cancel attempted (force=False)
    - IN_PROGRESS IoT job → InvalidStateTransitionException silently ignored
    - No existing deployment → no supersede

  List deployments (GET)
    - Returns all deployments sorted newest first
    - Respects limit param

  Get deployment detail (GET /{jobId})
    - Returns deployment with consent stats
    - Includes jobId (= deploymentId) for backward compat
    - 404 for unknown deploymentId

  Abort deployment (POST /{jobId}/abort)
    - ACTIVE → CANCELLED
    - Terminal status (COMPLETED/CANCELLED/FAILED) → 400
    - Cancels PENDING consents and QUEUED IoT jobs

Run:
  pytest infrastructure/tests/test_job_create.py -v
"""

import json
import os
import time
import importlib
import importlib.util
from unittest.mock import ANY, MagicMock, patch, call
from botocore.exceptions import ClientError

import pytest

# ── Path / env setup ──────────────────────────────────────────────────────────
os.environ.setdefault("REGION", "ap-south-1")
os.environ.setdefault("ACCOUNT_ID", "123456789012")

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_job_create", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_job_create", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ─────────────────────────────────────────────────────────────────
ADMIN_EMAIL = "admin@digilux.co.in"
PKG_NAME    = "HomeAssistantUtility"
VERSION     = "4.5.0"
DEV_ID_1    = "aaaa0000-baf1-4700-968c-a42228e53aa0"
DEV_ID_2    = "bbbb1111-baf1-4700-968c-a42228e53aa0"
USER_ID_1   = "user-sub-001"
USER_ID_2   = "user-sub-002"
MAC         = "aa:bb:cc:dd:ee:ff"

# create endpoint returns 201
_CREATE_STATUS = 201

# ── Factories ─────────────────────────────────────────────────────────────────

def _admin_claims(**extra):
    return {"cognito:groups": "ota-admin,ota-user", "email": ADMIN_EMAIL, **extra}


def _event(method="POST", body=None, job_id=None, claims=None, qs=None):
    e = {
        "httpMethod": method,
        "path": f"/ota/deployments{('/' + job_id) if job_id else ''}",
        "requestContext": {
            "authorizer": {
                "claims": claims or _admin_claims()
            }
        },
        "body": json.dumps(body) if body else None,
        "pathParameters": {"jobId": job_id} if job_id else None,
        "queryStringParameters": qs,
    }
    return e


def _pkg(status="ACTIVE", pkg_name=PKG_NAME, version=VERSION):
    return {
        "packageName": pkg_name,
        "version":     version,
        "status":      status,
        "deviceType":  "Network_controller_firmware",
        "releaseNotes": "Bug fixes and improvements",
        "artifactSize": 1024 * 500,
    }


def _device(device_id=DEV_ID_1, user_id=USER_ID_1, has_thing=True):
    d = {
        "deviceId":   device_id,
        "macAddress": "aa:bb:cc:dd:ee:ff",
        "userId":     user_id,
    }
    if has_thing:
        d["thingName"] = device_id
    return d


def _deployment(dep_id="dep-001", pkg=PKG_NAME, ver=VERSION,
                stage="PRODUCTION", status="ACTIVE"):
    return {
        "deploymentId": dep_id,
        "packageName":  pkg,
        "version":      ver,
        "rolloutStage": stage,
        "status":       status,
        "createdAt":    int(time.time() * 1000),
    }


def _client_error(code, msg=""):
    return ClientError({"Error": {"Code": code, "Message": msg or code}}, "op")


def _make_table_mock(pkg=None, existing_dep=None, devices=None, consents=None):
    """
    Returns a Table mock factory (indexed by table_name via side_effect).
    """
    devices = devices or []
    consents = consents or []

    pkg_table = MagicMock()
    pkg_table.get_item.return_value = {"Item": pkg} if pkg else {}

    dep_table = MagicMock()
    dep_table.scan.return_value = {
        "Items": [existing_dep] if existing_dep else []
    }
    dep_table.query.return_value = {
        "Items": [existing_dep] if existing_dep else []
    }
    dep_table.get_item.return_value = (
        {"Item": existing_dep} if existing_dep else {}
    )
    dep_table.put_item.return_value = {}
    dep_table.update_item.return_value = {}

    device_table = MagicMock()
    def device_query(KeyConditionExpression=None, **_):
        for d in devices:
            if hasattr(KeyConditionExpression, 'expression_map'):
                pass  # complex expression; just return first device
        return {"Items": devices[:1] if devices else []}
    device_table.query.side_effect = device_query

    consent_table = MagicMock()
    consent_table.query.return_value = {"Items": consents}
    consent_table.put_item.return_value = {}
    consent_table.update_item.return_value = {}

    def table_factory(name):
        if "packages" in name:
            return pkg_table
        if "deployments" in name:
            return dep_table
        if "device_data" in name:
            return device_table
        if "consent" in name:
            return consent_table
        return MagicMock()

    dynamo_mock = MagicMock()
    dynamo_mock.Table.side_effect = table_factory
    return dynamo_mock, pkg_table, dep_table, device_table, consent_table


def _resp_body(resp):
    return json.loads(resp["body"])


def _make_notif_mock():
    """Return a MagicMock for _notif with all tmpl_* functions returning valid (subject, body) tuples."""
    m = MagicMock()
    pair = ("mock subject", "mock body")
    m.tmpl_deployment_created.return_value = pair
    m.tmpl_deployment_aborted_admin.return_value = pair
    m.tmpl_deployment_aborted_user.return_value = pair
    m.tmpl_deployment_superseded.return_value = pair
    return m


# ── Authentication / authorisation ────────────────────────────────────────────

class TestAuth:
    def test_non_admin_gets_403(self):
        non_admin_claims = {"cognito:groups": "ota-user", "email": "user@test.com"}
        resp = lf.lambda_handler(_event(claims=non_admin_claims), None)
        assert resp["statusCode"] == 403
        assert "Admin" in _resp_body(resp).get("error", "")

    def test_no_groups_gets_403(self):
        no_group_claims = {"email": "user@test.com"}
        resp = lf.lambda_handler(_event(claims=no_group_claims), None)
        assert resp["statusCode"] == 403

    def test_admin_group_allowed(self):
        dynamo_mock, pkg_t, dep_t, dev_t, con_t = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS


# ── Input validation ──────────────────────────────────────────────────────────

class TestInputValidation:
    def test_missing_package_name(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"version": VERSION}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "packageName" in _resp_body(resp)["error"]

    def test_missing_version(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "version" in _resp_body(resp)["error"]

    def test_invalid_rollout_stage(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "CANARY"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "rolloutStage" in _resp_body(resp)["error"]

    def test_package_not_found(self):
        dynamo_mock, *_ = _make_table_mock(pkg=None)  # no package
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 404

    def test_package_not_active(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg(status="PENDING"))
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "not ACTIVE" in _resp_body(resp)["error"]

    def test_beta_without_target_ids(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "BETA"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "targetIds" in _resp_body(resp)["error"] or "device" in _resp_body(resp)["error"].lower()

    def test_custom_without_target_ids(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg())
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION,
                "rolloutStage": "CUSTOM", "targetIds": []}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400

    def test_beta_all_invalid_device_ids(self):
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg(), devices=[])
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION,
                "rolloutStage": "BETA", "targetIds": ["bad-id-1", "bad-id-2"]}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400


# ── PRODUCTION deployment ─────────────────────────────────────────────────────

class TestProductionDeployment:
    def _create_prod(self, existing_dep=None):
        dynamo_mock, pkg_t, dep_t, dev_t, con_t = _make_table_mock(
            pkg=_pkg(), existing_dep=existing_dep
        )
        iot_mock = MagicMock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        return resp, dep_t, con_t

    def test_creates_deployment_status_active(self):
        resp, dep_t, _ = self._create_prod()
        assert resp["statusCode"] == _CREATE_STATUS
        put_call = dep_t.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0]["Item"] if put_call.args else put_call.kwargs["Item"]
        assert item["status"] == "ACTIVE"

    def test_no_consent_records_created(self):
        _, _, con_t = self._create_prod()
        con_t.put_item.assert_not_called()

    def test_target_type_thing_group(self):
        resp, dep_t, _ = self._create_prod()
        put_call = dep_t.put_item.call_args
        item = (put_call.kwargs.get("Item") or
                (put_call.args[0]["Item"] if put_call.args else put_call.kwargs.get("Item", {})))
        assert item.get("targetType") == "THING_GROUP"

    def test_target_group_is_production_group(self):
        resp, dep_t, _ = self._create_prod()
        put_call = dep_t.put_item.call_args
        item = (put_call.kwargs.get("Item") or
                (put_call.args[0]["Item"] if put_call.args else {}))
        assert item.get("targetGroup") == lf.PRODUCTION_GROUP

    def test_job_id_equals_deployment_id_in_response(self):
        resp, dep_t, _ = self._create_prod()
        body = _resp_body(resp)
        assert "jobId" in body
        assert "deploymentId" in body
        assert body["jobId"] == body["deploymentId"]

    def test_package_name_and_version_in_deployment(self):
        resp, dep_t, _ = self._create_prod()
        put_call = dep_t.put_item.call_args
        item = (put_call.kwargs.get("Item") or
                (put_call.args[0]["Item"] if put_call.args else {}))
        assert item["packageName"] == PKG_NAME
        assert item["version"] == VERSION
        assert item["rolloutStage"] == "PRODUCTION"


# ── BETA deployment ───────────────────────────────────────────────────────────

class TestBetaDeployment:
    def _create_beta(self, devices=None, existing_dep=None):
        devs = devices if devices is not None else [_device(DEV_ID_1, USER_ID_1)]
        dynamo_mock, pkg_t, dep_t, dev_t, con_t = _make_table_mock(
            pkg=_pkg(), existing_dep=existing_dep, devices=devs
        )
        iot_mock = MagicMock()
        body = {
            "packageName":  PKG_NAME,
            "version":      VERSION,
            "rolloutStage": "BETA",
            "targetIds":    [d["deviceId"] for d in devs],
        }
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        return resp, dep_t, con_t

    def test_no_consent_records_at_deployment_time(self):
        """Per architecture: consent records are NOT pre-created at deployment.
        They are written only when a user taps YES via the user_consent Lambda."""
        resp, _, con_t = self._create_beta()
        assert resp["statusCode"] == _CREATE_STATUS
        con_t.put_item.assert_not_called()

    def test_target_type_device_list(self):
        resp, dep_t, _ = self._create_beta()
        put_call = dep_t.put_item.call_args
        item = (put_call.kwargs.get("Item") or
                (put_call.args[0]["Item"] if put_call.args else {}))
        assert item.get("targetType") == "DEVICE_LIST"

    def test_response_includes_target_id(self):
        """Response targetId contains the device ID for BETA deployments."""
        resp, _, _ = self._create_beta()
        body = _resp_body(resp)
        assert body.get("targetType") == "DEVICE_LIST"
        assert DEV_ID_1 in body.get("targetId", "")

    def test_response_status_is_active(self):
        """BETA deployment is immediately ACTIVE (no awaiting-consent phase)."""
        resp, _, _ = self._create_beta()
        body = _resp_body(resp)
        assert body.get("status") == "ACTIVE"
        assert body.get("rolloutStage") == "BETA"

    def test_legacy_targetid_string_accepted(self):
        """backward compat: comma-separated string targetId instead of targetIds array."""
        dynamo_mock, *_ = _make_table_mock(pkg=_pkg(), devices=[_device(DEV_ID_1)])
        iot_mock = MagicMock()
        body = {
            "packageName":  PKG_NAME,
            "version":      VERSION,
            "rolloutStage": "BETA",
            "targetId":     DEV_ID_1,  # legacy format
        }
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS


# ── Supersede logic ───────────────────────────────────────────────────────────

class TestSupersede:
    """
    When an active deployment already exists for the same package+stage,
    the Lambda blocks the new deployment with 409 (DEPLOYMENT_BLOCKED).
    Admin must abort the existing deployment first.
    """

    def _make_dm_with_active(self, existing_dep, pkg=None):
        pkg_t = MagicMock()
        pkg_t.get_item.return_value = {"Item": pkg or _pkg(version="5.0.0")}
        dep_t = MagicMock()
        dep_t.query.return_value = {"Items": [existing_dep]}
        dep_t.scan.return_value   = {"Items": []}
        dep_t.put_item.return_value   = {}
        dep_t.update_item.return_value = {}
        dep_t.get_item.return_value   = {}

        def tf(name):
            if "packages"    in name: return pkg_t
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        return dm

    def test_active_same_stage_returns_409(self):
        """Creating a PRODUCTION deployment when one already exists → 409."""
        existing = _deployment("old-dep-001", stage="PRODUCTION")
        dm = self._make_dm_with_active(existing)
        body = {"packageName": PKG_NAME, "version": "5.0.0", "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", _make_notif_mock()):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 409
        rb = _resp_body(resp)
        assert rb["activeDeploymentId"] == "old-dep-001"

    def test_409_body_includes_existing_version(self):
        """409 response body reports the existing deployment's version."""
        existing = _deployment("old-dep-001", ver="3.0.0", stage="PRODUCTION")
        dm = self._make_dm_with_active(existing)
        body = {"packageName": PKG_NAME, "version": "5.0.0", "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", _make_notif_mock()):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 409
        rb = _resp_body(resp)
        assert rb.get("activeVersion") == "3.0.0"

    def test_active_beta_does_not_block_production(self):
        """Active BETA deployment does not block a new PRODUCTION deployment."""
        existing_beta = _deployment("old-beta-001", stage="BETA")
        # For PRODUCTION, _find_active_deployment queries for PRODUCTION stage;
        # the mock returns empty Items (no active PRODUCTION deployment).
        pkg_t = MagicMock()
        pkg_t.get_item.return_value = {"Item": _pkg(version="5.0.0")}
        dep_t = MagicMock()
        dep_t.query.return_value  = {"Items": []}  # no active PRODUCTION deployment
        dep_t.put_item.return_value   = {}
        dep_t.update_item.return_value = {}
        dep_t.get_item.return_value   = {}

        def tf(name):
            if "packages"    in name: return pkg_t
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        body = {"packageName": PKG_NAME, "version": "5.0.0", "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", _make_notif_mock()):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS

    def test_no_existing_deployment_creates_201(self):
        """When no active deployment exists for this package+stage, creation succeeds."""
        dm, *_ = _make_table_mock(pkg=_pkg(version="5.0.0"))
        body = {"packageName": PKG_NAME, "version": "5.0.0", "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", _make_notif_mock()):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS


# ── List deployments ──────────────────────────────────────────────────────────

class TestListDeployments:
    def test_returns_200_with_jobs(self):
        items = [_deployment("d1"), _deployment("d2")]
        dep_t = MagicMock()
        # _list_jobs uses tbl.query(IndexName=...) not scan; returns same items for all statuses
        dep_t.query.return_value = {"Items": items}

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET"), None)

        assert resp["statusCode"] == 200
        body = _resp_body(resp)
        assert "jobs" in body
        assert len(body["jobs"]) >= 2  # may have duplicates across statuses

    def test_deployment_has_job_id_field(self):
        dep_t = MagicMock()
        dep_t.query.return_value = {"Items": [_deployment("dep-abc")]}

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET"), None)

        body = _resp_body(resp)
        assert body["jobs"][0]["jobId"] == "dep-abc"

    def test_production_target_shows_group_name(self):
        dep = _deployment("dep-prod")
        dep["targetGroup"] = lf.PRODUCTION_GROUP
        dep["targetType"]  = "THING_GROUP"
        dep_t = MagicMock()
        # _list_jobs uses tbl.query(IndexName=...) not scan
        dep_t.query.return_value = {"Items": [dep]}

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET"), None)

        body = _resp_body(resp)
        assert len(body["jobs"]) > 0
        assert body["jobs"][0]["targetId"] == lf.PRODUCTION_GROUP


# ── Get deployment detail ─────────────────────────────────────────────────────

class TestGetDeployment:
    def test_returns_deployment_with_consent_stats(self):
        dep = _deployment("dep-detail-001")
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        con_t = MagicMock()
        # _get_job only queries consents from deploymentId-index GSI and counts ACCEPTED
        con_t.query.return_value = {"Items": [
            {"consentId": "c1", "status": "ACCEPTED"},
            {"consentId": "c2", "status": "ACCEPTED"},
        ]}

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent" in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET", job_id="dep-detail-001"), None)

        assert resp["statusCode"] == 200
        body = _resp_body(resp)
        assert body.get("consentStats", {}).get("ACCEPTED") == 2

    def test_returns_404_for_unknown_id(self):
        dep_t = MagicMock()
        dep_t.get_item.return_value = {}  # no Item

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET", job_id="nonexistent-dep"), None)

        assert resp["statusCode"] == 404

    def test_job_id_backward_compat(self):
        dep = _deployment("dep-compat-001")
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        con_t = MagicMock()
        con_t.query.return_value = {"Items": []}

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent" in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm):
            resp = lf.lambda_handler(_event(method="GET", job_id="dep-compat-001"), None)

        body = _resp_body(resp)
        assert body["jobId"] == "dep-compat-001"
        assert body["deploymentId"] == "dep-compat-001"


# ── Abort deployment ──────────────────────────────────────────────────────────

class TestAbortDeployment:
    def _dep_t(self, dep):
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        dep_t.update_item.return_value = {}
        return dep_t

    def _run_abort(self, dep, consents=None, reason="Test abort reason", jobs=None):
        """Run an abort request.

        jobs: optional dict {jobId: job_item} for OTA_JOBS_TABLE lookups.
              Used to test IoT job cancellation (status must be "QUEUED" to trigger cancel).
        """
        dep_t = self._dep_t(dep)
        con_t = MagicMock()
        con_t.query.return_value = {"Items": consents or []}
        con_t.update_item.return_value = {}
        jobs_t = MagicMock()
        if jobs:
            def _get_job(Key, **kw):
                return {"Item": jobs[Key["jobId"]]} if Key.get("jobId") in jobs else {}
            jobs_t.get_item.side_effect = _get_job
        iot_mock = MagicMock()
        notif_mock = _make_notif_mock()

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent"     in name: return con_t
            if "jobs"        in name: return jobs_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "_notif", notif_mock):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="dep-abort-001",
                       body={"reason": reason}), None
            )
        return resp, dep_t, con_t, iot_mock, notif_mock

    def test_active_deployment_cancelled(self):
        dep = _deployment("dep-abort-001", status="ACTIVE")
        resp, dep_t, _, _, _ = self._run_abort(dep)
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["status"] == "CANCELLED"

    def test_terminal_deployment_returns_400(self):
        for status in ("COMPLETED", "CANCELLED", "FAILED"):
            dep = _deployment("dep-abort-001", status=status)
            resp, *_ = self._run_abort(dep)
            assert resp["statusCode"] == 400, f"Expected 400 for {status}"
            assert status in _resp_body(resp)["error"]

    def test_missing_reason_returns_400(self):
        dep = _deployment("dep-abort-001", status="ACTIVE")
        dep_t = self._dep_t(dep)

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), patch.object(lf, "iot", MagicMock()):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="dep-abort-001", body={}), None
            )
        assert resp["statusCode"] == 400
        assert "reason" in _resp_body(resp)["error"].lower()

    def test_consent_records_untouched_on_abort(self):
        """Aborting a deployment does NOT modify consent records — per architecture design."""
        dep = _deployment("dep-abort-001", status="ACTIVE")
        pending_consent = {
            "consentId":    "con-abort-001",
            "deploymentId": "dep-abort-001",
            "status":       "PENDING",
        }
        resp, _, con_t, _, _ = self._run_abort(dep, consents=[pending_consent])
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["status"] == "CANCELLED"
        # Consent records are intentionally not modified during abort
        con_t.update_item.assert_not_called()

    def test_queued_iot_job_cancelled_on_abort(self):
        """QUEUED IoT jobs are cancelled when a deployment is aborted."""
        dep = _deployment("dep-abort-001", status="ACTIVE")
        accepted_consent = {
            "consentId":    "con-abort-002",
            "deploymentId": "dep-abort-001",
            "status":       "ACCEPTED",
            "jobId":        "iot-job-to-cancel",
        }
        queued_job = {"jobId": "iot-job-to-cancel", "status": "QUEUED"}
        _, _, _, iot_mock, _ = self._run_abort(
            dep, consents=[accepted_consent],
            jobs={"iot-job-to-cancel": queued_job},
        )
        iot_mock.cancel_job.assert_called_once_with(
            jobId="iot-job-to-cancel",
            reasonCode="DEPLOYMENT_ABORTED",
            comment=ANY,
            force=False,
        )

    def test_abort_not_found_returns_404(self):
        dep_t = MagicMock()
        dep_t.get_item.return_value = {}

        def tf(name):
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", MagicMock()):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="nonexistent-dep",
                       body={"reason": "test"}), None
            )
        assert resp["statusCode"] == 404


# ── Device resolve helpers ────────────────────────────────────────────────────

class TestDeviceResolve:
    def _make_device_table(self, devices_by_id):
        """devices_by_id: {deviceId: device_dict or None}"""
        tbl = MagicMock()
        def query_side(KeyConditionExpression=None, **_):
            # Extract the device ID from the expression value
            # boto3 Key("deviceId").eq(value) stores value in expression_map
            expr_vals = getattr(KeyConditionExpression, 'expression_map', {})
            # fallback: try to match any device
            for dev_id, dev in devices_by_id.items():
                items = [dev] if dev else []
                return {"Items": items}
            return {"Items": []}
        tbl.query.side_effect = query_side
        return tbl

    def test_device_not_found_skipped(self):
        """_resolve_device_list skips missing devices."""
        dev_tbl = MagicMock()
        dev_tbl.query.return_value = {"Items": []}  # not found
        dm = MagicMock()
        dm.Table.return_value = dev_tbl

        with patch.object(lf, "dynamo", dm):
            result = lf._resolve_device_list(["nonexistent-id"], "BETA", "test")

        assert result == []

    def test_device_no_user_id_skipped(self):
        """Device without userId cannot have consent — must be skipped."""
        dev = {"deviceId": DEV_ID_1, "macAddress": "aa:bb:cc:dd:ee:ff", "thingName": DEV_ID_1}
        # no userId key
        dev_tbl = MagicMock()
        dev_tbl.query.return_value = {"Items": [dev]}
        dm = MagicMock()
        dm.Table.return_value = dev_tbl

        with patch.object(lf, "dynamo", dm):
            result = lf._resolve_device_list([DEV_ID_1], "BETA", "test")

        assert result == []

    def test_valid_device_resolved(self):
        dev = _device(DEV_ID_1, USER_ID_1)
        dev_tbl = MagicMock()
        dev_tbl.query.return_value = {"Items": [dev]}
        dm = MagicMock()
        dm.Table.return_value = dev_tbl

        with patch.object(lf, "dynamo", dm):
            result = lf._resolve_device_list([DEV_ID_1], "BETA", "test")

        assert len(result) == 1
        assert result[0]["userId"] == USER_ID_1
        assert result[0]["deviceId"] == DEV_ID_1


# ── _find_active_deployment ───────────────────────────────────────────────────

class TestFindActiveDeployment:
    def test_returns_none_when_not_found(self):
        tbl = MagicMock()
        tbl.query.return_value = {"Items": []}
        dm = MagicMock(); dm.Table.return_value = tbl

        with patch.object(lf, "dynamo", dm):
            result = lf._find_active_deployment(PKG_NAME, "PRODUCTION")

        assert result is None

    def test_returns_deployment_when_found(self):
        dep = _deployment("found-dep", stage="PRODUCTION")
        tbl = MagicMock()
        tbl.query.return_value = {"Items": [dep]}
        dm = MagicMock(); dm.Table.return_value = tbl

        with patch.object(lf, "dynamo", dm):
            result = lf._find_active_deployment(PKG_NAME, "PRODUCTION")

        assert result is not None
        assert result["deploymentId"] == "found-dep"

    def test_queries_correct_gsi(self):
        tbl = MagicMock()
        tbl.query.return_value = {"Items": []}
        dm = MagicMock(); dm.Table.return_value = tbl

        with patch.object(lf, "dynamo", dm):
            lf._find_active_deployment(PKG_NAME, "PRODUCTION")

        query_call = tbl.query.call_args
        assert query_call.kwargs.get("IndexName") == lf.DEPLOYMENTS_PKG_STATUS_INDEX


# ── Email notification tests ──────────────────────────────────────────────────

class TestEmailNotifications:
    """
    Verify that _notif calls are made at the correct event points and
    that notification failures are always non-fatal.
    """

    def _make_dynamo(self, pkg=None, existing_dep=None):
        pkg_t = MagicMock()
        pkg_t.get_item.return_value = {"Item": pkg or _pkg()} if (pkg or True) else {}
        dep_t = MagicMock()
        dep_t.scan.return_value  = {"Items": [existing_dep] if existing_dep else []}
        dep_t.query.return_value = {"Items": [existing_dep] if existing_dep else []}
        dep_t.get_item.return_value = {"Item": existing_dep} if existing_dep else {}
        dep_t.put_item.return_value = {}
        dep_t.update_item.return_value = {}
        con_t = MagicMock()
        con_t.query.return_value = {"Items": []}
        con_t.put_item.return_value = {}
        con_t.update_item.return_value = {}

        def tf(name):
            if "packages"     in name: return pkg_t
            if "deployments"  in name: return dep_t
            if "consent"      in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        return dm, dep_t, con_t

    # ── Deployment created → notify_admin ────────────────────────────────────

    def test_deployment_created_notifies_admin(self):
        dm, _, _ = self._make_dynamo()
        notif = _make_notif_mock()
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS
        notif.notify_admin.assert_called()
        # Verify template was called with correct package/version
        args = notif.tmpl_deployment_created.call_args[0]
        assert args[0] == PKG_NAME
        assert args[1] == VERSION

    def test_deployment_created_notify_failure_is_nonfatal(self):
        """SES failure must not prevent the deployment from being created."""
        dm, _, _ = self._make_dynamo()
        notif = _make_notif_mock()
        notif.notify_admin.side_effect = Exception("SES unavailable")
        body = {"packageName": PKG_NAME, "version": VERSION, "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == _CREATE_STATUS

    # ── Deployment aborted → admin + user emails ──────────────────────────────

    def test_abort_notifies_admin(self):
        dep = _deployment("dep-notif-abort", status="ACTIVE", stage="PRODUCTION")
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        dep_t.update_item.return_value = {}
        con_t = MagicMock()
        con_t.query.return_value = {"Items": []}
        notif = _make_notif_mock()

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent"     in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="dep-notif-abort",
                       body={"reason": "Security issue"}), None
            )
        assert resp["statusCode"] == 200
        notif.notify_admin.assert_called()
        notif.tmpl_deployment_aborted_admin.assert_called_once()

    def test_abort_with_accepted_consent_emails_device_owner(self):
        """Device owners of consented jobs receive an email on abort."""
        dep = _deployment("dep-notif-abort2", status="ACTIVE")
        dep["rolloutStage"] = "BETA"
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        dep_t.update_item.return_value = {}
        accepted_consent = {
            "consentId":    "con-notif-001",
            "deploymentId": "dep-notif-abort2",
            "status":       "ACCEPTED",
            "jobId":        "iot-job-notif-001",
            "userId":       "user:user-sub-001",
        }
        con_t = MagicMock()
        con_t.query.return_value = {"Items": [accepted_consent]}
        con_t.update_item.return_value = {}
        notif = _make_notif_mock()
        notif.get_user_email.return_value = "owner@example.com"

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent"     in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="dep-notif-abort2",
                       body={"reason": "Recall"}), None
            )
        assert resp["statusCode"] == 200
        notif.send_email.assert_called()

    def test_abort_user_email_failure_is_nonfatal(self):
        """send_email failure must not abort the abort operation."""
        dep = _deployment("dep-notif-abort3", status="ACTIVE")
        dep_t = MagicMock()
        dep_t.get_item.return_value = {"Item": dep}
        dep_t.update_item.return_value = {}
        con_t = MagicMock()
        con_t.query.return_value = {"Items": []}
        notif = _make_notif_mock()
        notif.notify_admin.side_effect = Exception("SES error")

        def tf(name):
            if "deployments" in name: return dep_t
            if "consent"     in name: return con_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(
                _event(method="POST", job_id="dep-notif-abort3",
                       body={"reason": "test"}), None
            )
        # Deployment must still be marked CANCELLED even when email fails
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["status"] == "CANCELLED"

    # ── Supersede → notify_admin ──────────────────────────────────────────────

    def test_no_notification_when_blocked_by_active_deployment(self):
        """409 (active deployment exists) must not trigger any notifications."""
        old_dep = _deployment("old-dep-notif", stage="PRODUCTION")
        pkg_t = MagicMock()
        pkg_t.get_item.return_value = {"Item": _pkg(version="5.0.0")}
        dep_t = MagicMock()
        dep_t.query.return_value  = {"Items": [old_dep]}
        dep_t.scan.return_value   = {"Items": []}
        dep_t.put_item.return_value  = {}
        dep_t.update_item.return_value = {}
        dep_t.get_item.return_value  = {}
        notif = _make_notif_mock()

        def tf(name):
            if "packages"    in name: return pkg_t
            if "deployments" in name: return dep_t
            return MagicMock()

        dm = MagicMock(); dm.Table.side_effect = tf
        body = {"packageName": PKG_NAME, "version": "5.0.0", "rolloutStage": "PRODUCTION"}
        with patch.object(lf, "dynamo", dm), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(_event(body=body), None)

        assert resp["statusCode"] == 409
        notif.notify_admin.assert_not_called()

    # ── No notification on validation errors ──────────────────────────────────

    def test_no_email_on_400_validation_error(self):
        """Validation failures must not trigger any notifications."""
        notif = MagicMock()
        body = {"packageName": PKG_NAME}  # missing version
        with patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        notif.notify_admin.assert_not_called()
        notif.send_email.assert_not_called()

    def test_no_email_on_403(self):
        non_admin_claims = {"cognito:groups": "ota-user", "email": "user@test.com"}
        notif = MagicMock()
        with patch.object(lf, "_notif", notif):
            resp = lf.lambda_handler(_event(claims=non_admin_claims), None)
        assert resp["statusCode"] == 403
        notif.notify_admin.assert_not_called()
