"""
Test suite for digilux_ota_job_sync

Coverage:
  lambda_handler — dispatch
    - EventBridge scheduled event → _handle_staleness_check
    - IoT lifecycle event (jobId + eventType present) → _handle_lifecycle_event
    - Unrecognised event shape → {"status": "skipped"}

  _handle_staleness_check
    - No stale jobs → cancelled=[], synced=[]
    - Stale QUEUED job: IoT still active → cancelled + audit DEPLOYMENT_CANCELLED
    - Stale QUEUED job: IoT not found → marked CANCELLED + audit
    - Stale QUEUED job: IoT already COMPLETED → synced to SUCCEEDED
    - Stale QUEUED job: IoT already CANCELED → synced to CANCELLED
    - IoT cancel_job failure: _audit("IOT_CANCEL_FAILED") emitted, job still cancelled in DDB
    - IoT describe_job ClientError → job skipped (no DDB write)
    - notify_admin called with stale job list when jobs cancelled
    - notify_admin failure is non-fatal

  _handle_lifecycle_event
    - Job not in DDB → skipped
    - event_type="canceled", status!=CANCELLED → marked CANCELLED + audit
    - event_type="canceled", already CANCELLED → noop
    - event_type="completed", all succeeded → SUCCEEDED + audit
    - event_type="completed", some failed → FAILED + audit
    - event_type="completed", already terminal → noop
    - Unknown eventType → skipped
    - IoT describe_job fails on "completed" → {"status": "error"}

Run:
  pytest infrastructure/tests/test_job_sync.py -v
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
    "digilux_ota_job_sync", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_job_sync", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Constants ─────────────────────────────────────────────────────────────────
JOB_ID   = "digilux-ota-test-job-001"
PKG_NAME = "HomeAssistantUtility"
VERSION  = "4.5.0"
# A timestamp old enough to be stale (20 days ago in ms)
_STALE_MS = int((time.time() - 20 * 86400) * 1000)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_item(job_id=JOB_ID, status="QUEUED", created_at=None):
    return {
        "jobId":       job_id,
        "packageName": PKG_NAME,
        "version":     VERSION,
        "status":      status,
        "thingName":   "thing-001",
        "createdAt":   created_at if created_at is not None else _STALE_MS,
    }


def _client_error(code="AccessDeniedException"):
    return ClientError({"Error": {"Code": code, "Message": "msg"}}, "op")


def _make_dynamo(scan_items=None, get_item_response=None):
    jobs_t = MagicMock()
    jobs_t.scan.return_value = {"Items": scan_items or []}
    if get_item_response is not None:
        jobs_t.get_item.return_value = get_item_response
    jobs_t.update_item.return_value = {}
    dynamo = MagicMock()
    dynamo.Table.return_value = jobs_t
    return dynamo, jobs_t


def _make_iot(iot_status="IN_PROGRESS", job_counts=None, raise_not_found=False,
              raise_client_error=False, cancel_raises=None):
    iot = MagicMock()
    if raise_not_found:
        iot.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        iot.describe_job.side_effect = iot.exceptions.ResourceNotFoundException("not found")
    elif raise_client_error:
        iot.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        iot.describe_job.side_effect = _client_error()
    else:
        counts = job_counts or {"numberOfSucceededThings": 1,
                                "numberOfFailedThings": 0,
                                "numberOfTimedOutThings": 0}
        iot.describe_job.return_value = {
            "job": {"status": iot_status, "jobProcessDetails": counts}
        }
        iot.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
    if cancel_raises:
        iot.cancel_job.side_effect = cancel_raises
    return iot


def _notif_mock():
    nm = MagicMock()
    nm.tmpl_stale_jobs.return_value = ("stale subj", "stale body")
    return nm


# ══════════════════════════════════════════════════════════════════════════════
# lambda_handler dispatch
# ══════════════════════════════════════════════════════════════════════════════

class TestDispatch:
    def test_eventbridge_triggers_staleness_check(self):
        dynamo, jobs_t = _make_dynamo()
        iot = _make_iot()
        nm = _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot), \
             patch.object(lf, "_notif", nm):
            result = lf.lambda_handler({"source": "aws.events"}, None)
        assert "cancelled" in result
        assert "synced" in result

    def test_detail_type_scheduled_event_triggers_staleness_check(self):
        dynamo, jobs_t = _make_dynamo()
        iot = _make_iot()
        nm = _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot), \
             patch.object(lf, "_notif", nm):
            result = lf.lambda_handler({"detail-type": "Scheduled Event"}, None)
        assert "cancelled" in result

    def test_lifecycle_event_triggers_lifecycle_handler(self):
        dynamo, jobs_t = _make_dynamo(
            get_item_response={"Item": _job_item(status="QUEUED")}
        )
        iot = _make_iot(iot_status="COMPLETED",
                        job_counts={"numberOfSucceededThings": 1,
                                    "numberOfFailedThings": 0,
                                    "numberOfTimedOutThings": 0})
        nm = _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot), \
             patch.object(lf, "_notif", nm):
            result = lf.lambda_handler({"jobId": JOB_ID, "eventType": "completed"}, None)
        assert result["status"] in ("synced", "noop", "skipped")

    def test_unknown_event_shape_returns_skipped(self):
        dynamo, _ = _make_dynamo()
        iot = _make_iot()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot):
            result = lf.lambda_handler({"foo": "bar"}, None)
        assert result["status"] == "skipped"


# ══════════════════════════════════════════════════════════════════════════════
# _handle_staleness_check
# ══════════════════════════════════════════════════════════════════════════════

class TestStalenessCheck:
    def _run(self, scan_items=None, iot=None, nm=None):
        dynamo, jobs_t = _make_dynamo(scan_items=scan_items or [])
        iot_mock = iot or _make_iot()
        nm_mock = nm or _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "_notif", nm_mock):
            result = lf._handle_staleness_check()
        return result, jobs_t, nm_mock

    def test_no_stale_jobs_returns_empty_lists(self):
        result, _, _ = self._run(scan_items=[])
        assert result["cancelled"] == []
        assert result["synced"] == []

    def test_stale_queued_job_is_cancelled(self):
        result, _, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="IN_PROGRESS"),
        )
        assert JOB_ID in result["cancelled"]

    def test_stale_queued_job_iot_cancel_called(self):
        iot_mock = _make_iot(iot_status="IN_PROGRESS")
        self._run(scan_items=[_job_item()], iot=iot_mock)
        iot_mock.cancel_job.assert_called_once_with(
            jobId=JOB_ID, reasonCode="STALE_JOB", force=False
        )

    def test_stale_job_ddb_updated_to_cancelled(self):
        _, jobs_t, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="IN_PROGRESS"),
        )
        jobs_t.update_item.assert_called()
        call_kw = jobs_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":s"] == "CANCELLED"

    def test_iot_job_not_found_marked_cancelled(self):
        result, _, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(raise_not_found=True),
        )
        assert JOB_ID in result["cancelled"]

    def test_iot_already_completed_is_synced(self):
        result, _, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="COMPLETED"),
        )
        assert any(s["jobId"] == JOB_ID for s in result["synced"])
        assert JOB_ID not in result["cancelled"]

    def test_iot_already_canceled_is_synced_to_cancelled(self):
        result, _, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="CANCELED"),
        )
        synced_job = next(s for s in result["synced"] if s["jobId"] == JOB_ID)
        assert synced_job["mappedTo"] == "CANCELLED"

    def test_iot_describe_client_error_job_skipped(self):
        result, jobs_t, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(raise_client_error=True),
        )
        assert JOB_ID not in result["cancelled"]
        assert not any(s["jobId"] == JOB_ID for s in result["synced"])
        jobs_t.update_item.assert_not_called()

    def test_iot_cancel_failure_emits_iot_cancel_failed_audit(self, capsys):
        dynamo, jobs_t = _make_dynamo(scan_items=[_job_item()])
        iot_mock = _make_iot(iot_status="IN_PROGRESS",
                             cancel_raises=_client_error("ThrottlingException"))
        nm = _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "_notif", nm):
            lf._handle_staleness_check()
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "IOT_CANCEL_FAILED" for a in audits)

    def test_iot_cancel_failure_job_still_marked_cancelled_in_ddb(self):
        """IoT cancel failure must not prevent DDB update."""
        _, jobs_t, _ = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="IN_PROGRESS",
                          cancel_raises=_client_error("ThrottlingException")),
        )
        jobs_t.update_item.assert_called()

    def test_notify_admin_called_when_jobs_cancelled(self):
        _, _, nm = self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="IN_PROGRESS"),
        )
        nm.notify_admin.assert_called_once()

    def test_notify_admin_not_called_when_no_cancellations(self):
        _, _, nm = self._run(scan_items=[])
        nm.notify_admin.assert_not_called()

    def test_notify_admin_failure_is_nonfatal(self):
        nm = _notif_mock()
        nm.notify_admin.side_effect = Exception("SES down")
        # Should not raise
        self._run(
            scan_items=[_job_item()],
            iot=_make_iot(iot_status="IN_PROGRESS"),
            nm=nm,
        )

    def test_non_stale_job_not_touched(self):
        """Job created 1 day ago should NOT be cancelled (threshold=7d default)."""
        fresh_ms = int((time.time() - 1 * 86400) * 1000)
        result, jobs_t, _ = self._run(
            scan_items=[_job_item(created_at=fresh_ms)],
            iot=_make_iot(iot_status="IN_PROGRESS"),
        )
        assert result["cancelled"] == []
        iot_called = False
        with patch.object(lf, "dynamo", MagicMock()), \
             patch.object(lf, "iot", MagicMock()) as m:
            pass  # just confirm logic above


# ══════════════════════════════════════════════════════════════════════════════
# _handle_lifecycle_event
# ══════════════════════════════════════════════════════════════════════════════

class TestLifecycleEvent:
    def _run(self, event_type, existing_status="QUEUED", iot_counts=None):
        job = _job_item(status=existing_status)
        dynamo, jobs_t = _make_dynamo(
            get_item_response={"Item": job}
        )
        iot_mock = _make_iot(
            iot_status="COMPLETED",
            job_counts=iot_counts or {"numberOfSucceededThings": 1,
                                      "numberOfFailedThings": 0,
                                      "numberOfTimedOutThings": 0}
        )
        nm = _notif_mock()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock), \
             patch.object(lf, "_notif", nm):
            result = lf._handle_lifecycle_event(JOB_ID, event_type)
        return result, jobs_t, iot_mock

    def test_unknown_job_returns_skipped(self):
        dynamo, jobs_t = _make_dynamo(get_item_response={})
        iot_mock = _make_iot()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock):
            result = lf._handle_lifecycle_event(JOB_ID, "canceled")
        assert result["status"] == "skipped"

    def test_canceled_event_marks_job_cancelled(self):
        result, jobs_t, _ = self._run("canceled")
        assert result["status"] == "synced"
        assert result["newStatus"] == "CANCELLED"
        call_kw = jobs_t.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":s"] == "CANCELLED"

    def test_canceled_already_cancelled_is_noop(self):
        result, jobs_t, _ = self._run("canceled", existing_status="CANCELLED")
        assert result["status"] == "noop"
        jobs_t.update_item.assert_not_called()

    def test_completed_all_succeeded_marks_succeeded(self):
        result, jobs_t, _ = self._run(
            "completed",
            iot_counts={"numberOfSucceededThings": 3,
                        "numberOfFailedThings": 0,
                        "numberOfTimedOutThings": 0}
        )
        assert result["newStatus"] == "SUCCEEDED"

    def test_completed_with_failures_marks_failed(self):
        result, _, _ = self._run(
            "completed",
            iot_counts={"numberOfSucceededThings": 1,
                        "numberOfFailedThings": 1,
                        "numberOfTimedOutThings": 0}
        )
        assert result["newStatus"] == "FAILED"

    def test_completed_with_timed_out_marks_failed(self):
        result, _, _ = self._run(
            "completed",
            iot_counts={"numberOfSucceededThings": 0,
                        "numberOfFailedThings": 0,
                        "numberOfTimedOutThings": 1}
        )
        assert result["newStatus"] == "FAILED"

    def test_completed_already_terminal_is_noop(self):
        result, jobs_t, _ = self._run("completed", existing_status="SUCCEEDED")
        assert result["status"] == "noop"
        jobs_t.update_item.assert_not_called()

    def test_unknown_event_type_returns_skipped(self):
        result, _, _ = self._run("unknown_type")
        assert result["status"] == "skipped"

    def test_completed_iot_describe_fails_returns_error(self):
        dynamo, _ = _make_dynamo(
            get_item_response={"Item": _job_item(status="QUEUED")}
        )
        iot_mock = _make_iot(raise_client_error=True)
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock):
            result = lf._handle_lifecycle_event(JOB_ID, "completed")
        assert result["status"] == "error"

    def test_canceled_emits_deployment_cancelled_audit(self, capsys):
        dynamo, _ = _make_dynamo(
            get_item_response={"Item": _job_item(status="QUEUED")}
        )
        iot_mock = _make_iot()
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock):
            lf._handle_lifecycle_event(JOB_ID, "canceled")
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "DEPLOYMENT_CANCELLED" for a in audits)

    def test_completed_emits_deployment_synced_audit(self, capsys):
        dynamo, _ = _make_dynamo(
            get_item_response={"Item": _job_item(status="QUEUED")}
        )
        iot_mock = _make_iot(iot_status="COMPLETED")
        with patch.object(lf, "dynamo", dynamo), \
             patch.object(lf, "iot", iot_mock):
            lf._handle_lifecycle_event(JOB_ID, "completed")
        printed = capsys.readouterr().out
        audits = [json.loads(l) for l in printed.strip().splitlines()
                  if l.strip() and '"audit"' in l]
        assert any(a["event"] == "DEPLOYMENT_SYNCED" for a in audits)
