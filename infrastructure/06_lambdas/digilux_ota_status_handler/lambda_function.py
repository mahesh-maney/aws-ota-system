"""
digilux_ota_status_handler
Triggered by IoT Rule when device publishes a job status update.
Topic: iot/device/+/ota/status
Message: {
  "jobId": "...",
  "deviceId": "...",
  "thingName": "...",
  "status": "IN_PROGRESS" | "SUCCEEDED" | "FAILED" | "REJECTED",
  "progress": 0-100,
  "statusDetail": "...",
  "packageName": "...",
  "version": "...",
  "error": "..."    # optional, on FAILED
}
"""
import datetime
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Attr

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ["REGION"]
INVENTORY_TABLE = os.environ.get("INVENTORY_TABLE", "digilux_device_inventory")
OTA_JOBS_TABLE  = os.environ.get("OTA_JOBS_TABLE", "digilux_ota_jobs")

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot", region_name=REGION)


def _audit(event: str, actor: str, resource: dict, result: str, **extra) -> None:
    print(json.dumps({
        "audit": True,
        "event": event,
        "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor": actor,
        "resource": resource,
        "result": result,
        **extra,
    }))


def lambda_handler(event, context):
    """
    event is the MQTT message payload injected by the IoT Rule SQL:
    SELECT *, topic(3) AS deviceId FROM 'iot/device/+/ota/status'
    """
    log.debug(json.dumps({"msg": "raw_status_event", "payload": event}))

    try:
        job_id     = event.get("jobId")
        device_id  = event.get("deviceId")
        thing_name = event.get("thingName")
        status     = event.get("status", "").upper()
        progress   = int(event.get("progress", 0))
        status_detail = event.get("statusDetail", "")
        pkg_name   = event.get("packageName", "")
        version    = event.get("version", "")
        error_msg  = event.get("error", "")

        log.info(json.dumps({
            "msg": "device_status_received",
            "deviceId": device_id, "thingName": thing_name,
            "jobId": job_id, "status": status,
            "progress": progress, "packageName": pkg_name, "version": version,
        }))

        if not all([job_id, device_id, status]):
            log.warning(json.dumps({
                "msg": "status_event_missing_fields",
                "jobId": job_id, "deviceId": device_id, "status": status,
            }))
            return

        now_ms = int(time.time() * 1000)
        device_key = thing_name or device_id

        log.debug(f"Updating deviceStatuses[{device_key}] in job {job_id}")
        jobs_table = dynamo.Table(OTA_JOBS_TABLE)
        aggregate  = _aggregate_status(job_id, device_key, status)
        jobs_table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET deviceStatuses.#dn = :ds, "
                "#jstatus = :js, "
                "lastUpdatedAt = :ts"
            ),
            ExpressionAttributeNames={
                "#dn": device_key,
                "#jstatus": "status",
            },
            ExpressionAttributeValues={
                ":ds": {
                    "status": status,
                    "progress": progress,
                    "statusDetail": status_detail,
                    "error": error_msg,
                    "updatedAt": now_ms,
                },
                ":js": aggregate,
                ":ts": now_ms,
            },
        )
        log.info(f"Job {job_id} aggregate status → {aggregate} (device {device_key} reported {status})")

        if status in ("SUCCEEDED", "FAILED"):
            inv_table = dynamo.Table(INVENTORY_TABLE)

            if status == "SUCCEEDED":
                log.info(json.dumps({
                    "msg": "device_update_succeeded",
                    "deviceId": device_id, "jobId": job_id,
                    "packageName": pkg_name, "version": version,
                }))
                inv_table.update_item(
                    Key={"deviceId": device_id},
                    UpdateExpression=(
                        "SET installedVersions.#pkg = :ver, "
                        "pendingJobId = :null, "
                        "lastUpdatedAt = :ts"
                    ),
                    ExpressionAttributeNames={"#pkg": pkg_name},
                    ExpressionAttributeValues={
                        ":ver": version, ":null": None, ":ts": now_ms,
                    },
                )
                log.info(f"Inventory updated: device={device_id} {pkg_name}={version}, pendingJobId cleared")

                _audit("DEVICE_UPDATE_SUCCEEDED",
                       f"device:{device_id}",
                       {"deviceId": device_id, "jobId": job_id},
                       "SUCCESS",
                       packageName=pkg_name, version=version, thingName=thing_name)

            else:
                log.warning(json.dumps({
                    "msg": "device_update_failed",
                    "deviceId": device_id, "jobId": job_id,
                    "packageName": pkg_name, "version": version,
                    "error": error_msg, "statusDetail": status_detail,
                }))
                inv_table.update_item(
                    Key={"deviceId": device_id},
                    UpdateExpression="SET pendingJobId = :null, lastUpdatedAt = :ts",
                    ExpressionAttributeValues={":null": None, ":ts": now_ms},
                )
                log.info(f"Inventory pendingJobId cleared for device={device_id} after FAILED")

                _audit("DEVICE_UPDATE_FAILED",
                       f"device:{device_id}",
                       {"deviceId": device_id, "jobId": job_id},
                       "FAILURE",
                       packageName=pkg_name, version=version,
                       thingName=thing_name, error=error_msg,
                       statusDetail=status_detail,
                       needsRecovery="NEEDS_RECOVERY" in (status_detail or ""))

            jobs_table.update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :s, completedAt = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status, ":ts": now_ms},
            )
            log.info(f"Job {job_id} marked {status} in {OTA_JOBS_TABLE}")

        elif status == "IN_PROGRESS":
            log.debug(f"Device {device_id} in-progress at {progress}% for job {job_id}")

    except Exception as e:
        log.exception(f"ERROR in status_handler: {e}")
        # Do not re-raise — prevents IoT Rule from retrying indefinitely


def _aggregate_status(job_id: str, reporting_thing: str, new_status: str) -> str:
    """
    Derive overall job status from individual device statuses.
    Returns the current best aggregate: if any device is IN_PROGRESS → IN_PROGRESS,
    if all SUCCEEDED → SUCCEEDED, if any FAILED → FAILED.
    """
    try:
        item = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item", {})
        statuses = {k: v["status"] for k, v in item.get("deviceStatuses", {}).items()}
        statuses[reporting_thing] = new_status

        all_vals = set(statuses.values())
        if "FAILED" in all_vals:
            return "FAILED"
        if "IN_PROGRESS" in all_vals:
            return "IN_PROGRESS"
        if all_vals == {"SUCCEEDED"}:
            return "SUCCEEDED"
        return item.get("status", "IN_PROGRESS")
    except Exception:
        return "IN_PROGRESS"
