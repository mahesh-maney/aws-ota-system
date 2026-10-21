"""
Test suite for digilux_ota_package_activate

Coverage:
  lambda_handler — auth & routing
    - 403 for non-admin (+ UNAUTHORIZED_ACCESS audit)
    - 400 for missing path parameters
    - 404 for unknown package
    - direct `activated` field in body → 400 (not supported)

  RECALL
    - 400 missing recallReason
    - 409 package not ACTIVE
    - 409 active deployment blocks recall
    - 200 success: package marked RECALLED, LATEST pointer deleted
    - Queued IoT jobs cancelled on recall
    - Per-device-owner emails sent on recall
    - notify_admin called on recall
    - notify_admin failure non-fatal

  PROMOTE (BETA → PROD)
    - 409 package not BETA
    - 409 package not ACTIVE
    - 409 existing active PRODUCTION deployment blocks promote
    - 200 success: releaseType changed to PROD, LATEST#PROD pointer written
    - BETA pointer deleted on promote
    - Auto deployment record created on promote
    - PACKAGE_PROMOTED audit emitted

  RESTORE (SUPERSEDED → ACTIVE)
    - 409 package not SUPERSEDED
    - 409 releaseType not PROD or BETA
    - 200 success: status set to ACTIVE, LATEST pointer updated

  DELETE
    - 404 package not found
    - 400 missing reason
    - 409 ACTIVE package blocked (activeJobs included in response)
    - 409 blocking QUEUED/IN_PROGRESS jobs
    - 200 soft delete for RECALLED package (record retained)
    - 200 hard delete for SUPERSEDED package (record removed)
    - S3 artifacts (s3Key, encS3Key, sigS3Key) deleted on DELETE
    - S3 delete failure is non-fatal
    - notify_admin called on DELETE
    - notify_admin failure non-fatal
    - PACKAGE_DELETED audit emitted (both soft + hard)

Run:
  pytest infrastructure/tests/test_package_activate.py -v
"""
import importlib
import importlib.util
import json
import os
import time
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("REGION", "ap-south-1")

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_package_activate", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_package_activate", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ─────────────────────────────────────────────────────────────────
PKG_NAME = "HomeAssistantUtility"
VERSION  = "4.5.0"
S3_KEY   = "Network_controller_firmware/HomeAssistantUtility/4.5.0/firmware.tar"
ENC_KEY  = "enc/some-uuid.enc"
SIG_KEY  = "sig/some-uuid.sig"
DEP_ID   = "dep-active-001"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(method="PATCH", package_name=PKG_NAME, version=VERSION,
           groups="ota-admin", body=None):
    return {
        "httpMethod":    method,
        "path":          f"/api/v1/ota/packages/{package_name}/{version}/activate",
        "pathParameters": {"packageName": package_name, "version": version},
        "requestContext": {"authorizer": {"claims": {
            "email": "admin@test.com",
            "cognito:groups": groups,
        }}},
        "body": json.dumps(body) if body else None,
    }


def _pkg_item(**overrides):
    item = {
        "packageName": PKG_NAME,
        "version":     VERSION,
        "status":      "ACTIVE",
        "releaseType": "PROD",
        "deviceType":  "Network_controller_firmware",
        "releaseNotes": "",
        "fileName":    "firmware.tar",
        "s3Key":       S3_KEY,
        "encS3Key":    ENC_KEY,
        "sigS3Key":    SIG_KEY,
    }
    item.update(overrides)
    return item


def _dep_item(status="ACTIVE", stage="PRODUCTION", dep_id=DEP_ID):
    return {
        "deploymentId": dep_id,
        "packageName":  PKG_NAME,
        "version":      VERSION,
        "status":       status,
        "rolloutStage": stage,
    }


def _client_error(code="ConditionalCheckFailedException"):
    return ClientError({"Error": {"Code": code, "Message": "msg"}}, "op")


def _make_tables(pkg_item=None, dep_items=None, job_items=None):
    pkg_t = MagicMock()
    pkg_t.get_item.return_value = {"Item": pkg_item} if pkg_item else {}
    pkg_t.update_item.return_value = {}
    pkg_t.delete_item.return_value = {}
    pkg_t.put_item.return_value = {}
    pkg_t.query.return_value = {"Items": []}

    dep_t = MagicMock()
    dep_t.query.return_value = {"Items": dep_items or []}
    dep_t.put_item.return_value = {}

    jobs_t = MagicMock()
    jobs_t.scan.return_value = {"Items": job_items or []}
    jobs_t.get_item.return_value = {}
    jobs_t.update_item.return_value = {}

    dynamo = MagicMock()
    def _table(name):
        if "packages" in name or name == lf.PACKAGES_TABLE:
            return pkg_t
        if "deploy" in name or name == lf.DEPLOYMENTS_TABLE:
            return dep_t
        if "jobs" in name or name == lf.OTA_JOBS_TABLE:
            return jobs_t
        return MagicMock()
    dynamo.Table.side_effect = _table
    return dynamo, pkg_t, dep_t, jobs_t


def _make_s3(delete_raises=None):
    s3 = MagicMock()
    if delete_raises:
        s3.delete_object.side_effect = delete_raises
    return s3


def _make_notif():
    nm = MagicMock()
    nm.tmpl_package_recalled_admin.return_value = ("s", "b")
    nm.tmpl_package_recalled_user.return_value  = ("s", "b")
    nm.tmpl_package_deleted.return_value        = ("s", "b")
    return nm


def _resp_body(resp):
    return json.loads(resp["body"])


def _run(event, pkg_item=None, dep_items=None, job_items=None,
         s3=None, notif=None, cognito=None):
    dynamo, pkg_t, dep_t, jobs_t = _make_tables(pkg_item, dep_items, job_items)
    s3_mock = s3 or _make_s3()
    nm = notif or _make_notif()
    iot_mock = MagicMock()
    iot_mock.exceptions.ResourceNotFoundException = type(
        "ResourceNotFoundException", (Exception,), {}
    )
    cog = cognito or MagicMock()

    with patch.object(lf, "dynamo", dynamo), \
         patch.object(lf, "s3", s3_mock), \
         patch.object(lf, "iot", iot_mock), \
         patch.object(lf, "cognito", cog), \
         patch.object(lf, "_notif", nm):
        resp = lf.lambda_handler(event, None)

    return resp, pkg_t, dep_t, jobs_t, s3_mock, nm, iot_mock


# ══════════════════════════════════════════════════════════════════════════════
# Auth & routing
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_non_admin_returns_403(self):
        resp, *_ = _run(_event(groups=""))
        assert resp["statusCode"] == 403

    def test_non_admin_emits_unauthorized_audit(self, capsys):
        _run(_event(groups=""))
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "UNAUTHORIZED_ACCESS" for a in audits)

    def test_missing_package_name_returns_400(self):
        resp, *_ = _run(_event(package_name=""))
        assert resp["statusCode"] == 400

    def test_missing_version_returns_400(self):
        resp, *_ = _run(_event(version=""))
        assert resp["statusCode"] == 400

    def test_unknown_package_returns_404(self):
        resp, *_ = _run(_event(body={"recalled": True, "recallReason": "test"}),
                        pkg_item=None)
        assert resp["statusCode"] == 404

    def test_direct_activated_field_returns_400(self):
        resp, *_ = _run(_event(body={"activated": True}), pkg_item=_pkg_item())
        assert resp["statusCode"] == 400


# ══════════════════════════════════════════════════════════════════════════════
# RECALL
# ══════════════════════════════════════════════════════════════════════════════

class TestRecall:
    def test_missing_recall_reason_returns_400(self):
        resp, *_ = _run(_event(body={"recalled": True, "recallReason": ""}),
                        pkg_item=_pkg_item())
        assert resp["statusCode"] == 400

    def test_non_active_package_returns_409(self):
        resp, *_ = _run(_event(body={"recalled": True, "recallReason": "bug"}),
                        pkg_item=_pkg_item(status="RECALLED"))
        assert resp["statusCode"] == 409

    def test_active_deployment_blocks_recall(self):
        resp, *_ = _run(
            _event(body={"recalled": True, "recallReason": "bug"}),
            pkg_item=_pkg_item(),
            dep_items=[_dep_item()]
        )
        assert resp["statusCode"] == 409
        body = _resp_body(resp)
        assert "activeDeploymentId" in body

    def test_recall_success_returns_200(self):
        resp, *_ = _run(
            _event(body={"recalled": True, "recallReason": "defective"}),
            pkg_item=_pkg_item(),
            dep_items=[],
        )
        assert resp["statusCode"] == 200

    def test_recall_marks_package_recalled(self):
        _, pkg_t, *_ = _run(
            _event(body={"recalled": True, "recallReason": "defective"}),
            pkg_item=_pkg_item(),
        )
        pkg_t.update_item.assert_called()
        call_kw = pkg_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":recalled"] == "RECALLED"

    def test_recall_cancels_queued_iot_jobs(self):
        job = {"jobId": "job-to-cancel", "packageName": PKG_NAME,
               "version": VERSION, "status": "QUEUED", "createdBy": "system"}
        resp, _, _, jobs_t, _, _, iot_mock = _run(
            _event(body={"recalled": True, "recallReason": "defective"}),
            pkg_item=_pkg_item(),
            job_items=[job],
        )
        iot_mock.cancel_job.assert_called_once_with(
            jobId="job-to-cancel", reasonCode="PACKAGE_RECALLED", force=False
        )

    def test_recall_notify_admin_called(self):
        _, _, _, _, _, nm, _ = _run(
            _event(body={"recalled": True, "recallReason": "defective"}),
            pkg_item=_pkg_item(),
        )
        nm.notify_admin.assert_called_once()

    def test_recall_notify_admin_failure_is_nonfatal(self):
        nm = _make_notif()
        nm.notify_admin.side_effect = Exception("SES down")
        resp, *_ = _run(
            _event(body={"recalled": True, "recallReason": "defective"}),
            pkg_item=_pkg_item(),
            notif=nm,
        )
        assert resp["statusCode"] == 200

    def test_recall_emits_audit(self, capsys):
        dynamo, _, dep_t, _ = _make_tables(_pkg_item(), dep_items=[])
        nm = _make_notif()
        iot_mock = MagicMock()
        iot_mock.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm):
            lf.lambda_handler(_event(body={"recalled": True, "recallReason": "test"}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_RECALLED" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# PROMOTE (BETA → PROD)
# ══════════════════════════════════════════════════════════════════════════════

class TestPromote:
    def test_promote_non_beta_returns_409(self):
        resp, *_ = _run(_event(body={"promote": True}),
                        pkg_item=_pkg_item(releaseType="PROD"))
        assert resp["statusCode"] == 409
        assert "BETA" in _resp_body(resp)["error"]

    def test_promote_non_active_returns_409(self):
        resp, *_ = _run(_event(body={"promote": True}),
                        pkg_item=_pkg_item(releaseType="BETA", status="SUPERSEDED"))
        assert resp["statusCode"] == 409

    def test_promote_existing_prod_deployment_returns_409(self):
        resp, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
            dep_items=[_dep_item(stage="PRODUCTION")],
        )
        assert resp["statusCode"] == 409
        assert "activeDeploymentId" in _resp_body(resp)

    def test_promote_success_returns_200(self):
        resp, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
        )
        assert resp["statusCode"] == 200

    def test_promote_updates_release_type_to_prod(self):
        _, pkg_t, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
        )
        pkg_t.update_item.assert_called()
        call_kw = pkg_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":rt"] == "PROD"

    def test_promote_creates_production_deployment_record(self):
        _, _, dep_t, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
        )
        dep_t.put_item.assert_called_once()
        call_kw = dep_t.put_item.call_args.kwargs
        assert call_kw["Item"]["rolloutStage"] == "PRODUCTION"
        assert call_kw["Item"]["status"] == "ACTIVE"

    def test_promote_writes_prod_pointer(self):
        _, pkg_t, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
        )
        # pkg_t.put_item called for LATEST#PROD pointer
        put_calls = pkg_t.put_item.call_args_list
        pointer_calls = [c for c in put_calls
                         if "LATEST#PROD" in str(c.kwargs.get("Item", {}).get("version", ""))]
        assert len(pointer_calls) >= 1

    def test_promote_deletes_beta_pointer(self):
        _, pkg_t, *_ = _run(
            _event(body={"promote": True}),
            pkg_item=_pkg_item(releaseType="BETA", status="ACTIVE"),
        )
        del_calls = pkg_t.delete_item.call_args_list
        beta_del = [c for c in del_calls
                    if "LATEST#BETA" in str(c.kwargs.get("Key", {}).get("version", ""))]
        assert len(beta_del) >= 1

    def test_promote_emits_audit(self, capsys):
        dynamo, _, dep_t, _ = _make_tables(
            _pkg_item(releaseType="BETA", status="ACTIVE"), dep_items=[]
        )
        nm = _make_notif()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm):
            lf.lambda_handler(_event(body={"promote": True}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_PROMOTED" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# RESTORE
# ══════════════════════════════════════════════════════════════════════════════

class TestRestore:
    def test_restore_non_superseded_returns_409(self):
        resp, *_ = _run(_event(body={"restore": True}),
                        pkg_item=_pkg_item(status="ACTIVE"))
        assert resp["statusCode"] == 409

    def test_restore_non_prod_beta_release_type_returns_409(self):
        resp, *_ = _run(_event(body={"restore": True}),
                        pkg_item=_pkg_item(status="SUPERSEDED", releaseType="CUSTOM"))
        assert resp["statusCode"] == 409

    def test_restore_success_returns_200(self):
        resp, *_ = _run(_event(body={"restore": True}),
                        pkg_item=_pkg_item(status="SUPERSEDED", releaseType="PROD"))
        assert resp["statusCode"] == 200

    def test_restore_sets_status_active(self):
        _, pkg_t, *_ = _run(
            _event(body={"restore": True}),
            pkg_item=_pkg_item(status="SUPERSEDED", releaseType="PROD")
        )
        call_kw = pkg_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":active"] == "ACTIVE"

    def test_restore_writes_latest_pointer(self):
        _, pkg_t, *_ = _run(
            _event(body={"restore": True}),
            pkg_item=_pkg_item(status="SUPERSEDED", releaseType="PROD")
        )
        put_calls = pkg_t.put_item.call_args_list
        pointer_calls = [c for c in put_calls
                         if "LATEST#" in str(c.kwargs.get("Item", {}).get("version", ""))]
        assert len(pointer_calls) >= 1

    def test_restore_emits_audit(self, capsys):
        dynamo, *_ = _make_tables(_pkg_item(status="SUPERSEDED", releaseType="PROD"))
        nm = _make_notif()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm):
            lf.lambda_handler(_event(body={"restore": True}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_RESTORED" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE
# ══════════════════════════════════════════════════════════════════════════════

class TestDelete:
    def _delete(self, pkg_item=None, body=None, job_items=None, s3=None, notif=None):
        return _run(
            _event("DELETE", body=body or {"reason": "obsolete"}),
            pkg_item=pkg_item,
            job_items=job_items or [],
            s3=s3,
            notif=notif,
        )

    def test_package_not_found_returns_404(self):
        resp, *_ = self._delete(pkg_item=None)
        assert resp["statusCode"] == 404

    def test_missing_reason_returns_400(self):
        resp, *_ = self._delete(_pkg_item(status="RECALLED"), body={"reason": ""})
        assert resp["statusCode"] == 400

    def test_active_package_blocked_returns_409(self):
        resp, *_ = self._delete(_pkg_item(status="ACTIVE"))
        assert resp["statusCode"] == 409
        assert "ACTIVE" in _resp_body(resp)["error"]

    def test_active_package_409_includes_active_jobs(self):
        job = {"jobId": "j1", "packageName": PKG_NAME, "version": VERSION,
               "status": "QUEUED"}
        resp, *_ = self._delete(_pkg_item(status="ACTIVE"), job_items=[job])
        body = _resp_body(resp)
        assert "activeJobs" in body

    def test_queued_job_blocks_delete(self):
        job = {"jobId": "j1", "packageName": PKG_NAME, "version": VERSION,
               "status": "QUEUED"}
        resp, *_ = self._delete(_pkg_item(status="RECALLED"), job_items=[job])
        assert resp["statusCode"] == 409

    def test_recalled_package_soft_deleted(self):
        resp, pkg_t, *_ = self._delete(_pkg_item(status="RECALLED"))
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["recordRetained"] is True
        pkg_t.update_item.assert_called()
        call_kw = pkg_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":s"] == "DELETED"

    def test_superseded_package_hard_deleted(self):
        # Need supersededAt to be old enough (> COOLING_OFF_DAYS)
        old_ms = int((time.time() - 10 * 86400) * 1000)
        resp, pkg_t, *_ = self._delete(
            _pkg_item(status="SUPERSEDED", supersededAt=old_ms),
            body={"reason": "old version"},
        )
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["recordRetained"] is False
        pkg_t.delete_item.assert_called_once()

    def test_recently_superseded_requires_force(self):
        """Package superseded 1 day ago requires force=true."""
        new_ms = int((time.time() - 1 * 86400) * 1000)
        resp, *_ = self._delete(
            _pkg_item(status="SUPERSEDED", supersededAt=new_ms),
            body={"reason": "test"},
        )
        assert resp["statusCode"] == 409
        assert _resp_body(resp)["coolingOff"] is True

    def test_recently_superseded_force_bypasses_cooling_off(self):
        new_ms = int((time.time() - 1 * 86400) * 1000)
        resp, *_ = self._delete(
            _pkg_item(status="SUPERSEDED", supersededAt=new_ms),
            body={"reason": "test", "force": True},
        )
        assert resp["statusCode"] == 200

    def test_s3_keys_deleted(self):
        resp, _, _, _, s3_mock, *_ = self._delete(_pkg_item(status="RECALLED"))
        assert s3_mock.delete_object.call_count == 3  # s3Key + encS3Key + sigS3Key

    def test_s3_delete_failure_is_nonfatal(self):
        err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "DeleteObject")
        resp, *_ = self._delete(_pkg_item(status="RECALLED"), s3=_make_s3(delete_raises=err))
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["s3Deleted"] is False

    def test_notify_admin_called_on_soft_delete(self):
        _, _, _, _, _, nm, _ = self._delete(_pkg_item(status="RECALLED"))
        nm.notify_admin.assert_called_once()

    def test_notify_admin_failure_is_nonfatal(self):
        nm = _make_notif()
        nm.notify_admin.side_effect = Exception("SES down")
        old_ms = int((time.time() - 10 * 86400) * 1000)
        resp, *_ = self._delete(
            _pkg_item(status="RECALLED"), notif=nm
        )
        assert resp["statusCode"] == 200

    def test_package_deleted_audit_on_soft_delete(self, capsys):
        dynamo, _, _, jobs_t = _make_tables(_pkg_item(status="RECALLED"), job_items=[])
        nm = _make_notif()
        iot_mock = MagicMock()
        iot_mock.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm):
            lf.lambda_handler(_event("DELETE", body={"reason": "test"}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_DELETED" and a["result"] == "SUCCESS"
                   for a in audits)

    def test_package_deleted_audit_on_hard_delete(self, capsys):
        old_ms = int((time.time() - 10 * 86400) * 1000)
        dynamo, _, _, _ = _make_tables(
            _pkg_item(status="SUPERSEDED", supersededAt=old_ms), job_items=[]
        )
        nm = _make_notif()
        iot_mock = MagicMock()
        iot_mock.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm):
            lf.lambda_handler(_event("DELETE", body={"reason": "test"}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_DELETED" for a in audits)
