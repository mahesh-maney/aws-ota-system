"""
Test suite for digilux_ota_package_status

Coverage:
  lambda_handler
    - 403 for non-admin caller
    - 400 for missing path parameters
    - GET delegates to _get_status
    - DELETE delegates to _delete_package

  _get_status
    - 404 when package not in DynamoDB
    - 200 with all standard fields present
    - Optional fields (sha256, artifactSize, corruptReason, totalSize) included when present
    - Fields absent when not in item

  _delete_package
    - 400 when reason is empty / missing
    - 404 when package not found
    - 409 when status=ACTIVE and force=false
    - 200 when status=ACTIVE and force=true (override)
    - 200 normal delete: S3 artifact removed, DDB marked DELETED
    - S3 delete failure is non-fatal (still returns 200)
    - Compat record deleted on success
    - notify_admin called on successful delete
    - notify_admin failure is non-fatal

  Audit events
    - UNAUTHORIZED_ACCESS emitted on 403
    - PACKAGE_DELETE_BLOCKED emitted on 409
    - PACKAGE_DELETED emitted on 200 delete

Run:
  pytest infrastructure/tests/test_package_status.py -v
"""
import importlib
import importlib.util
import json
import os
from unittest.mock import MagicMock, patch, call

import pytest

# ── env vars required at module import time ───────────────────────────────────
os.environ.setdefault("REGION", "ap-south-1")

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_package_status", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_package_status", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ─────────────────────────────────────────────────────────────────
PKG_NAME = "HomeAssistantUtility"
VERSION  = "4.5.0"
S3_KEY   = "Network_controller_firmware/HomeAssistantUtility/4.5.0/firmware.tar"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(method="GET", package_name=PKG_NAME, version=VERSION,
           groups="ota-admin", body=None):
    return {
        "httpMethod": method,
        "pathParameters": {"packageName": package_name, "version": version},
        "requestContext": {"authorizer": {"claims": {
            "email": "admin@test.com",
            "cognito:groups": groups,
        }}},
        "body": json.dumps(body) if body is not None else None,
    }


def _pkg_item(**overrides):
    item = {
        "packageName": PKG_NAME,
        "version":     VERSION,
        "status":      "ACTIVE",
        "deviceType":  "Network_controller_firmware",
        "releaseType": "PROD",
        "fileName":    "firmware.tar",
        "s3Key":       S3_KEY,
        "createdAt":   1700000000000,
        "uploadType":  "SINGLE",
    }
    item.update(overrides)
    return item


def _make_tables(pkg_item=None, compat_table=None):
    """Return (dynamo_mock, pkg_table_mock, compat_table_mock)."""
    pkg_t = MagicMock()
    pkg_t.get_item.return_value = {"Item": pkg_item} if pkg_item else {}
    pkg_t.update_item.return_value = {}

    compat_t = compat_table or MagicMock()

    dynamo = MagicMock()
    def _table(name):
        if "packages" in name or "ota_packages" in name:
            return pkg_t
        if "compat" in name or "compatibility" in name:
            return compat_t
        return MagicMock()
    dynamo.Table.side_effect = _table
    return dynamo, pkg_t, compat_t


def _make_s3(delete_raises=None):
    s3 = MagicMock()
    if delete_raises:
        s3.delete_object.side_effect = delete_raises
    return s3


def _resp_body(resp):
    return json.loads(resp["body"])


# ══════════════════════════════════════════════════════════════════════════════
# lambda_handler — auth & routing
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_non_admin_returns_403(self):
        dynamo, _, _ = _make_tables()
        with patch.object(lf, "dynamo", dynamo):
            resp = lf.lambda_handler(_event(groups=""), None)
        assert resp["statusCode"] == 403
        assert "Admin" in _resp_body(resp)["error"]

    def test_non_admin_emits_unauthorized_audit(self, capsys):
        dynamo, _, _ = _make_tables()
        with patch.object(lf, "dynamo", dynamo):
            lf.lambda_handler(_event(groups=""), None)
        printed = capsys.readouterr().out
        audit = json.loads(printed.strip().splitlines()[-1])
        assert audit["audit"] is True
        assert audit["event"] == "UNAUTHORIZED_ACCESS"
        assert audit["result"] == "FAILURE"

    def test_missing_package_name_returns_400(self):
        dynamo, _, _ = _make_tables(_pkg_item())
        with patch.object(lf, "dynamo", dynamo):
            resp = lf.lambda_handler(_event(package_name=""), None)
        assert resp["statusCode"] == 400

    def test_missing_version_returns_400(self):
        dynamo, _, _ = _make_tables(_pkg_item())
        with patch.object(lf, "dynamo", dynamo):
            resp = lf.lambda_handler(_event(version=""), None)
        assert resp["statusCode"] == 400


# ══════════════════════════════════════════════════════════════════════════════
# _get_status
# ══════════════════════════════════════════════════════════════════════════════

class TestGetStatus:
    def test_unknown_package_returns_404(self):
        dynamo, _, _ = _make_tables(pkg_item=None)
        with patch.object(lf, "dynamo", dynamo):
            resp = lf.lambda_handler(_event("GET"), None)
        assert resp["statusCode"] == 404

    def test_known_package_returns_200(self):
        dynamo, _, _ = _make_tables(_pkg_item())
        with patch.object(lf, "dynamo", dynamo):
            resp = lf.lambda_handler(_event("GET"), None)
        assert resp["statusCode"] == 200

    def test_response_contains_standard_fields(self):
        dynamo, _, _ = _make_tables(_pkg_item())
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        for field in ("packageName", "version", "status", "deviceType",
                      "releaseType", "fileName", "s3Key"):
            assert field in body, f"missing field: {field}"

    def test_sha256_included_when_present(self):
        dynamo, _, _ = _make_tables(_pkg_item(sha256="deadbeef" * 8))
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        assert body["sha256"] == "deadbeef" * 8

    def test_sha256_absent_when_missing(self):
        item = _pkg_item()
        item.pop("sha256", None)
        dynamo, _, _ = _make_tables(item)
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        assert "sha256" not in body

    def test_artifact_size_cast_to_int(self):
        from decimal import Decimal
        dynamo, _, _ = _make_tables(_pkg_item(artifactSize=Decimal("102400")))
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        assert body["artifactSize"] == 102400
        assert isinstance(body["artifactSize"], int)

    def test_corrupt_reason_included_when_present(self):
        dynamo, _, _ = _make_tables(_pkg_item(status="CORRUPTED", corruptReason="SHA256 mismatch"))
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        assert body["corruptReason"] == "SHA256 mismatch"

    def test_created_at_cast_to_int(self):
        from decimal import Decimal
        dynamo, _, _ = _make_tables(_pkg_item(createdAt=Decimal("1700000000000")))
        with patch.object(lf, "dynamo", dynamo):
            body = _resp_body(lf.lambda_handler(_event("GET"), None))
        assert isinstance(body["createdAt"], int)


# ══════════════════════════════════════════════════════════════════════════════
# _delete_package
# ══════════════════════════════════════════════════════════════════════════════

class TestDeletePackage:
    def _run_delete(self, pkg_item=None, body=None, s3=None):
        dynamo, pkg_t, compat_t = _make_tables(pkg_item)
        s3_mock = s3 or _make_s3()
        notif_mock = MagicMock()
        notif_mock.tmpl_package_deleted.return_value = ("subj", "body")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", s3_mock), \
             patch.object(lf, "_notif", notif_mock):
            resp = lf.lambda_handler(
                _event("DELETE", body=body if body is not None else {"reason": "obsolete"}), None
            )
        return resp, pkg_t, compat_t, s3_mock, notif_mock

    def test_missing_reason_returns_400(self):
        resp, *_ = self._run_delete(_pkg_item(), body={"reason": ""})
        assert resp["statusCode"] == 400
        assert "reason" in _resp_body(resp)["error"].lower()

    def test_missing_reason_field_returns_400(self):
        resp, *_ = self._run_delete(_pkg_item(), body={})
        assert resp["statusCode"] == 400

    def test_package_not_found_returns_404(self):
        resp, *_ = self._run_delete(pkg_item=None)
        assert resp["statusCode"] == 404

    def test_active_without_force_returns_409(self):
        resp, *_ = self._run_delete(_pkg_item(status="ACTIVE"),
                                    body={"reason": "test"})
        assert resp["statusCode"] == 409
        assert "ACTIVE" in _resp_body(resp)["error"]

    def test_active_409_emits_blocked_audit(self, capsys):
        self._run_delete(_pkg_item(status="ACTIVE"), body={"reason": "test"})
        printed = capsys.readouterr().out
        events = [json.loads(l) for l in printed.strip().splitlines() if l.strip()]
        audit_events = [e for e in events if e.get("audit")]
        assert any(e["event"] == "PACKAGE_DELETE_BLOCKED" for e in audit_events)

    def test_active_with_force_returns_200(self):
        resp, *_ = self._run_delete(
            _pkg_item(status="ACTIVE"),
            body={"reason": "emergency", "force": True}
        )
        assert resp["statusCode"] == 200

    def test_delete_marks_ddb_deleted(self):
        _, pkg_t, *_ = self._run_delete(_pkg_item(status="RECALLED"))
        pkg_t.update_item.assert_called_once()
        call_kw = pkg_t.update_item.call_args.kwargs
        assert ":s" in call_kw["ExpressionAttributeValues"]
        assert call_kw["ExpressionAttributeValues"][":s"] == "DELETED"

    def test_delete_removes_s3_artifact(self):
        resp, _, _, s3_mock, _ = self._run_delete(
            _pkg_item(status="RECALLED", s3Key=S3_KEY)
        )
        s3_mock.delete_object.assert_called_once()
        call_kw = s3_mock.delete_object.call_args.kwargs
        assert call_kw["Key"] == S3_KEY

    def test_s3_delete_failure_is_nonfatal(self):
        from botocore.exceptions import ClientError
        err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "DeleteObject")
        resp, *_ = self._run_delete(
            _pkg_item(status="RECALLED", s3Key=S3_KEY),
            s3=_make_s3(delete_raises=err)
        )
        assert resp["statusCode"] == 200
        assert _resp_body(resp)["s3Deleted"] is False

    def test_compat_record_deleted_on_success(self):
        _, _, compat_t, _, _ = self._run_delete(_pkg_item(status="RECALLED"))
        compat_t.delete_item.assert_called_once()

    def test_notify_admin_called_on_success(self):
        _, _, _, _, notif_mock = self._run_delete(_pkg_item(status="RECALLED"))
        notif_mock.notify_admin.assert_called_once()

    def test_notify_admin_failure_is_nonfatal(self):
        dynamo, _, _ = _make_tables(_pkg_item(status="RECALLED"))
        s3_mock = _make_s3()
        notif_mock = MagicMock()
        notif_mock.tmpl_package_deleted.return_value = ("subj", "body")
        notif_mock.notify_admin.side_effect = Exception("SES down")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", s3_mock), \
             patch.object(lf, "_notif", notif_mock):
            resp = lf.lambda_handler(_event("DELETE", body={"reason": "test"}), None)
        assert resp["statusCode"] == 200

    def test_package_deleted_audit_emitted_on_success(self, capsys):
        dynamo, _, _ = _make_tables(_pkg_item(status="RECALLED"))
        notif_mock = MagicMock()
        notif_mock.tmpl_package_deleted.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "s3", _make_s3()), \
             patch.object(lf, "_notif", notif_mock):
            lf.lambda_handler(_event("DELETE", body={"reason": "test"}), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "PACKAGE_DELETED" and a["result"] == "SUCCESS"
                   for a in audits)

    def test_response_body_contains_package_fields(self):
        resp, *_ = self._run_delete(_pkg_item(status="RECALLED"))
        body = _resp_body(resp)
        assert body["packageName"] == PKG_NAME
        assert body["version"] == VERSION
        assert body["status"] == "DELETED"
