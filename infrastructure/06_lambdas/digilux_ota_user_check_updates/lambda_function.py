"""
digilux_ota_user_check_updates
Returns available OTA updates for all controller devices owned by the calling user.

GET /api/v1/ota/my/updates

Auth: Cognito ID token (any authenticated user).
The userId is extracted from the JWT `sub` claim.
Only devices registered under that userId are returned.

Optimised architecture (pointer-based, zero per-device DB/IoT calls outside job checks):
  All DB I/O for package availability is done BEFORE the device loop:
    1. Fetch all user devices (one GSI query).
    2. Check if user is a beta user (one GetItem on digilux_ota_beta_users).
    3. Pre-fetch LATEST#BETA / LATEST#PROD pointer items for each unique package
       name (up to 2 GetItem calls per unique package name, outside the loop).
  Device loop is then in-memory (no IoT group calls, no deployment queries):
    a. If device has QUEUED or IN_PROGRESS job    → return JOB_ACTIVE (blocked).
    b. If device has FAILED or TIMED_OUT job      → note as lastFailedJob, continue.
    c. Pointer cache lookup: BETA (beta users) → PROD fallback.
    d. installedVersion == availableVersion        → no update (up to date).
    e. installedVersion >  availableVersion        → no update (never downgrade).
    f. installedVersion <  availableVersion        → entitlement check → offer update.

Response envelope (client contract):
  HTTP 200 + devices empty             → success + NO_UPDATE_MSG
  HTTP 200 + only NOT_REGISTERED       → success + NOT_REGISTERED_MSG + devices
                                         (otaStatus remains NOT_REGISTERED)
  HTTP 200 + REGISTERED / JOB_ACTIVE   → success + message "" + devices
                                         (client reads devices only)
  Non-200                              → failed + error message + devices []
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from collections import defaultdict
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from messages import (
    INTERNAL_ERROR_MSG,
    NO_UPDATE_MSG,
    NOT_REGISTERED_MSG,
    OTA_COMPLETED_MSG,
    OTA_FAILED_MSG,
    OTA_IN_PROGRESS_MSG,
    OTA_TIMED_OUT_MSG,
    UNAUTHORIZED_MSG,
)

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTOR = "check_updates"

REGION                 = os.environ["REGION"]
DEVICE_DATA_TABLE      = os.environ.get("DEVICE_DATA_TABLE",      "digilux_device_data")
PACKAGES_TABLE         = os.environ.get("PACKAGES_TABLE",         "digilux_ota_packages")
OTA_JOBS_TABLE         = os.environ.get("OTA_JOBS_TABLE",         "digilux_ota_jobs")
CONSENTS_TABLE         = os.environ.get("CONSENTS_TABLE",         "digilux_ota_user_consents")
BETA_USERS_TABLE       = os.environ.get("BETA_USERS_TABLE",       "digilux_ota_beta_users")
DEVICE_DATA_USER_INDEX = os.environ.get("DEVICE_DATA_USER_INDEX", "userId-index")
CONSENTS_USER_INDEX    = os.environ.get("CONSENTS_USER_INDEX",    "userId-deviceId-index")
ENTITLEMENT_FUNCTION   = os.environ.get("ENTITLEMENT_FUNCTION",   "digilux_entitlement_check")

dynamo        = boto3.resource("dynamodb", region_name=REGION)
iot           = boto3.client("iot",        region_name=REGION)
lambda_client = boto3.client("lambda",     region_name=REGION)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

class _Dec(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_Dec),
    }


def _success(devices: list) -> dict:
    """
    HTTP 200 envelope: status is always success.
    - devices empty                              → NO_UPDATE_MSG (client shows message)
    - all entries otaStatus=NOT_REGISTERED       → NOT_REGISTERED_MSG (client shows message;
                                                   otaStatus stays NOT_REGISTERED on each device)
    - otherwise (REGISTERED / JOB_ACTIVE / mix)  → message "" (client reads devices only)
    """
    if not devices:
        message = NO_UPDATE_MSG
    elif all(d.get("otaStatus") == "NOT_REGISTERED" for d in devices):
        message = NOT_REGISTERED_MSG
    else:
        message = ""

    return _resp(200, {
        "status":  "success",
        "message": message,
        "devices": devices,
    })


def _failed(http_status: int, message: str) -> dict:
    """Non-200 envelope: status=failed; client shows message."""
    return _resp(http_status, {
        "status":  "failed",
        "message": message,
        "devices": [],
    })


def _log(level: str, msg: str, **fields) -> None:
    record = {"msg": msg, **fields}
    getattr(log, level)(json.dumps(record, default=str))


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


def _version_tuple(v: str):
    result = []
    for part in str(v).split("."):
        try:
            result.append(int(part.split("-")[0]))
        except (ValueError, AttributeError):
            result.append(0)
    return tuple(result) if result else (0,)


def _is_newer(candidate: str, installed: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(installed)


# ──────────────────────────────────────────────────────────────────────────────
# Beta-user check
# ──────────────────────────────────────────────────────────────────────────────

def _is_beta_user(user_id: str) -> bool:
    """Check if the user is enrolled in the beta programme (single GetItem)."""
    try:
        item   = dynamo.Table(BETA_USERS_TABLE).get_item(
            Key={"userId": user_id}
        ).get("Item")
        result = item is not None
        _log("debug", "beta_user_check", userId=user_id, isBeta=result)
        return result
    except Exception as e:
        _log("warning", "beta_user_check_failed",
             userId=user_id, error=str(e),
             detail="Defaulting to non-beta")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Entitlement check
# ──────────────────────────────────────────────────────────────────────────────

def _invoke_entitlement_check(controller_id: str, firmware_category: str | None,
                               tier_override: str | None) -> dict:
    """
    Invoke digilux_entitlement_check (internal Lambda-to-Lambda).
    Fails open on any error — OTA is never blocked by entitlement service outage.
    """
    if not controller_id or not firmware_category:
        _log("debug", "entitlement_check_skipped",
             controllerId=controller_id, firmwareCategory=firmware_category)
        return {"eligible": True, "reason": "missing_input"}

    payload = json.dumps({
        "controllerId":     controller_id,
        "firmwareCategory": firmware_category,
        "tierOverride":     tier_override,
    }).encode()

    t_start = time.monotonic()
    try:
        resp    = lambda_client.invoke(
            FunctionName=ENTITLEMENT_FUNCTION, InvocationType="RequestResponse", Payload=payload
        )
        result  = json.loads(resp["Payload"].read())
        elapsed = int((time.monotonic() - t_start) * 1000)

        if resp.get("FunctionError"):
            _log("warning", "entitlement_function_error",
                 controllerId=controller_id, elapsedMs=elapsed,
                 detail="Failing open")
            return {"eligible": True, "reason": "entitlement_function_error"}

        _log("debug", "entitlement_invoke_complete",
             controllerId=controller_id,
             eligible=result.get("eligible"), reason=result.get("reason"),
             elapsedMs=elapsed)
        return result
    except Exception as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("warning", "entitlement_invoke_failed",
             controllerId=controller_id, error=str(exc), elapsedMs=elapsed,
             detail="Failing open — never block legitimate OTA delivery")
        return {"eligible": True, "reason": "entitlement_invoke_failed"}


# ──────────────────────────────────────────────────────────────────────────────
# Active job check
# ──────────────────────────────────────────────────────────────────────────────

_BLOCKING_JOB_STATUSES  = {"QUEUED", "IN_PROGRESS"}
_RETRYABLE_JOB_STATUSES = {"FAILED", "TIMED_OUT"}
_COMPLETED_JOB_STATUSES = {"SUCCEEDED"}


def _get_job_live_status(job_id: str, thing_name: str | None) -> str | None:
    """
    Look up a job in OTA_JOBS_TABLE, optionally refresh via IoT live status.
    Returns the status string or None if job not found.
    """
    try:
        job = dynamo.Table(OTA_JOBS_TABLE).get_item(
            Key={"jobId": job_id}
        ).get("Item")
        if not job:
            _log("warning", "job_not_found_in_table",
                 jobId=job_id,
                 detail="pendingJobId set but no record in OTA_JOBS_TABLE")
            return None

        status = job.get("status", "")

        # Refresh live status from IoT Core for active jobs
        if thing_name and status in _BLOCKING_JOB_STATUSES:
            try:
                iot_resp   = iot.describe_job_execution(jobId=job_id, thingName=thing_name)
                iot_status = iot_resp.get("execution", {}).get("status")
                if iot_status:
                    _log("debug", "job_live_status_refreshed",
                         jobId=job_id, dbStatus=status, iotStatus=iot_status)
                    status = iot_status
            except Exception as exc:
                _log("warning", "job_live_status_refresh_failed",
                     jobId=job_id, error=str(exc),
                     detail="Using DynamoDB status as fallback")
        return status

    except Exception as exc:
        _log("warning", "job_status_lookup_failed",
             jobId=job_id, error=str(exc))
        return None


def _check_active_job(user_id: str, device_id: str, pkg_name: str,
                       thing_name: str | None, pending_job_id: str | None) -> tuple:
    """
    Check if there is an active (blocking), retryable, or completed job for this device+package.

    Uses device.pendingJobId as the quick path.
    Falls back to querying the consents table for ACCEPTED consents.

    Returns: (blocking, job_info, last_failed, last_completed)
      - blocking=True          → device has QUEUED or IN_PROGRESS job; do not offer update
      - blocking=False, last_failed    → previous job FAILED/TIMED_OUT; offer update + note
      - blocking=False, last_completed → previous job SUCCEEDED; surface completion to app
      - blocking=False, all None       → device is free, no recent job history
    """
    job_id_to_check = None
    job_version     = None

    # Quick path: pendingJobId on device record
    if pending_job_id and not pending_job_id.startswith("digilux-ota-dev-"):
        job_id_to_check = pending_job_id
        # Get version from job record
        try:
            jitem = dynamo.Table(OTA_JOBS_TABLE).get_item(
                Key={"jobId": pending_job_id}
            ).get("Item") or {}
            job_version = jitem.get("version", "")
        except Exception:
            pass

    # If no pendingJobId, check consents table for ACCEPTED + active job
    if not job_id_to_check:
        try:
            resp = dynamo.Table(CONSENTS_TABLE).query(
                IndexName=CONSENTS_USER_INDEX,
                KeyConditionExpression=(
                    Key("userId").eq(user_id) & Key("deviceId").eq(device_id)
                ),
                FilterExpression=(
                    Attr("status").eq("ACCEPTED") &
                    Attr("packageName").eq(pkg_name)
                ),
            )
            accepted = resp.get("Items", [])
            if accepted:
                # Sort by createdAt descending — use most recent
                accepted.sort(key=lambda c: int(c.get("createdAt", 0)), reverse=True)
                c = accepted[0]
                job_id_to_check = c.get("jobId")
                job_version     = c.get("version", "")
                _log("debug", "active_job_check_via_consent",
                     userId=user_id, deviceId=device_id,
                     consentId=c.get("consentId"), jobId=job_id_to_check)
        except Exception as e:
            _log("warning", "consent_query_for_active_job_failed",
                 userId=user_id, deviceId=device_id, error=str(e))

    if not job_id_to_check:
        return False, None, None, None

    status = _get_job_live_status(job_id_to_check, thing_name)
    if not status:
        return False, None, None, None

    _log("debug", "active_job_status_found",
         jobId=job_id_to_check, status=status,
         deviceId=device_id, packageName=pkg_name)

    if status in _BLOCKING_JOB_STATUSES:
        job_info = {
            "jobId":   job_id_to_check,
            "status":  status,
            "version": job_version or "",
            "message": OTA_IN_PROGRESS_MSG.format(version=job_version or ""),
        }
        _log("info", "device_blocked_by_active_job",
             deviceId=device_id, jobId=job_id_to_check, status=status,
             jobVersion=job_version)
        return True, job_info, None, None

    if status in _RETRYABLE_JOB_STATUSES:
        msg = (OTA_TIMED_OUT_MSG if status == "TIMED_OUT" else OTA_FAILED_MSG)
        last_failed = {
            "jobId":   job_id_to_check,
            "status":  status,
            "version": job_version or "",
            "message": msg.format(version=job_version or ""),
        }
        _log("info", "device_has_retryable_job",
             deviceId=device_id, jobId=job_id_to_check, status=status,
             detail="FAILED/TIMED_OUT job — will continue to offer update")
        return False, None, last_failed, None

    if status in _COMPLETED_JOB_STATUSES:
        last_completed = {
            "jobId":   job_id_to_check,
            "status":  status,
            "version": job_version or "",
            "message": OTA_COMPLETED_MSG.format(version=job_version or ""),
        }
        _log("info", "device_job_completed_found",
             deviceId=device_id, jobId=job_id_to_check, jobVersion=job_version,
             detail="SUCCEEDED job — will surface completion status to app")
        return False, None, None, last_completed

    # CANCELLED or unknown — device is free, no history to surface
    _log("debug", "job_terminal_device_free",
         jobId=job_id_to_check, status=status, deviceId=device_id)
    return False, None, None, None


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def _get_user_devices(user_id: str) -> list[dict]:
    t_start = time.monotonic()
    resp    = dynamo.Table(DEVICE_DATA_TABLE).query(
        IndexName=DEVICE_DATA_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id),
    )
    items   = resp.get("Items", [])
    elapsed = int((time.monotonic() - t_start) * 1000)
    _log("debug", "user_devices_fetched",
         userId=user_id, count=len(items), elapsedMs=elapsed)
    return items


def lambda_handler(event, context):
    handler_start = time.monotonic()
    request_id    = context.aws_request_id if context else None

    try:
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            _log("warning", "missing_sub_claim",
                 detail="JWT sub claim absent — rejecting with 401")
            return _failed(401, UNAUTHORIZED_MSG)

        email = claims.get("email", user_id)
        _log("info", "check_updates_request",
             userId=user_id, email=email, requestId=request_id)

        device_items = _get_user_devices(user_id)
        _log("info", "user_devices_fetched", userId=user_id, deviceCount=len(device_items))

        if not device_items:
            _log("info", "no_devices_for_user", userId=user_id)
            _audit("USER_CHECK_UPDATES", user_id, {"userId": user_id}, "SUCCESS",
                   devicesFound=0, updatesAvailable=0)
            return _success([])

        # ── Pre-loop I/O: beta check + package pointer caches ────────────────
        # All DB reads for package availability happen here, before the device
        # loop. Three caches are built:
        #   pkg_cache:    packageName → best of LATEST#BETA / LATEST#PROD pointer
        #   custom_cache: (deviceId, packageName) → LATEST#CUSTOM#{deviceId} pointer
        # Priority in device loop: CUSTOM > BETA/PROD.
        include_beta = _is_beta_user(user_id)

        unique_pkg_names = {
            (dev.get("package") or {}).get("name", "")
            for dev in device_items
            if (dev.get("package") or {}).get("name", "")
        }
        unique_device_ids = {
            dev.get("deviceId")
            for dev in device_items
            if dev.get("deviceId")
        }
        _log("debug", "pre_fetching_package_pointers",
             userId=user_id, packages=list(unique_pkg_names), includeBeta=include_beta)

        tbl = dynamo.Table(PACKAGES_TABLE)
        # pkg_cache: packageName → pointer item (or None if no active package)
        # Each pointer item has: targetVersion, releaseType, releaseNotes, fileName,
        #   deviceType, firmwareCategory (from full record), tierOverride (from full record)
        pkg_cache: dict = {}
        for pname in unique_pkg_names:
            beta_ptr = None
            prod_ptr = None

            if include_beta:
                try:
                    beta_ptr = tbl.get_item(
                        Key={"packageName": pname, "version": "LATEST#BETA"}
                    ).get("Item")
                except Exception as e:
                    _log("warning", "beta_pointer_fetch_failed",
                         packageName=pname, error=str(e))

            try:
                prod_ptr = tbl.get_item(
                    Key={"packageName": pname, "version": "LATEST#PROD"}
                ).get("Item")
            except Exception as e:
                _log("warning", "prod_pointer_fetch_failed",
                     packageName=pname, error=str(e))

            # For beta users: always pick the pointer with the newer targetVersion.
            # PROD deployments must be visible to beta users even when a BETA
            # deployment is also active (requirement #11). If BETA is ahead it
            # wins naturally; if PROD is ahead the beta user gets PROD.
            if include_beta and beta_ptr and prod_ptr:
                bv = beta_ptr.get("targetVersion", "")
                pv = prod_ptr.get("targetVersion", "")
                ptr = beta_ptr if _is_newer(bv, pv) else prod_ptr
                _log("debug", "beta_prod_pointer_selected",
                     packageName=pname, betaVersion=bv, prodVersion=pv,
                     selected=ptr.get("releaseType", ""))
            else:
                ptr = beta_ptr or prod_ptr
            if ptr:
                # Merge entitlement fields from the full package record
                target_ver = ptr.get("targetVersion", "")
                if target_ver:
                    try:
                        full = tbl.get_item(
                            Key={"packageName": pname, "version": target_ver}
                        ).get("Item") or {}
                        ptr["firmwareCategory"] = full.get("firmwareCategory")
                        ptr["tierOverride"]     = full.get("tierOverride")
                    except Exception as e:
                        _log("warning", "full_package_fetch_failed",
                             packageName=pname, version=target_ver, error=str(e))
            pkg_cache[pname] = ptr
            _log("debug", "pkg_cache_entry",
                 packageName=pname, found=bool(ptr),
                 targetVersion=ptr.get("targetVersion") if ptr else None,
                 releaseType=ptr.get("releaseType") if ptr else None)

        # ── Pre-fetch CUSTOM pointers (per deviceId × packageName) ────────────
        # CUSTOM deployments write LATEST#CUSTOM#{deviceId} pointer items into
        # the packages table. We look them up here so the device loop stays
        # in-memory. The entitlement fields are already embedded in the pointer
        # item at write time so no extra GetItem is needed.
        custom_cache: dict = {}  # key: (deviceId, packageName)
        for did in unique_device_ids:
            for pname in unique_pkg_names:
                try:
                    c_ptr = tbl.get_item(
                        Key={"packageName": pname, "version": f"LATEST#CUSTOM#{did}"}
                    ).get("Item")
                    if c_ptr:
                        custom_cache[(did, pname)] = c_ptr
                        _log("debug", "custom_pointer_found",
                             deviceId=did, packageName=pname,
                             targetVersion=c_ptr.get("targetVersion"))
                except Exception as e:
                    _log("warning", "custom_pointer_fetch_failed",
                         deviceId=did, packageName=pname, error=str(e))

        if custom_cache:
            _log("info", "custom_cache_loaded",
                 userId=user_id, entries=len(custom_cache))

        result_devices       = []
        not_registered_count = 0
        up_to_date_count     = 0
        blocked_count        = 0
        job_active_count     = 0

        # ── Group records by deviceId ─────────────────────────────────────────
        # A device can have multiple DB records (same deviceId, different
        # macAddress). We process each physical device as one unit so that a
        # blocking job on ANY record suppresses ALL updates for that device.
        device_groups: dict = defaultdict(list)
        for dev in device_items:
            did = dev.get("deviceId")
            if not did:
                _log("warning", "device_record_missing_deviceid",
                     userId=user_id, detail="Device record has no deviceId — skipping")
                continue
            device_groups[did].append(dev)

        for device_id, records in device_groups.items():

            _log("debug", "processing_device_group",
                 userId=user_id, deviceId=device_id, recordCount=len(records))

            # ── Pass 1: check ALL records for a blocking job ──────────────────
            # If any record has QUEUED/IN_PROGRESS we return JOB_ACTIVE for the
            # whole device and skip every other update — a device cannot receive
            # a new OTA while one is already in flight.
            blocking_job_info  = None
            last_failed_job    = None
            last_completed_job = None

            for rec in records:
                pkg_name       = (rec.get("package") or {}).get("name", "")
                thing_name     = rec.get("thingName")
                pending_job_id = rec.get("pendingJobId")
                blocking, job_info, lf, lc = _check_active_job(
                    user_id, device_id, pkg_name, thing_name, pending_job_id
                )
                if blocking:
                    blocking_job_info = job_info
                    # Attach the installed version + package from this record
                    blocking_job_info["_pkg_name"]   = pkg_name
                    blocking_job_info["_installed"]  = rec.get("globalInstalledVersion", "")
                    blocking_job_info["_thing_name"] = thing_name
                    break
                if lf and not last_failed_job:
                    last_failed_job = lf
                if lc and not last_completed_job:
                    last_completed_job = lc

            if blocking_job_info:
                job_active_count += 1
                pkg_name          = blocking_job_info.pop("_pkg_name", "")
                installed_version = blocking_job_info.pop("_installed", "")
                blocking_job_info.pop("_thing_name", None)
                _audit("ACTIVE_JOB_REPORTED", user_id,
                       {"deviceId": device_id, "jobId": blocking_job_info["jobId"],
                        "packageName": pkg_name, "version": blocking_job_info["version"]},
                       "SUCCESS", jobStatus=blocking_job_info["status"],
                       installedVersion=installed_version,
                       recordCount=len(records))
                result_devices.append({
                    "deviceId":         device_id,
                    "otaStatus":        "JOB_ACTIVE",
                    "package":          pkg_name,
                    "installedVersion": installed_version,
                    "activeJob":        blocking_job_info,
                })
                continue  # skip all further update checks for this device

            # ── Pass 2: update check — one entry per unique (deviceId, package) ─
            # Deduplicate by package name: if multiple records have the same
            # package, use the one with the highest installedVersion.
            best_by_pkg: dict = {}
            for rec in records:
                pkg_name          = (rec.get("package") or {}).get("name", "")
                installed_version = rec.get("globalInstalledVersion", "")
                if not pkg_name or not installed_version:
                    continue
                existing = best_by_pkg.get(pkg_name)
                if not existing or _is_newer(installed_version,
                                             existing.get("globalInstalledVersion", "")):
                    best_by_pkg[pkg_name] = rec

            if not best_by_pkg:
                _log("info", "device_not_registered_for_ota",
                     userId=user_id, deviceId=device_id,
                     detail="No record with both packageName and installedVersion")
                not_registered_count += 1
                result_devices.append({
                    "deviceId":  device_id,
                    "otaStatus": "NOT_REGISTERED",
                })
                continue

            for pkg_name, rec in best_by_pkg.items():
                installed_version = rec.get("globalInstalledVersion", "")
                thing_name        = rec.get("thingName")

                # ── Package pointer lookup: CUSTOM > BETA/PROD ────────────────
                # CUSTOM has highest priority — if admin created a targeted
                # deployment for this device, it overrides BETA and PROD.
                ptr = custom_cache.get((device_id, pkg_name)) or pkg_cache.get(pkg_name)
                if not ptr:
                    _log("info", "no_active_package_pointer",
                         userId=user_id, deviceId=device_id, packageName=pkg_name,
                         detail="No LATEST#CUSTOM, LATEST#PROD, or LATEST#BETA pointer")
                    up_to_date_count += 1
                    continue

                available_version = ptr.get("targetVersion", "")
                release_type      = ptr.get("releaseType", "")

                if not _is_newer(available_version, installed_version):
                    # Device is up to date. If the last job just succeeded for this
                    # exact version, surface a JOB_COMPLETED entry so the app can
                    # show the user a "firmware updated successfully" confirmation
                    # before declaring no new update available (requirement #22).
                    if (last_completed_job and
                            last_completed_job.get("version") == available_version):
                        result_devices.append({
                            "deviceId":         device_id,
                            "otaStatus":        "JOB_COMPLETED",
                            "package":          pkg_name,
                            "installedVersion": installed_version,
                            "lastCompletedJob": last_completed_job,
                        })
                        _log("info", "device_update_complete",
                             userId=user_id, deviceId=device_id,
                             packageName=pkg_name, version=available_version)
                    else:
                        _log("info", "device_up_to_date",
                             userId=user_id, deviceId=device_id,
                             packageName=pkg_name,
                             installedVersion=installed_version,
                             availableVersion=available_version)
                        up_to_date_count += 1
                    continue

                # ── Entitlement check ─────────────────────────────────────────
                firmware_category = ptr.get("firmwareCategory") or None
                tier_override     = ptr.get("tierOverride")     or None

                entitlement = _invoke_entitlement_check(
                    thing_name, firmware_category, tier_override
                )
                if not entitlement.get("eligible", True):
                    _log("info", "update_blocked_by_entitlement",
                         userId=user_id, deviceId=device_id,
                         packageName=pkg_name, availableVersion=available_version,
                         reason=entitlement.get("reason"),
                         subscriptionTier=entitlement.get("subscriptionTier"))
                    _audit("UPDATE_BLOCKED_ENTITLEMENT", user_id,
                           {"deviceId": device_id, "packageName": pkg_name,
                            "version": available_version},
                           "BLOCKED",
                           reason=entitlement.get("reason"),
                           subscriptionTier=entitlement.get("subscriptionTier"),
                           minimumTier=entitlement.get("minimumTier"),
                           firmwareCategory=firmware_category)
                    blocked_count += 1
                    continue

                # ── Build update-available entry ──────────────────────────────
                entry: dict = {
                    "deviceId":         device_id,
                    "otaStatus":        "REGISTERED",
                    "package":          pkg_name,
                    "installedVersion": installed_version,
                    "availableVersion": available_version,
                    "fileName":         ptr.get("fileName", ""),
                    "releaseNotes":     ptr.get("releaseNotes", ""),
                    "releaseType":      release_type,
                }
                if last_failed_job:
                    entry["lastFailedJob"] = last_failed_job
                if last_completed_job:
                    entry["lastCompletedJob"] = last_completed_job

                result_devices.append(entry)
                _log("info", "update_available",
                     userId=user_id, deviceId=device_id,
                     packageName=pkg_name,
                     installedVersion=installed_version,
                     availableVersion=available_version,
                     releaseType=release_type,
                     hasLastFailedJob=bool(last_failed_job))
                _audit("UPDATE_AVAILABLE", user_id,
                       {"deviceId": device_id, "packageName": pkg_name,
                        "version": available_version},
                       "SUCCESS",
                       installedVersion=installed_version,
                       availableVersion=available_version,
                       releaseType=release_type,
                       fileName=ptr.get("fileName", ""))

        handler_ms = int((time.monotonic() - handler_start) * 1000)

        _audit("USER_CHECK_UPDATES", user_id, {"userId": user_id}, "SUCCESS",
               totalDevices=len(device_items),
               updatesAvailable=len(result_devices),
               notRegistered=not_registered_count,
               upToDate=up_to_date_count,
               blocked=blocked_count,
               jobActive=job_active_count,
               handlerMs=handler_ms)

        _log("info", "check_updates_complete",
             userId=user_id,
             totalDevices=len(device_items),
             updatesAvailable=len(result_devices),
             notRegistered=not_registered_count,
             upToDate=up_to_date_count,
             blocked=blocked_count,
             jobActive=job_active_count,
             handlerMs=handler_ms)

        return _success(result_devices)

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "aws_client_error", awsError=code, awsMessage=msg)
        return _failed(500, INTERNAL_ERROR_MSG)
    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in user_check_updates: {e}")
        return _failed(500, INTERNAL_ERROR_MSG)
