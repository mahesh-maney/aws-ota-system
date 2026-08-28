"""
digilux_ota_job_sync
Keeps digilux_ota_jobs DynamoDB in sync with IoT Core job statuses.

Triggered two ways:

1. EventBridge scheduled rule (daily)  — staleness check
   Scans for QUEUED/IN_PROGRESS jobs older than STALE_JOB_DAYS days.
   For each stale job:
     - If IoT already shows a terminal state → sync DDB
     - If IoT still shows it active       → cancel IoT job + mark CANCELLED

2. IoT Rule on $aws/events/job/+/+  — lifecycle event sync
   When a job is cancelled or completed directly in IoT Core
   (console, CLI, etc.) the DDB record is synced immediately.
   IoT Rule SQL injects jobId and eventType into the event payload.
"""
import datetime
import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION         = os.environ["REGION"]
OTA_JOBS_TABLE = os.environ.get("OTA_JOBS_TABLE", "digilux_ota_jobs")
STALE_JOB_DAYS = int(os.environ.get("STALE_JOB_DAYS", "7"))

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot", region_name=REGION)


# ── Helpers ───────────────────────────────────────────────────────────────────

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        import decimal
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _audit(event_name: str, actor: str, resource: dict, result: str, **extra) -> None:
    print(json.dumps({
        "audit":    True,
        "event":    event_name,
        "ts":       datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor":    actor,
        "resource": resource,
        "result":   result,
        **extra,
    }, cls=_DecimalEncoder))


def _update_job_status(jobs_table, job_id: str, status: str,
                       now_ms: int, actor: str, reason: str) -> None:
    jobs_table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET #s = :s, syncedAt = :ts, syncedBy = :by, syncReason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s":  status,
            ":ts": now_ms,
            ":by": actor,
            ":r":  reason,
        },
    )
    log.info(json.dumps({
        "msg": "job_status_updated",
        "jobId": job_id, "status": status, "actor": actor, "reason": reason,
    }))


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    log.info(json.dumps({"msg": "sync_triggered", "event_keys": list(event.keys())}))

    # EventBridge scheduled event
    if event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event":
        return _handle_staleness_check()

    # IoT Rule lifecycle event — jobId and eventType injected by Rule SQL
    job_id     = event.get("jobId")
    event_type = event.get("eventType")   # "canceled" or "completed"

    if job_id and event_type:
        return _handle_lifecycle_event(job_id, event_type)

    log.warning(f"Unrecognised event shape — skipping. keys={list(event.keys())}")
    return {"status": "skipped"}


# ── Trigger 1: Staleness check (scheduled daily) ──────────────────────────────

def _handle_staleness_check() -> dict:
    """
    Scan digilux_ota_jobs for QUEUED/IN_PROGRESS jobs older than STALE_JOB_DAYS.
    Cancel or sync each one.
    """
    from boto3.dynamodb.conditions import Attr

    now_ms     = int(time.time() * 1000)
    cutoff_ms  = now_ms - (STALE_JOB_DAYS * 24 * 60 * 60 * 1000)
    jobs_table = dynamo.Table(OTA_JOBS_TABLE)

    result     = jobs_table.scan(
        FilterExpression=Attr("status").is_in(["QUEUED", "IN_PROGRESS"])
    )
    stale_jobs = [j for j in result.get("Items", [])
                  if (j.get("createdAt") or 0) < cutoff_ms]

    log.info(json.dumps({
        "msg": "staleness_check_start",
        "staleCount": len(stale_jobs),
        "thresholdDays": STALE_JOB_DAYS,
    }))

    cancelled = []
    synced    = []

    for job in stale_jobs:
        job_id   = job["jobId"]
        age_days = (now_ms - (job.get("createdAt") or 0)) / (1000 * 86400)

        # Check IoT Core status
        try:
            iot_job    = iot.describe_job(jobId=job_id)["job"]
            iot_status = iot_job.get("status", "")
        except iot.exceptions.ResourceNotFoundException:
            log.warning(f"IoT job {job_id} not found — marking CANCELLED in DDB")
            _update_job_status(jobs_table, job_id, "CANCELLED", now_ms,
                               "system", f"IoT job not found during staleness check (age: {age_days:.1f}d)")
            cancelled.append(job_id)
            _audit("DEPLOYMENT_CANCELLED", "system", {"jobId": job_id}, "SUCCESS",
                   trigger="STALENESS_CHECK", reason="IoT job not found", ageDays=round(age_days, 1))
            continue
        except ClientError as e:
            log.error(f"Failed to describe IoT job {job_id}: {e}")
            continue

        # IoT already terminal — just sync DDB
        if iot_status in ("COMPLETED", "CANCELED", "DELETION_IN_PROGRESS"):
            mapped = "CANCELLED" if iot_status == "CANCELED" else "SUCCEEDED"
            _update_job_status(jobs_table, job_id, mapped, now_ms,
                               "system", f"Synced from IoT status={iot_status} during staleness check")
            synced.append({"jobId": job_id, "iotStatus": iot_status, "mappedTo": mapped})
            _audit("DEPLOYMENT_SYNCED", "system", {"jobId": job_id}, "SUCCESS",
                   trigger="STALENESS_CHECK", iotStatus=iot_status, mappedStatus=mapped)
            continue

        # IoT still active and job is stale — cancel it
        log.info(f"Auto-cancelling stale job {job_id} (age: {age_days:.1f}d, IoT: {iot_status})")
        try:
            iot.cancel_job(jobId=job_id, reasonCode="STALE_JOB", force=False)
        except ClientError as e:
            log.error(f"Failed to cancel IoT job {job_id}: {e}")

        reason = f"Auto-cancelled: no activity for {age_days:.1f} days (threshold: {STALE_JOB_DAYS}d)"
        _update_job_status(jobs_table, job_id, "CANCELLED", now_ms, "system", reason)
        cancelled.append(job_id)
        _audit("DEPLOYMENT_CANCELLED", "system", {"jobId": job_id}, "SUCCESS",
               trigger="STALENESS_CHECK", ageDays=round(age_days, 1),
               thresholdDays=STALE_JOB_DAYS, iotStatus=iot_status)

    log.info(json.dumps({
        "msg": "staleness_check_complete",
        "cancelled": len(cancelled), "synced": len(synced),
    }))
    return {"cancelled": cancelled, "synced": synced}


# ── Trigger 2: IoT job lifecycle event ────────────────────────────────────────

def _handle_lifecycle_event(job_id: str, event_type: str) -> dict:
    """
    Sync DDB immediately when IoT Core fires a job lifecycle event.
    Only updates jobs that exist in digilux_ota_jobs (ignores unrelated IoT jobs).
    """
    jobs_table = dynamo.Table(OTA_JOBS_TABLE)
    existing   = jobs_table.get_item(Key={"jobId": job_id}).get("Item")

    if not existing:
        log.info(f"Job {job_id} not in {OTA_JOBS_TABLE} — skipping lifecycle sync")
        return {"status": "skipped", "jobId": job_id}

    current_status = existing.get("status", "")
    now_ms         = int(time.time() * 1000)
    resource       = {
        "jobId":       job_id,
        "packageName": existing.get("packageName"),
        "version":     existing.get("version"),
    }

    # ── canceled ──────────────────────────────────────────────────────────────
    if event_type == "canceled":
        if current_status == "CANCELLED":
            log.info(f"Job {job_id} already CANCELLED — noop")
            return {"status": "noop"}

        _update_job_status(jobs_table, job_id, "CANCELLED", now_ms,
                           "system", "Cancelled directly in IoT Core (console/CLI/API outside OTA system)")
        _audit("DEPLOYMENT_CANCELLED", "system", resource, "SUCCESS",
               trigger="IOT_LIFECYCLE_EVENT", eventType=event_type,
               previousStatus=current_status)
        return {"status": "synced", "jobId": job_id, "newStatus": "CANCELLED"}

    # ── completed ─────────────────────────────────────────────────────────────
    if event_type == "completed":
        if current_status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            log.info(f"Job {job_id} already terminal ({current_status}) — noop")
            return {"status": "noop"}

        # Inspect execution counts to determine true outcome
        try:
            iot_job  = iot.describe_job(jobId=job_id)["job"]
            counts   = iot_job.get("jobProcessDetails", {})
            failed   = counts.get("numberOfFailedThings",   0)
            timed_out = counts.get("numberOfTimedOutThings", 0)
            succeeded = counts.get("numberOfSucceededThings", 0)
        except ClientError as e:
            log.error(f"Failed to describe IoT job {job_id} on completed event: {e}")
            return {"status": "error"}

        mapped = "SUCCEEDED" if (failed == 0 and timed_out == 0 and succeeded > 0) else "FAILED"
        reason = (f"Synced from IoT completed event "
                  f"(succeeded={succeeded}, failed={failed}, timedOut={timed_out})")
        _update_job_status(jobs_table, job_id, mapped, now_ms, "system", reason)
        _audit("DEPLOYMENT_SYNCED", "system", resource, "SUCCESS",
               trigger="IOT_LIFECYCLE_EVENT", eventType=event_type,
               mappedStatus=mapped, succeededThings=succeeded,
               failedThings=failed, timedOutThings=timed_out)
        return {"status": "synced", "jobId": job_id, "newStatus": mapped}

    log.warning(f"Unknown lifecycle eventType={event_type} for job {job_id} — skipping")
    return {"status": "skipped"}
