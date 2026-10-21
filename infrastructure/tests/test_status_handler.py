"""
Test suite for digilux_ota_status_handler

Coverage:
  lambda_handler — MQTT status dispatch
    - Missing required fields (jobId / deviceId / status) → returns early, no DB writes
    - IN_PROGRESS: updates deviceStatuses, does not mark terminal
    - TIMED_OUT: clears pendingJobId, marks job TIMED_OUT, fires owner + admin emails
    - SUCCEEDED: updates installedVersions, clears pendingJobId, fires owner email
    - FAILED: clears pendingJobId, fires owner + admin emails
    - REJECTED: clears pendingJobId, fires admin email
    - All notification paths are non-fatal (SES exception never propagates)
    - SECURITY_ALERT audit emitted for codes in SECURITY_ERROR_CODES (FAILED + REJECTED)
    - Unhandled exception is caught (IoT Rule retry prevention)

  _aggregate_status
    - Any FAILED/REJECTED/TIMED_OUT → "FAILED"
    - Any IN_PROGRESS (no failures)  → "IN_PROGRESS"
    - All SUCCEEDED                  → "SUCCEEDED"

  Audit events
    - DEVICE_UPDATE_SUCCEEDED on success
    - DEVICE_UPDATE_FAILED on failure
    - DEVICE_UPDATE_REJECTED on rejection
    - DEVICE_UPDATE_TIMED_OUT on timeout
    - SECURITY_ALERT on security error codes

Run:
  pytest infrastructure/tests/test_status_handler.py -v
"""
import importlib
import importlib.util
import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest

os.environ.setdefault("REGION", "ap-south-1")

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_status_handler", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_status_handler", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ─────────────────────────────────────────────────────────────────
PKG_NAME   = "HomeAssistantUtility"
VERSION    = "4.5.0"
JOB_ID     = "digilux-ota-test-job-001"
DEVICE_ID  = "edb39bba-baf1-4700-968c-a42228e53aa0"
THING_NAME = DEVICE_ID
MAC        = "aa:bb:cc:dd:ee:ff"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(status="IN_PROGRESS", job_id=JOB_ID, device_id=DEVICE_ID,
           thing_name=None, progress=50, error=None, error_code=None,
           pkg_name=PKG_NAME, version=VERSION):
    e = {
        "jobId":       job_id,
        "deviceId":    device_id,
        "thingName":   thing_name or device_id,
        "status":      status,
        "progress":    progress,
        "statusDetail": "",
        "packageName": pkg_name,
        "version":     version,
    }
    if error:
        e["error"] = error
    if error_code is not None:
        e["statusDetails"] = {"errorCode": error_code}
    return e


def _device_item(mac=MAC, pending_job=None):
    d = {"deviceId": DEVICE_ID, "macAddress": mac, "thingName": THING_NAME}
    if pending_job:
        d["pendingJobId"] = pending_job
    return d


def _make_tables(device_items=None, job_item=None, dep_table=None):
    """Returns (dynamo_mock, jobs_table_mock, data_table_mock, dep_table_mock)."""
    jobs_t = MagicMock()
    jobs_t.update_item.return_value = {}
    # _aggregate_status calls get_item to read existing deviceStatuses
    jobs_t.get_item.return_value = {
        "Item": {
            "jobId":      JOB_ID,
            "status":     "IN_PROGRESS",
            "createdBy":  "user:owner-001",
            "deviceStatuses": {},
            **(job_item or {}),
        }
    }

    data_t = MagicMock()
    items = device_items if device_items is not None else [_device_item()]
    data_t.query.return_value = {"Items": items}
    data_t.update_item.return_value = {}

    dep_t = dep_table or MagicMock()

    dynamo = MagicMock()
    def _table(name):
        if "jobs" in name:
            return jobs_t
        if "device_data" in name or "device" in name:
            return data_t
        if "deploy" in name:
            return dep_t
        return MagicMock()
    dynamo.Table.side_effect = _table

    return dynamo, jobs_t, data_t, dep_t


def _run(event, device_items=None, job_item=None, notif_mock=None):
    dynamo, jobs_t, data_t, dep_t = _make_tables(device_items, job_item)
    nm = notif_mock or MagicMock()
    nm.tmpl_install_succeeded.return_value       = ("subj", "body")
    nm.tmpl_install_failed_user.return_value     = ("subj", "body")
    nm.tmpl_install_failed_admin.return_value    = ("subj", "body")
    nm.tmpl_install_timed_out_user.return_value  = ("subj", "body")
    nm.tmpl_install_timed_out_admin.return_value = ("subj", "body")
    nm.get_user_email.return_value               = "owner@test.com"

    iot_mock = MagicMock()
    iot_jobs_data = MagicMock()
    cognito_mock = MagicMock()
    cognito_mock.admin_get_user.return_value = {
        "UserAttributes": [{"Name": "email", "Value": "owner@test.com"}]
    }

    with patch.object(lf, "dynamo", dynamo), \
         patch.object(lf, "iot", iot_mock), \
         patch.object(lf, "_iot_jobs_data", iot_jobs_data), \
         patch.object(lf, "cognito", cognito_mock), \
         patch.object(lf, "_notif", nm), \
         patch.object(lf, "COGNITO_USER_POOL_ID", "ap-south-1_testpool"):
        lf.lambda_handler(event, None)

    return jobs_t, data_t, nm


# ══════════════════════════════════════════════════════════════════════════════
# Missing fields
# ══════════════════════════════════════════════════════════════════════════════

class TestMissingFields:
    def test_missing_job_id_no_db_write(self):
        e = _event()
        e.pop("jobId")
        jobs_t, _, _ = _run(e)
        jobs_t.update_item.assert_not_called()

    def test_missing_device_id_no_db_write(self):
        e = _event()
        e.pop("deviceId")
        jobs_t, _, _ = _run(e)
        jobs_t.update_item.assert_not_called()

    def test_missing_status_no_db_write(self):
        e = _event()
        e["status"] = ""
        jobs_t, _, _ = _run(e)
        jobs_t.update_item.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# IN_PROGRESS
# ══════════════════════════════════════════════════════════════════════════════

class TestInProgress:
    def test_in_progress_updates_device_status_in_job(self):
        jobs_t, _, _ = _run(_event("IN_PROGRESS"))
        jobs_t.update_item.assert_called()

    def test_in_progress_does_not_clear_pending_job(self):
        jobs_t, data_t, _ = _run(_event("IN_PROGRESS"))
        # data_table.update_item should NOT be called for IN_PROGRESS
        data_t.update_item.assert_not_called()

    def test_in_progress_no_admin_notification(self):
        _, _, nm = _run(_event("IN_PROGRESS"))
        nm.notify_admin.assert_not_called()
        nm.send_email.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# SUCCEEDED
# ══════════════════════════════════════════════════════════════════════════════

class TestSucceeded:
    def test_succeeded_updates_installed_versions(self):
        _, data_t, _ = _run(_event("SUCCEEDED"))
        data_t.update_item.assert_called()
        # Check that the update sets installedVersions or version
        call_kw = data_t.update_item.call_args.kwargs
        vals = call_kw.get("ExpressionAttributeValues", {})
        assert VERSION in vals.values() or any(VERSION in str(v) for v in vals.values())

    def test_succeeded_clears_pending_job_id(self):
        _, data_t, _ = _run(_event("SUCCEEDED"))
        call_kw = data_t.update_item.call_args.kwargs
        vals = call_kw.get("ExpressionAttributeValues", {})
        assert None in vals.values(), "pendingJobId must be set to None"

    def test_succeeded_marks_job_succeeded(self):
        jobs_t, _, _ = _run(_event("SUCCEEDED"))
        # At least one update_item call should set status=SUCCEEDED
        all_calls = jobs_t.update_item.call_args_list
        any_succeeded = any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "SUCCEEDED"
            for c in all_calls
        )
        assert any_succeeded

    def test_succeeded_sends_owner_email(self):
        _, _, nm = _run(_event("SUCCEEDED"))
        nm.send_email.assert_called_once()

    def test_succeeded_no_admin_notification(self):
        _, _, nm = _run(_event("SUCCEEDED"))
        nm.notify_admin.assert_not_called()

    def test_succeeded_email_failure_is_nonfatal(self):
        nm = MagicMock()
        nm.tmpl_install_succeeded.return_value = ("s", "b")
        nm.send_email.side_effect = Exception("SES down")
        # Should not raise
        _run(_event("SUCCEEDED"), notif_mock=nm)

    def test_succeeded_emits_audit(self, capsys):
        dynamo, jobs_t, data_t, _ = _make_tables()
        nm = MagicMock()
        nm.tmpl_install_succeeded.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm), \
             patch.object(lf, "COGNITO_USER_POOL_ID", ""):
            lf.lambda_handler(_event("SUCCEEDED"), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "DEVICE_UPDATE_SUCCEEDED" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# FAILED
# ══════════════════════════════════════════════════════════════════════════════

class TestFailed:
    def test_failed_clears_pending_job_id(self):
        _, data_t, _ = _run(_event("FAILED"))
        data_t.update_item.assert_called()
        call_kw = data_t.update_item.call_args.kwargs
        vals = call_kw.get("ExpressionAttributeValues", {})
        assert None in vals.values()

    def test_failed_marks_job_failed(self):
        jobs_t, _, _ = _run(_event("FAILED"))
        all_calls = jobs_t.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "FAILED"
            for c in all_calls
        )

    def test_failed_sends_owner_and_admin_emails(self):
        _, _, nm = _run(_event("FAILED"))
        nm.send_email.assert_called_once()
        nm.notify_admin.assert_called_once()

    def test_failed_email_failure_is_nonfatal(self):
        nm = MagicMock()
        nm.tmpl_install_failed_user.return_value  = ("s", "b")
        nm.tmpl_install_failed_admin.return_value = ("s", "b")
        nm.send_email.side_effect   = Exception("SES down")
        nm.notify_admin.side_effect = Exception("SES down")
        _run(_event("FAILED"), notif_mock=nm)  # must not raise

    def test_failed_no_email_when_device_not_found(self):
        _, _, nm = _run(_event("FAILED"), device_items=[])
        nm.send_email.assert_not_called()
        nm.notify_admin.assert_not_called()

    def test_security_alert_audit_for_security_code(self, capsys):
        dynamo, jobs_t, data_t, _ = _make_tables()
        nm = MagicMock()
        nm.tmpl_install_failed_user.return_value  = ("s", "b")
        nm.tmpl_install_failed_admin.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm), \
             patch.object(lf, "COGNITO_USER_POOL_ID", ""):
            lf.lambda_handler(_event("FAILED", error_code=10003), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "SECURITY_ALERT" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# REJECTED
# ══════════════════════════════════════════════════════════════════════════════

class TestRejected:
    def test_rejected_marks_job_failed(self):
        """REJECTED maps to FAILED at the job level."""
        jobs_t, _, _ = _run(_event("REJECTED"))
        all_calls = jobs_t.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "FAILED"
            for c in all_calls
        )

    def test_rejected_sends_admin_email(self):
        _, _, nm = _run(_event("REJECTED"))
        nm.notify_admin.assert_called_once()

    def test_rejected_email_failure_is_nonfatal(self):
        nm = MagicMock()
        nm.tmpl_install_failed_user.return_value  = ("s", "b")
        nm.tmpl_install_failed_admin.return_value = ("s", "b")
        nm.send_email.side_effect   = Exception("SES down")
        nm.notify_admin.side_effect = Exception("SES down")
        _run(_event("REJECTED"), notif_mock=nm)

    def test_rejected_security_alert_for_security_code(self, capsys):
        dynamo, jobs_t, data_t, _ = _make_tables()
        nm = MagicMock()
        nm.tmpl_install_failed_user.return_value  = ("s", "b")
        nm.tmpl_install_failed_admin.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm), \
             patch.object(lf, "COGNITO_USER_POOL_ID", ""):
            lf.lambda_handler(_event("REJECTED", error_code=10004), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "SECURITY_ALERT" for a in audits)

    def test_non_security_code_no_security_alert(self, capsys):
        dynamo, jobs_t, data_t, _ = _make_tables()
        nm = MagicMock()
        nm.tmpl_install_failed_user.return_value  = ("s", "b")
        nm.tmpl_install_failed_admin.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm), \
             patch.object(lf, "COGNITO_USER_POOL_ID", ""):
            lf.lambda_handler(_event("REJECTED", error_code=3001), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert not any(a["event"] == "SECURITY_ALERT" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# TIMED_OUT
# ══════════════════════════════════════════════════════════════════════════════

class TestTimedOut:
    def test_timed_out_marks_job_timed_out(self):
        jobs_t, _, _ = _run(_event("TIMED_OUT"))
        all_calls = jobs_t.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "TIMED_OUT"
            for c in all_calls
        )

    def test_timed_out_clears_pending_job_id(self):
        _, data_t, _ = _run(_event("TIMED_OUT"))
        data_t.update_item.assert_called()
        call_kw = data_t.update_item.call_args.kwargs
        vals = call_kw.get("ExpressionAttributeValues", {})
        assert None in vals.values()

    def test_timed_out_sends_owner_and_admin_emails(self):
        _, _, nm = _run(_event("TIMED_OUT"))
        nm.send_email.assert_called_once()
        nm.notify_admin.assert_called_once()

    def test_timed_out_email_failure_is_nonfatal(self):
        nm = MagicMock()
        nm.tmpl_install_timed_out_user.return_value  = ("s", "b")
        nm.tmpl_install_timed_out_admin.return_value = ("s", "b")
        nm.send_email.side_effect   = Exception("SES down")
        nm.notify_admin.side_effect = Exception("SES down")
        _run(_event("TIMED_OUT"), notif_mock=nm)

    def test_timed_out_emits_audit(self, capsys):
        dynamo, _, _, _ = _make_tables()
        nm = MagicMock()
        nm.tmpl_install_timed_out_user.return_value  = ("s", "b")
        nm.tmpl_install_timed_out_admin.return_value = ("s", "b")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()), \
             patch.object(lf, "cognito", MagicMock()), \
             patch.object(lf, "_notif", nm), \
             patch.object(lf, "COGNITO_USER_POOL_ID", ""):
            lf.lambda_handler(_event("TIMED_OUT"), None)
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "DEVICE_UPDATE_TIMED_OUT" for a in audits)


# ══════════════════════════════════════════════════════════════════════════════
# Exception safety
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptionSafety:
    def test_dynamo_error_is_caught_does_not_raise(self):
        """IoT Rule must not retry on unhandled errors."""
        dynamo = MagicMock()
        dynamo.Table.side_effect = Exception("DynamoDB unavailable")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", MagicMock()), \
             patch.object(lf, "_iot_jobs_data", MagicMock()):
            # Must complete without raising
            lf.lambda_handler(_event("SUCCEEDED"), None)


# ══════════════════════════════════════════════════════════════════════════════
# _aggregate_status
# ══════════════════════════════════════════════════════════════════════════════

class TestAggregateStatus:
    def _run_aggregate(self, existing_statuses: dict, new_status: str):
        dynamo = MagicMock()
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "jobId": JOB_ID,
                "status": "IN_PROGRESS",
                "deviceStatuses": {k: {"status": v} for k, v in existing_statuses.items()},
            }
        }
        dynamo.Table.return_value = table
        with patch.object(lf, "dynamo", dynamo):
            return lf._aggregate_status(JOB_ID, "new-device", new_status)

    def test_failed_device_yields_failed(self):
        assert self._run_aggregate({"dev-a": "SUCCEEDED"}, "FAILED") == "FAILED"

    def test_timed_out_device_yields_failed(self):
        assert self._run_aggregate({}, "TIMED_OUT") == "FAILED"

    def test_in_progress_yields_in_progress(self):
        assert self._run_aggregate({"dev-a": "SUCCEEDED"}, "IN_PROGRESS") == "IN_PROGRESS"

    def test_all_succeeded_yields_succeeded(self):
        assert self._run_aggregate({"dev-a": "SUCCEEDED"}, "SUCCEEDED") == "SUCCEEDED"

    def test_rejected_yields_failed(self):
        assert self._run_aggregate({}, "REJECTED") == "FAILED"
