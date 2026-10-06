"""
digilux_ota_job_create
Creates an OTA deployment.

Routes (admin-only):
  POST /api/v1/ota/deployments              — create deployment
  GET  /api/v1/ota/deployments              — list deployments
  GET  /api/v1/ota/deployments/{jobId}      — deployment detail
  POST /api/v1/ota/deployments/{jobId}/abort — abort deployment

Body (create):
{
  "packageName":  "HomeAssistantUtility",
  "version":      "4.5.0",
  "rolloutStage": "BETA" | "PRODUCTION" | "CUSTOM",
  "targetIds":    ["deviceId-1", "deviceId-2"]   # BETA / CUSTOM only
}

Architecture (deployment-centric):
  DEPLOYMENT   = campaign / offer — stored in digilux_ota_deployments
  CONSENT      = per-device YES decision — stored in digilux_ota_user_consents
  IOT JOB      = per-device execution — stored in digilux_ota_jobs

  1. Validate package is ACTIVE.
  2. Supersede any existing ACTIVE deployment for same package+stage.
  3. Create deployment record (status=ACTIVE) in digilux_ota_deployments.
  4. Return deployment info.

Key rules:
  - No AWAITING_CONSENT at deployment level — deployments start ACTIVE.
  - No pre-created consent records — consent is written only when user taps YES.
  - DECLINED state does not exist — user taps NO → audit log only, nothing written.
  - One active deployment per package per stage at any time.
  - BETA always uses DEVICE_LIST (explicit targetIds).
  - PRODUCTION always targets DGX-Production THING_GROUP.
  - CUSTOM uses explicit DEVICE_LIST.
  - Superseding: old deployment → CANCELLED + audit log; consent records untouched.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTOR = "job_create"

REGION             = os.environ["REGION"]
ACCOUNT_ID         = os.environ["ACCOUNT_ID"]
PACKAGES_TABLE     = os.environ.get("PACKAGES_TABLE",     "digilux_ota_packages")
DEVICE_DATA_TABLE  = os.environ.get("DEVICE_DATA_TABLE",  "digilux_device_data")
OTA_JOBS_TABLE     = os.environ.get("OTA_JOBS_TABLE",     "digilux_ota_jobs")
CONSENTS_TABLE     = os.environ.get("CONSENTS_TABLE",     "digilux_ota_user_consents")
BETA_USERS_TABLE   = os.environ.get("BETA_USERS_TABLE",   "digilux_ota_beta_users")
DEPLOYMENTS_TABLE  = os.environ.get("DEPLOYMENTS_TABLE",  "digilux_ota_deployments")
PRODUCTION_GROUP   = os.environ.get("PRODUCTION_GROUP",   "DGX-Gateways")

# GSI names
DEPLOYMENTS_PKG_STATUS_INDEX = os.environ.get("DEPLOYMENTS_PKG_STATUS_INDEX", "packageName-status-index")
DEPLOYMENTS_STATUS_INDEX     = os.environ.get("DEPLOYMENTS_STATUS_INDEX",     "status-createdAt-index")
CONSENTS_DEPLOYMENT_INDEX    = os.environ.get("CONSENTS_DEPLOYMENT_INDEX",    "deploymentId-index")

MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "200"))

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot",       region_name=REGION)


# ──────────────────────────────────────────────────────────────────────────────
# Logging helpers
# ──────────────────────────────────────────────────────────────────────────────

def _audit(event: str, actor: str, resource: dict, result: str, **extra) -> None:
    print(json.dumps({
        "audit":    True,
        "event":    event,
        "ts":       datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor":    actor,
        "resource": resource,
        "result":   result,
        **extra,
    }, default=str))


def _log(level: str, msg: str, **fields) -> None:
    record = {"msg": msg, **fields}
    getattr(log, level)(json.dumps(record, default=str))


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Deployment helpers
# ──────────────────────────────────────────────────────────────────────────────

def _find_active_deployment(pkg_name: str, rollout_stage: str) -> dict | None:
    """
    Find the single ACTIVE deployment for a given package+stage.
    Queries packageName-status-index GSI for packageName=X AND status=ACTIVE,
    then filters by rolloutStage. There should be at most one per stage.
    """
    _log("debug", "find_active_deployment",
         packageName=pkg_name, rolloutStage=rollout_stage)
    try:
        resp = dynamo.Table(DEPLOYMENTS_TABLE).query(
            IndexName=DEPLOYMENTS_PKG_STATUS_INDEX,
            KeyConditionExpression=(
                Key("packageName").eq(pkg_name) & Key("status").eq("ACTIVE")
            ),
            FilterExpression=Attr("rolloutStage").eq(rollout_stage),
        )
        items = resp.get("Items", [])
        if items:
            _log("debug", "find_active_deployment_found",
                 packageName=pkg_name, rolloutStage=rollout_stage,
                 deploymentId=items[0].get("deploymentId"),
                 count=len(items))
        return items[0] if items else None
    except Exception as e:
        _log("warning", "find_active_deployment_failed",
             packageName=pkg_name, rolloutStage=rollout_stage, error=str(e))
        return None


def _supersede_deployment(existing_dep: dict, new_dep_id: str,
                           actor: str, now_ms: int) -> None:
    """
    Mark an existing ACTIVE deployment as CANCELLED (superseded by new_dep_id).
    Per architecture: consent records are never touched — ACCEPTED consents and
    their linked IoT jobs remain valid. DECLINED state does not exist.
    """
    old_dep_id = existing_dep["deploymentId"]
    _log("info", "supersede_deployment_start",
         oldDeploymentId=old_dep_id, newDeploymentId=new_dep_id,
         packageName=existing_dep.get("packageName"),
         version=existing_dep.get("version"),
         rolloutStage=existing_dep.get("rolloutStage"),
         supersededBy=actor)

    try:
        dynamo.Table(DEPLOYMENTS_TABLE).update_item(
            Key={"deploymentId": old_dep_id},
            UpdateExpression=(
                "SET #s = :s, cancelledAt = :ts, cancelledBy = :by, "
                "cancelledReason = :r, supersededBy = :new"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":   "CANCELLED",
                ":ts":  now_ms,
                ":by":  actor,
                ":r":   f"SUPERSEDED_BY_{new_dep_id}",
                ":new": new_dep_id,
            },
        )
        _log("info", "supersede_old_deployment_cancelled",
             oldDeploymentId=old_dep_id, supersededBy=new_dep_id, actor=actor)
        _audit("DEPLOYMENT_SUPERSEDED", actor,
               {"deploymentId": old_dep_id,
                "packageName": existing_dep.get("packageName"),
                "version": existing_dep.get("version")},
               "SUCCESS",
               supersededBy=new_dep_id,
               rolloutStage=existing_dep.get("rolloutStage"),
               detail="Consent records untouched — ACCEPTED consents remain valid per architecture")

        # Clean up pointers so devices stop being offered the old version.
        # The new deployment's create flow writes fresh pointers immediately after.
        old_stage = existing_dep.get("rolloutStage", "")
        if old_stage == "CUSTOM":
            old_target_ids = existing_dep.get("targetIds") or []
            if old_target_ids:
                _delete_custom_pointers(
                    existing_dep["packageName"], old_target_ids, old_dep_id
                )
                _log("info", "custom_pointers_cleaned_on_supersede",
                     oldDeploymentId=old_dep_id,
                     deviceCount=len(old_target_ids))
        elif old_stage in _STAGE_POINTER_KEY:
            # For PROD/BETA supersede: the new deployment's _write_stage_pointer
            # will overwrite the pointer atomically, so this is a no-op in practice.
            # Called here for safety in case of partial failures.
            _delete_stage_pointer(existing_dep["packageName"], old_stage, old_dep_id)
    except Exception as e:
        _log("error", "supersede_deployment_cancel_failed",
             oldDeploymentId=old_dep_id, error=str(e))
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Device resolution helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_device_by_id(device_id: str) -> dict | None:
    _log("debug", "device_lookup", deviceId=device_id)
    items = dynamo.Table(DEVICE_DATA_TABLE).query(
        KeyConditionExpression=Key("deviceId").eq(device_id)
    ).get("Items", [])
    return items[0] if items else None


def _resolve_device_list(device_ids: list[str], rollout_stage: str,
                          caller: str) -> list[dict]:
    """
    Resolve a list of device IDs to {deviceId, userId, thingName, macAddress}.
    Resolves devices in parallel using ThreadPoolExecutor (max 20 workers).
    Skips devices not found or without userId (logs warning for each).
    Hard limit: MAX_DEVICES devices per deployment.
    """
    _log("info", "resolve_device_list_start",
         rolloutStage=rollout_stage, idCount=len(device_ids))
    device_table = dynamo.Table(DEVICE_DATA_TABLE)
    devices: list[dict] = []
    lock = threading.Lock()

    def _resolve_one(device_id: str) -> None:
        items = device_table.query(
            KeyConditionExpression=Key("deviceId").eq(device_id)
        ).get("Items", [])

        if not items:
            _log("warning", "device_not_found_skipped",
                 deviceId=device_id, rolloutStage=rollout_stage,
                 detail="DeviceId not found in device_data — skipping")
            _audit("DEVICE_SKIPPED_NOT_FOUND", caller,
                   {"deviceId": device_id}, "WARN", rolloutStage=rollout_stage)
            return

        item    = items[0]
        user_id = item.get("userId")

        if not user_id:
            _log("warning", "device_no_userid_skipped",
                 deviceId=device_id, rolloutStage=rollout_stage,
                 thingName=item.get("thingName"),
                 detail="Device has no userId — cannot associate consent")
            _audit("DEVICE_SKIPPED_NO_USERID", caller,
                   {"deviceId": device_id}, "WARN", rolloutStage=rollout_stage)
            return

        _log("debug", "device_resolved",
             deviceId=device_id, userId=user_id, rolloutStage=rollout_stage)
        with lock:
            devices.append({
                "deviceId":   device_id,
                "userId":     user_id,
                "thingName":  item.get("thingName", ""),
                "macAddress": item.get("macAddress", ""),
            })

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(_resolve_one, did) for did in device_ids]
        for future in as_completed(futures):
            future.result()  # propagate exceptions

    _log("info", "resolve_device_list_complete",
         rolloutStage=rollout_stage, requested=len(device_ids),
         resolved=len(devices), skipped=len(device_ids) - len(devices))
    return devices


# ──────────────────────────────────────────────────────────────────────────────
# CUSTOM deployment pointer helpers
# ──────────────────────────────────────────────────────────────────────────────
# CUSTOM deployments target specific device IDs. To make them visible to
# check_updates (which does in-memory pointer lookups), we write a per-device
# pointer item keyed as LATEST#CUSTOM#{deviceId} in the packages table.
# Priority in check_updates: CUSTOM > BETA > PROD.

def _write_custom_pointers(pkg_name: str, version: str, pkg: dict,
                            target_ids: list[str], deployment_id: str) -> None:
    """Write LATEST#CUSTOM#{deviceId} pointer for each target device."""
    tbl = dynamo.Table(PACKAGES_TABLE)
    for device_id in target_ids:
        pointer_key = f"LATEST#CUSTOM#{device_id}"
        try:
            tbl.put_item(Item={
                "packageName":      pkg_name,
                "version":          pointer_key,
                "targetVersion":    version,
                "releaseType":      "CUSTOM",
                "releaseNotes":     pkg.get("releaseNotes", ""),
                "fileName":         pkg.get("fileName", ""),
                "firmwareCategory": pkg.get("firmwareCategory"),
                "tierOverride":     pkg.get("tierOverride"),
                "deploymentId":     deployment_id,
                "deviceId":         device_id,
            })
            _log("debug", "custom_pointer_written",
                 packageName=pkg_name, deviceId=device_id,
                 targetVersion=version, deploymentId=deployment_id)
        except Exception as e:
            _log("warning", "custom_pointer_write_failed",
                 packageName=pkg_name, deviceId=device_id,
                 deploymentId=deployment_id, error=str(e))


def _delete_custom_pointers(pkg_name: str, target_ids: list[str],
                             deployment_id: str) -> None:
    """Delete LATEST#CUSTOM#{deviceId} pointers for a superseded or aborted CUSTOM deployment."""
    tbl = dynamo.Table(PACKAGES_TABLE)
    for device_id in target_ids:
        pointer_key = f"LATEST#CUSTOM#{device_id}"
        try:
            tbl.delete_item(Key={"packageName": pkg_name, "version": pointer_key})
            _log("debug", "custom_pointer_deleted",
                 packageName=pkg_name, deviceId=device_id, deploymentId=deployment_id)
        except Exception as e:
            _log("warning", "custom_pointer_delete_failed",
                 packageName=pkg_name, deviceId=device_id,
                 deploymentId=deployment_id, error=str(e))


# ──────────────────────────────────────────────────────────────────────────────
# PRODUCTION / BETA deployment pointer helpers
# ──────────────────────────────────────────────────────────────────────────────
# PRODUCTION and BETA deployments write a single LATEST#PROD or LATEST#BETA
# pointer item in the packages table. check_updates reads these pointers in its
# pre-loop I/O phase and uses them to serve updates without querying the
# deployments table per device.

_STAGE_POINTER_KEY  = {"PRODUCTION": "LATEST#PROD", "BETA": "LATEST#BETA"}
_STAGE_RELEASE_TYPE = {"PRODUCTION": "PROD",        "BETA": "BETA"}


def _write_stage_pointer(pkg_name: str, version: str, pkg: dict,
                          rollout_stage: str, deployment_id: str) -> None:
    """Write LATEST#PROD or LATEST#BETA pointer for a PRODUCTION or BETA deployment."""
    pointer_key  = _STAGE_POINTER_KEY.get(rollout_stage)
    release_type = _STAGE_RELEASE_TYPE.get(rollout_stage)
    if not pointer_key:
        return
    try:
        dynamo.Table(PACKAGES_TABLE).put_item(Item={
            "packageName":  pkg_name,
            "version":      pointer_key,
            "targetVersion": version,
            "releaseType":  release_type,
            "releaseNotes": pkg.get("releaseNotes", ""),
            "fileName":     pkg.get("fileName", ""),
            "deploymentId": deployment_id,
        })
        _log("info", "stage_pointer_written",
             packageName=pkg_name, pointerKey=pointer_key,
             targetVersion=version, deploymentId=deployment_id)
    except Exception as e:
        _log("warning", "stage_pointer_write_failed",
             packageName=pkg_name, pointerKey=pointer_key,
             deploymentId=deployment_id, error=str(e))


def _delete_stage_pointer(pkg_name: str, rollout_stage: str,
                           deployment_id: str) -> None:
    """Delete LATEST#PROD or LATEST#BETA pointer when a deployment is superseded or aborted.
    Only deletes if the pointer still references this deployment (a supersede
    writes the new pointer first, so this is a no-op for the old deployment).
    """
    pointer_key = _STAGE_POINTER_KEY.get(rollout_stage)
    if not pointer_key:
        return
    try:
        tbl = dynamo.Table(PACKAGES_TABLE)
        current = tbl.get_item(
            Key={"packageName": pkg_name, "version": pointer_key}
        ).get("Item", {})
        if current.get("deploymentId") == deployment_id:
            tbl.delete_item(Key={"packageName": pkg_name, "version": pointer_key})
            _log("info", "stage_pointer_deleted",
                 packageName=pkg_name, pointerKey=pointer_key, deploymentId=deployment_id)
        else:
            _log("debug", "stage_pointer_skip_delete",
                 packageName=pkg_name, pointerKey=pointer_key,
                 reason="pointer already updated by newer deployment",
                 currentDeploymentId=current.get("deploymentId"))
    except Exception as e:
        _log("warning", "stage_pointer_delete_failed",
             packageName=pkg_name, pointerKey=pointer_key,
             deploymentId=deployment_id, error=str(e))


# ──────────────────────────────────────────────────────────────────────────────
# List / Get / Abort
# ──────────────────────────────────────────────────────────────────────────────

def _list_jobs(event: dict) -> dict:
    """
    GET /ota/deployments — list deployments, newest first.
    Uses status-createdAt-index GSI to avoid a full table Scan.
    Supports optional ?status= query param to filter by a single status.
    Without ?status=, queries all four known statuses and merges the results.
    """
    params        = event.get("queryStringParameters") or {}
    limit         = min(int(params.get("limit", 50)), 200)
    status_filter = (params.get("status") or "").upper() or None

    _log("debug", "list_deployments_start", limit=limit, statusFilter=status_filter)

    tbl = dynamo.Table(DEPLOYMENTS_TABLE)
    items: list[dict] = []

    statuses_to_query = (
        [status_filter]
        if status_filter
        else ["ACTIVE", "COMPLETED", "CANCELLED", "FAILED"]
    )

    for status in statuses_to_query:
        try:
            resp = tbl.query(
                IndexName=DEPLOYMENTS_STATUS_INDEX,
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,  # newest first within each status
                Limit=limit,             # cap per-status to avoid over-fetching
            )
            items.extend(resp.get("Items", []))
        except Exception as e:
            _log("warning", "list_deployments_gsi_query_failed",
                 status=status, error=str(e))

    # Merge all statuses, sort newest-first, then trim to final limit
    items.sort(key=lambda x: int(x.get("createdAt", 0)), reverse=True)
    items = items[:limit]

    _log("info", "list_deployments_complete",
         returnedCount=len(items), limit=limit, statusFilter=status_filter)

    jobs = []
    for i in items:
        # targetId for UI: group name for PROD, comma-joined IDs for BETA/CUSTOM
        target_group = i.get("targetGroup") or ""
        target_ids   = i.get("targetIds") or []
        target_id    = target_group or ",".join(str(t) for t in target_ids)

        jobs.append({
            "jobId":        i.get("deploymentId"),        # backward compat with UI
            "deploymentId": i.get("deploymentId"),
            "packageName":  i.get("packageName"),
            "version":      i.get("version"),
            "deviceType":   i.get("deviceType"),
            "targetType":   i.get("targetType"),
            "targetId":     target_id,
            "rolloutStage": i.get("rolloutStage"),
            "status":       i.get("status"),
            "createdBy":    i.get("createdBy"),
            "createdAt":    int(i["createdAt"])    if "createdAt"    in i else None,
            "cancelledAt":  int(i["cancelledAt"])  if "cancelledAt"  in i else None,
            "supersededBy": i.get("supersededBy"),
            "counters":     i.get("counters"),
        })

    return _response(200, {"jobs": jobs, "count": len(jobs)})


def _get_job(deployment_id: str) -> dict:
    """GET /ota/deployments/{jobId} — deployment detail with consent stats."""
    _log("debug", "get_deployment_start", deploymentId=deployment_id)

    item = dynamo.Table(DEPLOYMENTS_TABLE).get_item(
        Key={"deploymentId": deployment_id}
    ).get("Item")

    if not item:
        _log("warning", "get_deployment_not_found", deploymentId=deployment_id)
        return _response(404, {"error": f"Deployment {deployment_id} not found"})

    _log("debug", "get_deployment_found",
         deploymentId=deployment_id, status=item.get("status"),
         packageName=item.get("packageName"), version=item.get("version"))

    # Consent stats from deploymentId-index GSI (only ACCEPTED records exist)
    try:
        resp = dynamo.Table(CONSENTS_TABLE).query(
            IndexName=CONSENTS_DEPLOYMENT_INDEX,
            KeyConditionExpression=Key("deploymentId").eq(deployment_id),
        )
        consents = resp.get("Items", [])
        stats = {"ACCEPTED": sum(1 for c in consents if c.get("status") == "ACCEPTED")}
        item["consentStats"] = stats
        _log("debug", "consent_stats_computed",
             deploymentId=deployment_id, totalConsents=len(consents), stats=stats)
    except Exception as e:
        _log("warning", "consent_stats_fetch_failed",
             deploymentId=deployment_id, error=str(e))

    # Build backward-compat fields for admin UI
    target_group = item.get("targetGroup") or ""
    target_ids   = item.get("targetIds") or []
    item["jobId"]     = deployment_id
    item["targetId"]  = target_group or ",".join(str(t) for t in target_ids)
    item["targetType"] = item.get("targetType", "")

    return _response(200, json.loads(json.dumps(item, cls=_DecimalEncoder)))


def _abort_job(deployment_id: str, claims: dict, body: dict) -> dict:
    """POST /ota/deployments/{jobId}/abort — cancel an active deployment.
    Requires a non-empty `reason` in the request body.
    Blocked if any IoT jobs for this deployment are currently IN_PROGRESS.
    Per architecture: consent records are not touched; ACCEPTED consents remain valid.
    """
    actor = claims.get("email", claims.get("sub", "unknown"))
    _log("info", "abort_deployment_start", deploymentId=deployment_id, requestedBy=actor)

    # ── Require abort reason ──────────────────────────────────────────────────
    reason = str(body.get("reason", "")).strip()
    if not reason:
        _log("warning", "abort_missing_reason",
             deploymentId=deployment_id, requestedBy=actor)
        return _response(400, {"error": "An abort reason is required."})

    try:
        item = dynamo.Table(DEPLOYMENTS_TABLE).get_item(
            Key={"deploymentId": deployment_id}
        ).get("Item")

        if not item:
            _log("warning", "abort_deployment_not_found",
                 deploymentId=deployment_id, requestedBy=actor)
            return _response(404, {"error": f"Deployment {deployment_id} not found"})

        current_status = item.get("status", "")

        if current_status in ("COMPLETED", "CANCELLED", "FAILED"):
            _log("warning", "abort_deployment_already_terminal",
                 deploymentId=deployment_id, currentStatus=current_status,
                 requestedBy=actor)
            return _response(400, {
                "error": f"Cannot abort a {current_status} deployment."
            })

        # ── Block if any jobs for this deployment are IN_PROGRESS ─────────────
        # Query consents by deploymentId to get associated jobIds, then check
        # each job's status. A device mid-download must not be interrupted.
        try:
            consent_resp = dynamo.Table(CONSENTS_TABLE).query(
                IndexName=CONSENTS_DEPLOYMENT_INDEX,
                KeyConditionExpression=Key("deploymentId").eq(deployment_id),
            )
            consents = consent_resp.get("Items", [])
            in_progress_jobs = []
            for c in consents:
                job_id = c.get("jobId")
                if not job_id:
                    continue
                job = dynamo.Table(OTA_JOBS_TABLE).get_item(
                    Key={"jobId": job_id}
                ).get("Item")
                if job and job.get("status") == "IN_PROGRESS":
                    in_progress_jobs.append(job_id)

            if in_progress_jobs:
                _log("warning", "abort_blocked_by_in_progress_jobs",
                     deploymentId=deployment_id, requestedBy=actor,
                     inProgressJobs=in_progress_jobs)
                return _response(409, {
                    "error": (
                        f"Cannot abort: {len(in_progress_jobs)} device(s) are currently "
                        "downloading this firmware. Wait for them to finish or time out."
                    ),
                    "inProgressJobs": in_progress_jobs,
                })
        except Exception as e:
            _log("warning", "abort_in_progress_check_failed",
                 deploymentId=deployment_id, error=str(e),
                 detail="Proceeding with abort — could not verify job statuses")

        now_ms = int(time.time() * 1000)

        dynamo.Table(DEPLOYMENTS_TABLE).update_item(
            Key={"deploymentId": deployment_id},
            UpdateExpression=(
                "SET #s = :s, cancelledAt = :ts, cancelledBy = :by, cancelledReason = :r"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":  "CANCELLED",
                ":ts": now_ms,
                ":by": actor,
                ":r":  reason,
            },
        )

        _log("info", "abort_deployment_complete",
             deploymentId=deployment_id, requestedBy=actor,
             previousStatus=current_status, reason=reason)
        _audit("DEPLOYMENT_ABORTED", actor,
               {"deploymentId": deployment_id,
                "packageName": item.get("packageName"),
                "version": item.get("version")},
               "SUCCESS",
               previousStatus=current_status,
               rolloutStage=item.get("rolloutStage"),
               reason=reason,
               detail="Consent records untouched — ACCEPTED consents remain valid per architecture")

        # Clean up pointers so devices immediately stop being offered this update.
        abort_stage = item.get("rolloutStage", "")
        if abort_stage == "CUSTOM":
            abort_target_ids = item.get("targetIds") or []
            if abort_target_ids:
                _delete_custom_pointers(
                    item["packageName"], abort_target_ids, deployment_id
                )
                _log("info", "custom_pointers_cleaned_on_abort",
                     deploymentId=deployment_id,
                     deviceCount=len(abort_target_ids))
        elif abort_stage in _STAGE_POINTER_KEY:
            _delete_stage_pointer(item["packageName"], abort_stage, deployment_id)
            _log("info", "stage_pointer_cleaned_on_abort",
                 deploymentId=deployment_id, rolloutStage=abort_stage)

        return _response(200, {
            "jobId":        deployment_id,
            "status":       "CANCELLED",
            "abortedBy":    actor,
            "abortReason":  reason,
        })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "abort_deployment_aws_error",
             deploymentId=deployment_id, awsError=code, awsMessage=msg)
        _audit("DEPLOYMENT_ABORT_FAILED", actor,
               {"deploymentId": deployment_id}, "FAILURE",
               awsError=code, awsMessage=msg)
        return _response(400, {"error": msg})


# ──────────────────────────────────────────────────────────────────────────────
# Lambda handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    method       = event.get("httpMethod", "POST").upper()
    path_params  = event.get("pathParameters") or {}
    job_id_param = path_params.get("jobId")
    request_id   = context.aws_request_id if context else None

    _log("info", "request_received",
         method=method, path=event.get("path", ""),
         jobIdParam=job_id_param, requestId=request_id)

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "ota-admin" not in claims.get("cognito:groups", ""):
            _log("warning", "unauthorized_access_attempt",
                 path=event.get("path", ""), method=method,
                 groups=claims.get("cognito:groups", ""))
            _audit("UNAUTHORIZED_ACCESS", ACTOR,
                   {"path": event.get("path", "")}, "FAILURE",
                   reason="not_in_ota_admin_group",
                   groups=claims.get("cognito:groups", ""))
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))

        # ── Route: GET detail ─────────────────────────────────────────────────
        if method == "GET" and job_id_param:
            return _get_job(job_id_param)

        # ── Route: GET list ───────────────────────────────────────────────────
        if method == "GET":
            return _list_jobs(event)

        # ── Route: POST abort ─────────────────────────────────────────────────
        if method == "POST" and job_id_param:
            abort_body = json.loads(event.get("body") or "{}")
            return _abort_job(job_id_param, claims, abort_body)

        # ── Route: POST create ────────────────────────────────────────────────
        body = json.loads(event.get("body") or "{}")

        for field in ["packageName", "version"]:
            if not body.get(field):
                _log("warning", "missing_required_field", field=field, caller=caller)
                return _response(400, {"error": f"Missing required field: {field}"})

        pkg_name      = body["packageName"].strip()
        version       = body["version"].strip()
        rollout_stage = body.get("rolloutStage", "PRODUCTION").upper()

        VALID_STAGES = {"BETA", "PRODUCTION", "CUSTOM"}
        if rollout_stage not in VALID_STAGES:
            _log("warning", "invalid_rollout_stage",
                 rolloutStage=rollout_stage, validStages=list(VALID_STAGES))
            return _response(400, {
                "error": f"rolloutStage must be one of: {', '.join(sorted(VALID_STAGES))}"
            })

        _log("info", "create_deployment_start",
             packageName=pkg_name, version=version,
             rolloutStage=rollout_stage, caller=caller)

        # ── Validate package is ACTIVE ────────────────────────────────────────
        pkg = dynamo.Table(PACKAGES_TABLE).get_item(
            Key={"packageName": pkg_name, "version": version}
        ).get("Item")

        if not pkg:
            _log("warning", "package_not_found",
                 packageName=pkg_name, version=version, caller=caller)
            return _response(404, {"error": f"Package {pkg_name}@{version} not found"})

        if pkg.get("status") != "ACTIVE":
            _log("warning", "package_not_active",
                 packageName=pkg_name, version=version,
                 currentStatus=pkg.get("status"), caller=caller)
            return _response(400, {
                "error": f"Package {pkg_name}@{version} is not ACTIVE (current status: {pkg.get('status')})"
            })

        _log("info", "package_validated",
             packageName=pkg_name, version=version,
             deviceType=pkg.get("deviceType"),
             artifactSize=pkg.get("artifactSize"))

        # ── Resolve target devices (BETA/CUSTOM only) ─────────────────────────
        devices    = []
        target_type = None
        target_ids  = []
        target_group = None

        if rollout_stage in ("BETA", "CUSTOM"):
            # Support both targetIds array and legacy comma-separated targetId string
            raw_ids = body.get("targetIds") or []
            if not raw_ids and body.get("targetId"):
                raw_ids = [t.strip() for t in body["targetId"].split(",") if t.strip()]

            if not raw_ids:
                _log("warning", "no_target_ids_provided",
                     rolloutStage=rollout_stage, caller=caller)
                return _response(400, {
                    "error": f"No device IDs provided for {rollout_stage} deployment. "
                             "Set targetIds array in request body."
                })

            if len(raw_ids) > MAX_DEVICES:
                _log("warning", "too_many_devices",
                     requested=len(raw_ids), limit=MAX_DEVICES,
                     rolloutStage=rollout_stage, caller=caller)
                return _response(400, {
                    "error": f"Too many devices: {len(raw_ids)} exceeds the limit of {MAX_DEVICES}. "
                             "Split into multiple deployments."
                })

            t_resolve = time.monotonic()
            devices = _resolve_device_list(raw_ids, rollout_stage, caller)
            resolve_ms = int((time.monotonic() - t_resolve) * 1000)

            if not devices:
                _log("warning", "no_valid_devices_resolved",
                     rolloutStage=rollout_stage, requested=len(raw_ids),
                     resolveMs=resolve_ms)
                return _response(400, {
                    "error": "None of the provided device IDs are valid or have registered users."
                })

            # Store exactly the resolved device IDs (not raw input)
            target_ids  = [d["deviceId"] for d in devices]
            target_type = "DEVICE_LIST"
            _log("info", "device_list_resolved",
                 rolloutStage=rollout_stage, requested=len(raw_ids),
                 resolved=len(devices), resolveMs=resolve_ms)

        else:  # PRODUCTION
            target_type  = "THING_GROUP"
            target_group = PRODUCTION_GROUP
            _log("info", "production_deployment_targeting_group",
                 group=PRODUCTION_GROUP,
                 detail="PRODUCTION deployments always target DGX-Production — no device list needed at deploy time")

        # ── Block if an active deployment already exists for this package+stage ─
        # Admin must abort the existing deployment before creating a new one.
        now_ms        = int(time.time() * 1000)
        deployment_id = f"digilux-ota-{pkg_name}-{version}-{int(time.time())}".replace(".", "-")

        existing_dep = _find_active_deployment(pkg_name, rollout_stage)

        if existing_dep:
            existing_dep_id  = existing_dep["deploymentId"]
            existing_version = existing_dep.get("version", "")
            _log("warning", "active_deployment_exists",
                 packageName=pkg_name, rolloutStage=rollout_stage,
                 existingDeploymentId=existing_dep_id,
                 existingVersion=existing_version, caller=caller)
            _audit("DEPLOYMENT_BLOCKED", caller,
                   {"packageName": pkg_name, "version": version},
                   "FAILURE",
                   reason="active_deployment_exists",
                   existingDeploymentId=existing_dep_id,
                   rolloutStage=rollout_stage)
            return _response(409, {
                "error": (
                    f"An active {rollout_stage} deployment already exists for {pkg_name} "
                    f"(v{existing_version}, id={existing_dep_id}). "
                    "Abort it first before creating a new deployment."
                ),
                "activeDeploymentId": existing_dep_id,
                "activeVersion":      existing_version,
            })

        _log("info", "no_existing_active_deployment",
             packageName=pkg_name, rolloutStage=rollout_stage)

        # ── Write deployment record (ACTIVE) ───────────────────────────────────
        deployment_item = {
            "deploymentId": deployment_id,
            "packageName":  pkg_name,
            "version":      version,
            "deviceType":   pkg.get("deviceType", ""),
            "releaseNotes": pkg.get("releaseNotes", ""),
            "rolloutStage": rollout_stage,
            "targetType":   target_type,
            "status":       "ACTIVE",
            "createdAt":    now_ms,
            "createdBy":    caller,
            "counters": {
                "accepted":  0,
                "succeeded": 0,
                "cancelled": 0,
                "failed":    0,
            },
        }
        if target_ids:
            deployment_item["targetIds"] = target_ids
        if target_group:
            deployment_item["targetGroup"] = target_group

        _log("info", "writing_deployment_record",
             deploymentId=deployment_id, packageName=pkg_name, version=version,
             rolloutStage=rollout_stage, targetType=target_type,
             deviceCount=len(devices), caller=caller)

        dynamo.Table(DEPLOYMENTS_TABLE).put_item(Item=deployment_item)

        _log("info", "deployment_record_written",
             deploymentId=deployment_id, status="ACTIVE",
             targetType=target_type,
             detail="Deployment is ACTIVE — no IoT Job at deployment level")

        # Write per-device CUSTOM pointers so check_updates can serve this
        # deployment with highest priority (CUSTOM > BETA > PROD).
        if rollout_stage == "CUSTOM" and target_ids:
            _write_custom_pointers(pkg_name, version, pkg, target_ids, deployment_id)
            _log("info", "custom_pointers_written",
                 deploymentId=deployment_id, deviceCount=len(target_ids))

        # Write LATEST#PROD or LATEST#BETA pointer so check_updates can serve
        # this update to all eligible devices without a per-device DB query.
        if rollout_stage in _STAGE_POINTER_KEY:
            _write_stage_pointer(pkg_name, version, pkg, rollout_stage, deployment_id)

        _audit("DEPLOYMENT_CREATED", caller,
               {"packageName": pkg_name, "version": version},
               "SUCCESS",
               deploymentId=deployment_id,
               rolloutStage=rollout_stage,
               targetType=target_type,
               targetGroup=target_group or None,
               deviceCount=len(devices),
               deviceType=pkg.get("deviceType", ""),
               )

        target_id_response = target_group or ",".join(target_ids)

        msg = f"Deployment created. Devices in {target_id_response} will be offered this update."

        return _response(201, {
            "jobId":        deployment_id,     # backward compat for UI
            "deploymentId": deployment_id,
            "packageName":  pkg_name,
            "version":      version,
            "targetType":   target_type,
            "targetId":     target_id_response,
            "rolloutStage": rollout_stage,
            "status":       "ACTIVE",
            "message":      msg,
        })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "aws_client_error", awsError=code, awsMessage=msg)
        _audit("DEPLOYMENT_AWS_ERROR", ACTOR, {}, "FAILURE",
               awsError=code, awsMessage=msg)
        return _response(500, {"error": f"AWS error: {msg}"})
    except Exception as e:
        _log("error", "unhandled_exception", error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in job_create handler: {e}")
        _audit("DEPLOYMENT_UNHANDLED_ERROR", ACTOR, {}, "FAILURE",
               error=str(e), excType=type(e).__name__)
        return _response(500, {"error": "Internal server error"})
