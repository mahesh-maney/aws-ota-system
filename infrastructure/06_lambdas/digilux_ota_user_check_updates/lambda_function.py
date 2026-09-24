"""
digilux_ota_user_check_updates
Returns available OTA updates for all controller devices owned by the calling user.

GET /api/v1/ota/my/updates

Auth: Cognito ID token (any authenticated user — NOT admin only).
The userId is extracted from the JWT `sub` claim.
Only devices registered under that userId are returned.

OTA state (installedVersions, pendingJobId, thingName, model, hwRevision) is stored
directly on digilux_device_data items — no separate inventory table lookup needed.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTOR = "check_updates"

REGION                 = os.environ["REGION"]
DEVICE_DATA_TABLE      = os.environ.get("DEVICE_DATA_TABLE",      "digilux_device_data")
PACKAGES_TABLE         = os.environ.get("PACKAGES_TABLE",         "digilux_ota_packages")
DEVICE_DATA_USER_INDEX = os.environ.get("DEVICE_DATA_USER_INDEX", "userId-index")
CANARY_GROUP           = os.environ.get("CANARY_GROUP",           "DGX-Canary")
ENTITLEMENT_FUNCTION   = os.environ.get("ENTITLEMENT_FUNCTION",   "digilux_entitlement_check")
OTA_JOBS_TABLE         = os.environ.get("OTA_JOBS_TABLE",         "digilux_ota_jobs")
OTA_IN_PROGRESS_MSG    = os.environ.get(
    "OTA_IN_PROGRESS_MSG",
    "Your Firmware update ver {version} is in progress, please check after some time for status. "
    "Note: Please ensure the controller is Powered on.",
)
OTA_FAILED_MSG         = os.environ.get(
    "OTA_FAILED_MSG",
    "Your last firmware ver {version} update failed. Please contact support.",
)

dynamo         = boto3.resource("dynamodb", region_name=REGION)
iot            = boto3.client("iot",        region_name=REGION)
lambda_client  = boto3.client("lambda",     region_name=REGION)


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


def _log(level: str, msg: str, **fields) -> None:
    """Emit a structured JSON log line at the given level."""
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
    }))


def _version_tuple(v: str):
    """Parse '4.2.0' → (4, 2, 0). Non-numeric segments use their numeric prefix (e.g. '28188-rc1' → 28188)."""
    result = []
    for part in str(v).split("."):
        try:
            result.append(int(part.split("-")[0]))
        except (ValueError, AttributeError):
            result.append(0)
    return tuple(result) if result else (0,)


def _is_newer(candidate: str, installed: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(installed)


def _invoke_entitlement_check(controller_id: str, firmware_category: str,
                               tier_override: str | None) -> dict:
    """
    Invoke digilux_entitlement_check (internal Lambda-to-Lambda).
    Returns the entitlement response dict.
    Fails open on any error — existing OTA behaviour is never blocked by
    an entitlement service outage.
    """
    if not controller_id or not firmware_category:
        _log("debug", "entitlement_check_skipped",
             controllerId=controller_id, firmwareCategory=firmware_category,
             detail="Missing input — defaulting to eligible=True")
        return {"eligible": True, "reason": "missing_input"}

    payload = json.dumps({
        "controllerId":     controller_id,
        "firmwareCategory": firmware_category,
        "tierOverride":     tier_override,
    }).encode()

    _log("debug", "entitlement_invoke_start",
         controllerId=controller_id, firmwareCategory=firmware_category,
         function=ENTITLEMENT_FUNCTION)

    t_start = time.monotonic()
    try:
        resp = lambda_client.invoke(
            FunctionName   = ENTITLEMENT_FUNCTION,
            InvocationType = "RequestResponse",
            Payload        = payload,
        )
        result  = json.loads(resp["Payload"].read())
        elapsed = int((time.monotonic() - t_start) * 1000)

        # Lambda invocation errors surface as FunctionError key
        if resp.get("FunctionError"):
            _log("warning", "entitlement_function_error",
                 controllerId=controller_id, firmwareCategory=firmware_category,
                 functionError=resp["FunctionError"], elapsedMs=elapsed,
                 detail="Failing open — returning eligible=True")
            return {"eligible": True, "reason": "entitlement_function_error"}

        _log("debug", "entitlement_invoke_complete",
             controllerId=controller_id, firmwareCategory=firmware_category,
             eligible=result.get("eligible"), reason=result.get("reason"),
             subscriptionTier=result.get("subscriptionTier"),
             elapsedMs=elapsed)
        return result
    except Exception as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("warning", "entitlement_invoke_failed",
             controllerId=controller_id, firmwareCategory=firmware_category,
             error=str(exc), excType=type(exc).__name__, elapsedMs=elapsed,
             detail="Failing open — never block legitimate OTA delivery")
        return {"eligible": True, "reason": "entitlement_invoke_failed"}


# ──────────────────────────────────────────────────────────────────────────────
# Core logic
# ──────────────────────────────────────────────────────────────────────────────

def _get_user_devices(user_id: str) -> list[dict]:
    """Query digilux_device_data by userId-index GSI. Returns list of device items."""
    _log("debug", "user_device_query_start",
         userId=user_id, table=DEVICE_DATA_TABLE, index=DEVICE_DATA_USER_INDEX)
    t_start = time.monotonic()
    tbl  = dynamo.Table(DEVICE_DATA_TABLE)
    resp = tbl.query(
        IndexName=DEVICE_DATA_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id),
    )
    items = resp.get("Items", [])
    elapsed = int((time.monotonic() - t_start) * 1000)
    _log("debug", "user_device_query_complete",
         userId=user_id, deviceCount=len(items), elapsedMs=elapsed)
    return items


def _is_beta_device(thing_name: str) -> bool:
    """Return True if the device's IoT thing is in the canary group."""
    if not thing_name:
        _log("debug", "beta_check_skipped_no_thing_name")
        return False
    _log("debug", "beta_check_start",
         thingName=thing_name, canaryGroup=CANARY_GROUP)
    t_start = time.monotonic()
    try:
        resp   = iot.list_thing_groups_for_thing(thingName=thing_name)
        groups = [g.get("groupName", "") for g in resp.get("thingGroups", [])]
        result = CANARY_GROUP in groups
        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("debug", "beta_check_complete",
             thingName=thing_name, groups=groups,
             isBeta=result, elapsedMs=elapsed)
        return result
    except Exception as e:
        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("warning", "beta_check_failed",
             thingName=thing_name, error=str(e), elapsedMs=elapsed,
             detail="Could not check canary group — defaulting to non-beta")
        return False


def _get_latest_available_version(package_name: str, include_beta: bool) -> dict | None:
    """
    Query packages for packageName where:
      - status = ACTIVE  (binary has been processed by artifact_processor)
      - activated = True (admin has explicitly published this version)
      - releaseType = PROD always; also UAT if device is in the canary group
    Returns the highest semver version that matches, or None.
    """
    _log("debug", "package_lookup_start",
         packageName=package_name, includeBeta=include_beta)
    t_start = time.monotonic()

    tbl = dynamo.Table(PACKAGES_TABLE)

    filter_expr = (
        Attr("status").eq("ACTIVE") &
        Attr("activated").eq(True)
    )
    if not include_beta:
        filter_expr = filter_expr & Attr("releaseType").eq("PROD")

    resp  = tbl.query(
        KeyConditionExpression=Key("packageName").eq(package_name),
        FilterExpression=filter_expr,
    )
    items = resp.get("Items", [])
    elapsed = int((time.monotonic() - t_start) * 1000)

    _log("debug", "package_lookup_complete",
         packageName=package_name, includeBeta=include_beta,
         candidateCount=len(items), elapsedMs=elapsed,
         versions=[i.get("version") for i in items])

    if not items:
        return None
    best = max(items, key=lambda i: _version_tuple(i.get("version", "0.0.0")))
    _log("debug", "package_best_version_selected",
         packageName=package_name, selectedVersion=best.get("version"),
         releaseType=best.get("releaseType"))
    return best


def _get_active_job(job_id: str, thing_name: str | None) -> dict | None:
    """
    Look up a job in digilux_ota_jobs, then refresh its status via IoT if possible.
    Returns the job record dict, or None if not found / on error.
    """
    t_start = time.monotonic()
    _log("debug", "active_job_lookup_start", jobId=job_id, thingName=thing_name)
    try:
        resp = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id})
        job  = resp.get("Item")
        if not job:
            _log("warning", "active_job_not_found", jobId=job_id,
                 detail="pendingJobId set on device but no job record in OTA_JOBS_TABLE")
            return None

        # Refresh status from IoT for the most up-to-date execution state
        if thing_name:
            try:
                iot_resp   = iot.describe_job_execution(jobId=job_id, thingName=thing_name)
                iot_status = iot_resp.get("execution", {}).get("status")
                if iot_status:
                    _log("debug", "active_job_live_status_refreshed",
                         jobId=job_id, thingName=thing_name,
                         dbStatus=job.get("status"), iotStatus=iot_status)
                    job["status"] = iot_status
            except Exception as exc:
                _log("warning", "active_job_iot_refresh_failed",
                     jobId=job_id, thingName=thing_name, error=str(exc),
                     detail="Using DynamoDB status as fallback")

        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("debug", "active_job_lookup_complete",
             jobId=job_id, status=job.get("status"),
             packageName=job.get("packageName"), version=job.get("version"),
             elapsedMs=elapsed)
        return job

    except Exception as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        _log("warning", "active_job_lookup_failed",
             jobId=job_id, error=str(exc), excType=type(exc).__name__, elapsedMs=elapsed)
        return None


_IN_PROGRESS_JOB_STATUSES = {"AWAITING_CONSENT", "QUEUED", "IN_PROGRESS"}


def _job_user_message(status: str, version: str) -> str | None:
    """Return the user-facing message for a job status, or None if no message needed."""
    if status in _IN_PROGRESS_JOB_STATUSES:
        return OTA_IN_PROGRESS_MSG.format(version=version)
    if status == "FAILED":
        return OTA_FAILED_MSG.format(version=version)
    return None  # SUCCEEDED or unknown — let normal flow handle


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    handler_start = time.monotonic()
    request_id    = context.aws_request_id if context else None

    try:
        # ── Auth: extract userId from Cognito JWT claims ─────────────────────
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            _log("warning", "missing_sub_claim",
                 detail="JWT sub claim absent — rejecting with 401")
            return _resp(401, {"error": "Unauthorized — invalid token"})

        email = claims.get("email", user_id)
        _log("info", "check_updates_request",
             userId=user_id, email=email, requestId=request_id)

        # ── Fetch devices owned by this user ─────────────────────────────────
        device_items = _get_user_devices(user_id)
        _log("info", "user_devices_fetched",
             userId=user_id, deviceCount=len(device_items))

        if not device_items:
            _log("info", "no_devices_for_user",
                 userId=user_id, detail="User has no registered devices")
            _audit("USER_CHECK_UPDATES", user_id, {"userId": user_id}, "SUCCESS",
                   devicesFound=0, updatesAvailable=0)
            return _resp(200, {"devices": []})

        result_devices      = []
        not_registered_count = 0
        up_to_date_count    = 0
        blocked_count       = 0
        job_active_count    = 0

        for dev in device_items:
            device_id = dev.get("deviceId")
            if not device_id:
                _log("warning", "device_record_missing_deviceid",
                     userId=user_id, record=str(dev)[:200],
                     detail="Device record has no deviceId — skipping")
                continue

            # ── OTA fields come directly from device_data item ────────────────
            installed_version = dev.get("globalInstalledVersion", "")
            pkg_info          = dev.get("package") or {}
            pkg_name          = pkg_info.get("name", "")
            thing_name        = dev.get("thingName")

            _log("debug", "processing_device",
                 userId=user_id, deviceId=device_id, thingName=thing_name,
                 packageName=pkg_name, installedVersion=installed_version)

            if not installed_version or not pkg_name:
                _log("info", "device_not_registered_for_ota",
                     userId=user_id, deviceId=device_id, thingName=thing_name,
                     hasInstalledVersion=bool(installed_version),
                     hasPackageName=bool(pkg_name),
                     detail="OTA agent not yet started or registration incomplete")
                not_registered_count += 1
                result_devices.append({
                    "deviceId":  device_id,
                    "otaStatus": "NOT_REGISTERED",
                })
                continue

            # ── Active job check — if a job exists, report its status ─────────
            # Only surface jobs that follow the OTA production naming convention
            # ("digilux-ota-<pkg>-<ver>-<ts>").  Dev/simulate jobs use the prefix
            # "digilux-ota-dev-" and must never affect this status response.
            # Any future non-OTA IoT jobs should also use a different prefix so
            # they are automatically ignored here.
            pending_job_id = dev.get("pendingJobId")
            if pending_job_id and pending_job_id.startswith("digilux-ota-dev-"):
                _log("debug", "pending_job_id_skipped_non_ota",
                     userId=user_id, deviceId=device_id, jobId=pending_job_id,
                     detail="Dev/simulate job — not surfaced in available-updates")
                pending_job_id = None
            last_failed_job: dict | None = None
            if pending_job_id:
                _log("info", "device_has_pending_job",
                     userId=user_id, deviceId=device_id,
                     jobId=pending_job_id, thingName=thing_name)
                job = _get_active_job(pending_job_id, thing_name)
                if job:
                    job_status  = job.get("status", "")
                    job_version = job.get("version", "")

                    if job_status in _IN_PROGRESS_JOB_STATUSES:
                        # Job is actively in progress — block version comparison
                        message = OTA_IN_PROGRESS_MSG.format(version=job_version)
                        _log("info", "active_job_reported",
                             userId=user_id, deviceId=device_id,
                             jobId=pending_job_id, jobStatus=job_status,
                             jobVersion=job_version, packageName=job.get("packageName"),
                             messageTemplate="in_progress")
                        _audit("ACTIVE_JOB_REPORTED", user_id,
                               {"deviceId": device_id, "jobId": pending_job_id,
                                "packageName": job.get("packageName"), "version": job_version},
                               "SUCCESS", jobStatus=job_status,
                               installedVersion=installed_version)
                        job_active_count += 1
                        result_devices.append({
                            "deviceId":         device_id,
                            "otaStatus":        "JOB_ACTIVE",
                            "package":          job.get("packageName", pkg_name),
                            "installedVersion": installed_version,
                            "activeJob": {
                                "jobId":   pending_job_id,
                                "status":  job_status,
                                "version": job_version,
                                "message": message,
                            },
                        })
                        continue

                    if job_status == "FAILED":
                        # Failed job — do NOT block version comparison.
                        # Carry the failure info so it can be attached to the
                        # UPDATE_AVAILABLE response, letting the user see the
                        # new version while also showing what failed last time.
                        last_failed_job = {
                            "jobId":   pending_job_id,
                            "status":  "FAILED",
                            "version": job_version,
                            "message": OTA_FAILED_MSG.format(version=job_version),
                        }
                        _log("info", "failed_job_fall_through",
                             userId=user_id, deviceId=device_id,
                             jobId=pending_job_id, jobVersion=job_version,
                             detail="FAILED job — continuing version check so user can see new available update")
                    else:
                        # SUCCEEDED (or unknown) — fall through to normal version check
                        _log("debug", "active_job_succeeded_fall_through",
                             userId=user_id, deviceId=device_id,
                             jobId=pending_job_id, jobStatus=job_status,
                             detail="Job succeeded — continuing with normal update check")
                else:
                    # Job record missing in OTA_JOBS_TABLE despite pendingJobId being set
                    _log("warning", "pending_job_record_missing_fall_through",
                         userId=user_id, deviceId=device_id,
                         jobId=pending_job_id,
                         detail="No job record found — treating device as normal update candidate")

            # ── Determine if this device sees UAT packages ───────────────────
            include_beta = _is_beta_device(thing_name)
            _log("info", "device_beta_status",
                 userId=user_id, deviceId=device_id,
                 thingName=thing_name, includeBeta=include_beta)

            # ── Compare installed package with latest available version ───────
            latest_pkg = _get_latest_available_version(pkg_name, include_beta)
            if not latest_pkg:
                _log("info", "no_available_package",
                     userId=user_id, deviceId=device_id, packageName=pkg_name,
                     includeBeta=include_beta,
                     detail="No ACTIVE+activated package found — device is up to date or package not released")
                up_to_date_count += 1
                continue

            latest_ver = latest_pkg.get("version", "")
            _log("debug", "version_comparison",
                 userId=user_id, deviceId=device_id, packageName=pkg_name,
                 installedVersion=installed_version, latestVersion=latest_ver,
                 installedTuple=list(_version_tuple(installed_version)),
                 latestTuple=list(_version_tuple(latest_ver)))

            if not _is_newer(latest_ver, installed_version):
                _log("info", "device_up_to_date",
                     userId=user_id, deviceId=device_id, packageName=pkg_name,
                     installedVersion=installed_version, latestVersion=latest_ver)
                up_to_date_count += 1
                continue

            # ── Entitlement check — gate on subscription tier ─────────────────
            firmware_category = latest_pkg.get("firmwareCategory") or None
            tier_override     = latest_pkg.get("tierOverride")     or None
            _log("debug", "entitlement_check_required",
                 userId=user_id, deviceId=device_id, packageName=pkg_name,
                 availableVersion=latest_ver, firmwareCategory=firmware_category,
                 tierOverride=tier_override)

            entitlement = _invoke_entitlement_check(thing_name, firmware_category, tier_override)

            if not entitlement.get("eligible", True):
                _log("info", "update_blocked_by_entitlement",
                     userId=user_id, deviceId=device_id,
                     packageName=pkg_name,
                     installedVersion=installed_version,
                     availableVersion=latest_ver,
                     firmwareCategory=firmware_category,
                     reason=entitlement.get("reason"),
                     subscriptionTier=entitlement.get("subscriptionTier"),
                     minimumTier=entitlement.get("minimumTier"))
                _audit("UPDATE_BLOCKED_ENTITLEMENT", user_id,
                       {"deviceId": device_id, "packageName": pkg_name, "version": latest_ver},
                       "BLOCKED",
                       reason=entitlement.get("reason"),
                       subscriptionTier=entitlement.get("subscriptionTier"),
                       minimumTier=entitlement.get("minimumTier"),
                       firmwareCategory=firmware_category)
                blocked_count += 1
            else:
                entry: dict = {
                    "deviceId":         device_id,
                    "otaStatus":        "REGISTERED",
                    "package":          pkg_name,
                    "installedVersion": installed_version,
                    "availableVersion": latest_ver,
                    "fileName":         latest_pkg.get("fileName", ""),
                    "releaseNotes":     latest_pkg.get("releaseNotes", ""),
                }
                if last_failed_job:
                    entry["lastFailedJob"] = last_failed_job
                result_devices.append(entry)
                _log("info", "update_available",
                     userId=user_id, deviceId=device_id,
                     packageName=pkg_name,
                     installedVersion=installed_version,
                     availableVersion=latest_ver,
                     fileName=latest_pkg.get("fileName", ""),
                     releaseNotesLength=len(latest_pkg.get("releaseNotes", "")))
                _audit("UPDATE_AVAILABLE", user_id,
                       {"deviceId": device_id, "packageName": pkg_name, "version": latest_ver},
                       "SUCCESS",
                       installedVersion=installed_version,
                       availableVersion=latest_ver,
                       fileName=latest_pkg.get("fileName", ""),
                       isBeta=include_beta)

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

        return _resp(200, {"devices": result_devices})

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "aws_client_error",
             awsError=code, awsMessage=msg)
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in user_check_updates: {e}")
        return _resp(500, {"error": "Internal server error"})
