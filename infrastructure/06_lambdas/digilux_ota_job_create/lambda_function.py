"""
digilux_ota_job_create
Creates a consent-gated OTA deployment.
Admin triggers, devices cannot be updated without explicit user consent.

POST /api/v1/ota/deployments
Admin-only.

Body:
{
  "packageName":  "HomeAssistantUtility",
  "version":      "4.5.0",
  "targetType":   "THING" | "THING_GROUP",
  "targetId":     "deviceId-uuid"  |  "DGX-Canary",
  "rolloutStage": "BETA" | "UAT" | "PRODUCTION",
  "rolloutConfig": {}   # optional overrides
}

Flow:
  1. Validate package is ACTIVE.
  2. Resolve target devices.
  3. Write deployment record (status=AWAITING_CONSENT) to digilux_ota_jobs.
  4. Write one PENDING consent record per device to digilux_ota_user_consents.
  5. Return deployment info — no IoT Job is created here.
  IoT Jobs are created by digilux_ota_user_consent when the user taps YES.
"""
import datetime
import json
import logging
import os
import time
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTOR = "job_create"


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


def _log(level: str, msg: str, **fields) -> None:
    """Emit a structured JSON log line at the given level."""
    record = {"msg": msg, **fields}
    getattr(log, level)(json.dumps(record, default=str))


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


REGION           = os.environ["REGION"]
ACCOUNT_ID       = os.environ["ACCOUNT_ID"]
PACKAGES_TABLE   = os.environ.get("PACKAGES_TABLE",   "digilux_ota_packages")
DEVICE_DATA_TABLE = os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data")
OTA_JOBS_TABLE   = os.environ.get("OTA_JOBS_TABLE",   "digilux_ota_jobs")
CONSENTS_TABLE   = os.environ.get("CONSENTS_TABLE",   "digilux_ota_user_consents")
BETA_USERS_TABLE = os.environ.get("BETA_USERS_TABLE", "digilux_ota_beta_users")

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot",       region_name=REGION)

# Staged rollout defaults (startup scale)
ROLLOUT_CONFIGS = {
    "BETA":       {"maximumPerMinute": 2},
    "CUSTOM":     {"maximumPerMinute": 2},
    "CANARY":     {"maximumPerMinute": 2},
    "UAT": {
        "maximumPerMinute": 5,
        "exponentialRate": {
            "baseRatePerMinute": 2,
            "incrementFactor": 2,
            "rateIncreaseCriteria": {"numberOfSucceededThings": 5},
        },
    },
    "PRODUCTION": {
        "maximumPerMinute": 20,
        "exponentialRate": {
            "baseRatePerMinute": 5,
            "incrementFactor": 2,
            "rateIncreaseCriteria": {"numberOfSucceededThings": 20},
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Device resolution helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_device_by_id(device_id: str) -> dict | None:
    """Query device_data by deviceId (hash key). Returns first item or None."""
    _log("debug", "device_lookup_start", deviceId=device_id)
    items = dynamo.Table(DEVICE_DATA_TABLE).query(
        KeyConditionExpression=Key("deviceId").eq(device_id)
    ).get("Items", [])
    if not items:
        _log("debug", "device_lookup_not_found", deviceId=device_id)
        return None
    _log("debug", "device_lookup_found",
         deviceId=device_id,
         userId=items[0].get("userId"),
         thingName=items[0].get("thingName"))
    return items[0]


def _resolve_thing_group_devices(group_name: str) -> list[dict]:
    """
    Enumerate all thingNames in an IoT thing group, then look up each in
    device_data to get deviceId + userId.  Scan once, build lookup map.
    Returns list of {deviceId, userId, thingName, macAddress}.
    """
    _log("info", "thing_group_resolution_start", groupName=group_name)
    t_start = time.monotonic()

    # 1. Get all thing names in the group (paginated)
    thing_names = []
    paginator = iot.get_paginator("list_things_in_thing_group")
    for page in paginator.paginate(thingGroupName=group_name):
        thing_names.extend(page.get("things", []))

    if not thing_names:
        _log("warning", "thing_group_empty_or_missing",
             groupName=group_name,
             detail="IoT thing group is empty or does not exist")
        return []

    _log("info", "thing_group_iot_listed",
         groupName=group_name, thingCount=len(thing_names))

    # 2. Scan device_data once; build thingName → device_record map
    tbl = dynamo.Table(DEVICE_DATA_TABLE)
    name_set = set(thing_names)
    device_map = {}
    scan_pages = 0
    scan_kwargs = {}
    while True:
        resp = tbl.scan(**scan_kwargs)
        scan_pages += 1
        for item in resp.get("Items", []):
            tn = item.get("thingName")
            if tn in name_set:
                device_map[tn] = item
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    _log("debug", "thing_group_dynamo_scan_complete",
         groupName=group_name, scanPages=scan_pages,
         matchedInDb=len(device_map), totalThings=len(thing_names))

    # 3. Build result list — skip things with no device_data or no userId
    resolved = []
    skipped_no_record = []
    skipped_no_userid = []

    for tn in thing_names:
        item = device_map.get(tn)
        if not item:
            skipped_no_record.append(tn)
            _log("warning", "thing_not_in_device_data",
                 thingName=tn, groupName=group_name,
                 detail="thingName not found in device_data — skipping from deployment")
            _audit("DEVICE_SKIPPED_NO_RECORD", ACTOR,
                   {"thingName": tn, "group": group_name}, "WARN",
                   detail="Device has no device_data record — excluded from deployment")
            continue
        if not item.get("userId"):
            skipped_no_userid.append(tn)
            _log("warning", "thing_has_no_userid",
                 thingName=tn, deviceId=item.get("deviceId"), groupName=group_name,
                 detail="Device has no userId — cannot send consent notification")
            _audit("DEVICE_SKIPPED_NO_USERID", ACTOR,
                   {"thingName": tn, "deviceId": item.get("deviceId"), "group": group_name},
                   "WARN",
                   detail="Device excluded from deployment — no userId registered")
            continue
        resolved.append({
            "deviceId":   item["deviceId"],
            "userId":     item["userId"],
            "thingName":  tn,
            "macAddress": item.get("macAddress", ""),
        })

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    _log("info", "thing_group_resolution_complete",
         groupName=group_name,
         totalThings=len(thing_names), resolvedCount=len(resolved),
         skippedNoRecord=len(skipped_no_record), skippedNoUserId=len(skipped_no_userid),
         elapsedMs=elapsed_ms)

    _audit("DEPLOYMENT_TARGET_RESOLVED", ACTOR,
           {"group": group_name}, "SUCCESS",
           totalThings=len(thing_names), resolvedCount=len(resolved),
           skippedNoRecord=len(skipped_no_record), skippedNoUserId=len(skipped_no_userid),
           elapsedMs=elapsed_ms)

    return resolved


def _create_consent_records(deployment_id: str, devices: list[dict],
                             pkg_name: str, version: str) -> int:
    """
    Write one PENDING consent record per device.
    Returns the number of records created.
    """
    _log("info", "consent_records_write_start",
         deploymentId=deployment_id, deviceCount=len(devices),
         packageName=pkg_name, version=version)

    now_ms = int(time.time() * 1000)
    tbl    = dynamo.Table(CONSENTS_TABLE)
    count  = 0
    errors = 0

    for dev in devices:
        consent_id = str(uuid.uuid4())
        _log("debug", "consent_record_write",
             deploymentId=deployment_id, consentId=consent_id,
             deviceId=dev["deviceId"], userId=dev["userId"],
             packageName=pkg_name, version=version)
        try:
            tbl.put_item(Item={
                "consentId":    consent_id,
                "deploymentId": deployment_id,
                "userId":       dev["userId"],
                "deviceId":     dev["deviceId"],
                "packageName":  pkg_name,
                "version":      version,
                "status":       "PENDING",
                "createdAt":    now_ms,
            })
            count += 1
        except ClientError as e:
            errors += 1
            _log("error", "consent_record_write_failed",
                 deploymentId=deployment_id, consentId=consent_id,
                 deviceId=dev["deviceId"],
                 awsError=e.response["Error"]["Code"],
                 awsMessage=e.response["Error"]["Message"])
            _audit("CONSENT_RECORD_WRITE_FAILED", ACTOR,
                   {"deploymentId": deployment_id, "deviceId": dev["deviceId"]},
                   "FAILURE",
                   awsError=e.response["Error"]["Code"])

    _log("info", "consent_records_write_complete",
         deploymentId=deployment_id, created=count, errors=errors,
         packageName=pkg_name, version=version)

    if count > 0:
        _audit("CONSENT_RECORDS_CREATED", ACTOR,
               {"deploymentId": deployment_id, "packageName": pkg_name, "version": version},
               "SUCCESS",
               created=count, errors=errors)

    return count


# ──────────────────────────────────────────────────────────────────────────────
# Handler
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
                 groups=claims.get("cognito:groups", ""),
                 detail="Caller is not in ota-admin group")
            _audit("UNAUTHORIZED_ACCESS", ACTOR,
                   {"path": event.get("path", "")}, "FAILURE",
                   reason="not_in_ota_admin_group",
                   groups=claims.get("cognito:groups", ""))
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))
        _log("debug", "admin_caller_identified",
             caller=caller, groups=claims.get("cognito:groups", ""))

        if method == "GET" and job_id_param:
            _log("debug", "routing_to_get_job", jobId=job_id_param)
            return _get_job(job_id_param)
        if method == "GET":
            _log("debug", "routing_to_list_jobs")
            return _list_jobs(event)
        if method == "POST" and job_id_param:
            _log("info", "routing_to_abort_job",
                 jobId=job_id_param, caller=caller)
            return _abort_job(job_id_param, claims)

        # ── POST /ota/deployments — create deployment ─────────────────────────
        body = json.loads(event.get("body") or "{}")
        for field in ["packageName", "version"]:
            if not body.get(field):
                _log("warning", "missing_required_field",
                     field=field, caller=caller)
                return _response(400, {"error": f"Missing required field: {field}"})

        pkg_name      = body["packageName"]
        version       = body["version"]
        rollout_stage = body.get("rolloutStage", "PRODUCTION").upper()

        _log("info", "create_deployment_start",
             packageName=pkg_name, version=version,
             rolloutStage=rollout_stage, caller=caller)

        if rollout_stage not in ROLLOUT_CONFIGS:
            _log("warning", "invalid_rollout_stage",
                 rolloutStage=rollout_stage, caller=caller,
                 validStages=list(ROLLOUT_CONFIGS.keys()))
            return _response(400, {"error": f"rolloutStage must be one of: {', '.join(ROLLOUT_CONFIGS)}"})

        t_resolve = time.monotonic()

        # Resolve target devices
        if rollout_stage == "BETA":
            device_table = dynamo.Table(DEVICE_DATA_TABLE)
            target_ids   = body.get("targetIds", [])
            if not target_ids:
                _log("warning", "beta_no_target_ids",
                     packageName=pkg_name, version=version, caller=caller)
                return _response(400, {"error": "No beta users selected. Select at least one beta user."})
            _log("info", "resolving_beta_targets",
                 targetIds=target_ids, count=len(target_ids))
            devices = []
            for device_id in target_ids:
                items = device_table.query(
                    KeyConditionExpression=Key("deviceId").eq(device_id)
                ).get("Items", [])
                if not items:
                    _log("warning", "beta_device_not_found",
                         deviceId=device_id,
                         detail="Beta target deviceId not found in device_data — skipping")
                    _audit("DEVICE_SKIPPED_NOT_FOUND", caller,
                           {"deviceId": device_id}, "WARN",
                           rolloutStage="BETA")
                    continue
                item = items[0]
                if not item.get("userId"):
                    _log("warning", "beta_device_no_userid",
                         deviceId=device_id, thingName=item.get("thingName"),
                         detail="Beta target device has no userId — skipping")
                    _audit("DEVICE_SKIPPED_NO_USERID", caller,
                           {"deviceId": device_id}, "WARN",
                           rolloutStage="BETA")
                    continue
                devices.append({
                    "deviceId":  device_id,
                    "userId":    item["userId"],
                    "thingName": item.get("thingName", ""),
                    "macAddress": item.get("macAddress", ""),
                })
                _log("debug", "beta_device_resolved",
                     deviceId=device_id, userId=item["userId"],
                     thingName=item.get("thingName"))
            if not devices:
                _log("warning", "beta_no_valid_devices",
                     targetIds=target_ids, caller=caller)
                return _response(400, {"error": "None of the selected beta users have a registered device."})
            target_type = "THING_LIST"
            target_id   = ",".join(d["deviceId"] for d in devices)

        elif rollout_stage == "CUSTOM":
            target_ids = body.get("targetIds", [])
            if not target_ids:
                _log("warning", "custom_no_target_ids",
                     packageName=pkg_name, version=version, caller=caller)
                return _response(400, {"error": "No device IDs provided for CUSTOM deployment."})
            _log("info", "resolving_custom_targets",
                 targetIds=target_ids, count=len(target_ids))
            device_table = dynamo.Table(DEVICE_DATA_TABLE)
            devices = []
            for device_id in target_ids:
                items = device_table.query(
                    KeyConditionExpression=Key("deviceId").eq(device_id)
                ).get("Items", [])
                if not items:
                    _log("warning", "custom_device_not_found",
                         deviceId=device_id,
                         detail="Custom target deviceId not found in device_data — skipping")
                    _audit("DEVICE_SKIPPED_NOT_FOUND", caller,
                           {"deviceId": device_id}, "WARN",
                           rolloutStage="CUSTOM")
                    continue
                item = items[0]
                devices.append({
                    "deviceId":  device_id,
                    "userId":    item.get("userId", ""),
                    "thingName": item.get("thingName", ""),
                    "macAddress": item.get("macAddress", ""),
                })
                _log("debug", "custom_device_resolved",
                     deviceId=device_id, userId=item.get("userId"))
            if not devices:
                _log("warning", "custom_no_valid_devices",
                     targetIds=target_ids, caller=caller)
                return _response(400, {"error": "None of the provided device IDs were found."})
            target_type = "THING_LIST"
            target_id   = ",".join(d["deviceId"] for d in devices)

        else:
            target_type = (body.get("targetType") or "").upper()
            target_id   = body.get("targetId", "")
            if not target_type or not target_id:
                _log("warning", "missing_target_fields",
                     targetType=target_type, targetId=target_id, caller=caller)
                return _response(400, {"error": "Missing required fields: targetType, targetId"})
            if target_type not in ("THING", "THING_GROUP"):
                _log("warning", "invalid_target_type",
                     targetType=target_type, caller=caller)
                return _response(400, {"error": "targetType must be THING or THING_GROUP"})

            if target_type == "THING":
                dev = _get_device_by_id(target_id)
                if not dev:
                    _log("warning", "thing_device_not_found",
                         deviceId=target_id, caller=caller)
                    return _response(404, {"error": f"Device {target_id} not found in OTA inventory."})
                if not dev.get("userId"):
                    _log("warning", "thing_device_no_userid",
                         deviceId=target_id, thingName=dev.get("thingName"), caller=caller)
                    return _response(400, {"error": f"Device {target_id} has no registered user."})
                devices = [{
                    "deviceId":   target_id,
                    "userId":     dev["userId"],
                    "thingName":  dev.get("thingName", ""),
                    "macAddress": dev.get("macAddress", ""),
                }]
                _log("info", "single_thing_resolved",
                     deviceId=target_id, userId=dev["userId"],
                     thingName=dev.get("thingName"))
            else:  # THING_GROUP
                devices = _resolve_thing_group_devices(target_id)
                if not devices:
                    _log("warning", "thing_group_no_devices",
                         groupName=target_id, caller=caller)
                    return _response(400, {
                        "error": f"Thing group '{target_id}' has no registered devices."
                    })

        resolve_ms = int((time.monotonic() - t_resolve) * 1000)
        _log("info", "target_resolution_complete",
             rolloutStage=rollout_stage, targetType=target_type, targetId=target_id,
             deviceCount=len(devices), resolveMs=resolve_ms)

        # Validate package is ACTIVE
        _log("debug", "package_validation_start",
             packageName=pkg_name, version=version)
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
            return _response(400, {"error": f"Package {pkg_name}@{version} is not ACTIVE"})

        _log("info", "package_validated",
             packageName=pkg_name, version=version,
             releaseType=pkg.get("releaseType"), deviceType=pkg.get("deviceType"),
             artifactSize=pkg.get("artifactSize"))

        # Create deployment record
        deployment_id = f"digilux-ota-{pkg_name}-{version}-{int(time.time())}".replace(".", "-")
        now_ms = int(time.time() * 1000)

        rollout_cfg = ROLLOUT_CONFIGS[rollout_stage].copy()
        if body.get("rolloutConfig"):
            rollout_cfg.update(body["rolloutConfig"])
            _log("debug", "custom_rollout_config_applied",
                 deploymentId=deployment_id, rolloutConfig=rollout_cfg)

        _log("info", "writing_deployment_record",
             deploymentId=deployment_id, packageName=pkg_name, version=version,
             rolloutStage=rollout_stage, deviceCount=len(devices), caller=caller)

        dynamo.Table(OTA_JOBS_TABLE).put_item(Item={
            "jobId":          deployment_id,
            "packageName":    pkg_name,
            "version":        version,
            "deviceType":     pkg.get("deviceType", ""),
            "targetType":     target_type,
            "targetId":       target_id,
            "rolloutStage":   rollout_stage,
            "status":         "AWAITING_CONSENT",
            "createdAt":      now_ms,
            "createdBy":      caller,
            "consentCount":   len(devices),
            "deviceStatuses": {},
        })
        _log("info", "deployment_record_created",
             deploymentId=deployment_id, status="AWAITING_CONSENT",
             consentCount=len(devices))

        # Create one PENDING consent record per device
        t_consent = time.monotonic()
        consent_count = _create_consent_records(deployment_id, devices, pkg_name, version)
        consent_ms = int((time.monotonic() - t_consent) * 1000)

        _log("info", "deployment_created_awaiting_consent",
             deploymentId=deployment_id, packageName=pkg_name, version=version,
             consentCount=consent_count, createdBy=caller,
             rolloutStage=rollout_stage, targetType=target_type,
             consentWriteMs=consent_ms, resolveMs=resolve_ms)

        _audit("DEPLOYMENT_CREATED", caller,
               {"packageName": pkg_name, "version": version},
               "SUCCESS",
               deploymentId=deployment_id,
               targetType=target_type, targetId=target_id,
               rolloutStage=rollout_stage, consentCount=consent_count,
               deviceType=pkg.get("deviceType", ""),
               releaseType=pkg.get("releaseType", ""),
               awaitingConsent=True,
               resolveMs=resolve_ms, consentWriteMs=consent_ms)

        return _response(201, {
            "jobId":          deployment_id,
            "packageName":    pkg_name,
            "version":        version,
            "targetType":     target_type,
            "targetId":       target_id,
            "rolloutStage":   rollout_stage,
            "status":         "AWAITING_CONSENT",
            "consentCount":   consent_count,
            "message": f"Deployment created. Consent notifications sent to {consent_count} device(s).",
        })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "aws_client_error",
             awsError=code, awsMessage=msg)
        _audit("DEPLOYMENT_AWS_ERROR", ACTOR, {}, "FAILURE",
               awsError=code, awsMessage=msg)
        return _response(500, {"error": f"AWS error: {msg}"})
    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in job_create handler: {e}")
        _audit("DEPLOYMENT_UNHANDLED_ERROR", ACTOR, {}, "FAILURE",
               error=str(e), excType=type(e).__name__)
        return _response(500, {"error": "Internal server error"})


def _get_job(job_id: str) -> dict:
    """GET /ota/deployments/{jobId} — deployment detail with consent stats."""
    _log("debug", "get_job_start", jobId=job_id)
    item = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        _log("warning", "get_job_not_found", jobId=job_id)
        return _response(404, {"error": f"Job {job_id} not found"})

    _log("debug", "get_job_found",
         jobId=job_id, status=item.get("status"),
         packageName=item.get("packageName"), version=item.get("version"))

    # Enrich live IoT status if an actual IoT Job exists
    iot_job_id = item.get("iotJobId")
    if iot_job_id:
        try:
            iot_job = iot.describe_job(jobId=iot_job_id)["job"]
            item["iotStatus"]    = iot_job.get("jobProcessDetails", {})
            item["iotJobStatus"] = iot_job.get("status")
            _log("debug", "iot_job_status_fetched",
                 iotJobId=iot_job_id, iotStatus=iot_job.get("status"))
        except Exception as e:
            _log("warning", "iot_job_status_fetch_failed",
                 iotJobId=iot_job_id, error=str(e))
    elif item.get("status") not in ("AWAITING_CONSENT",):
        # Legacy: jobId IS the iotJobId
        try:
            iot_job = iot.describe_job(jobId=job_id)["job"]
            item["iotStatus"]    = iot_job.get("jobProcessDetails", {})
            item["iotJobStatus"] = iot_job.get("status")
            _log("debug", "iot_job_status_fetched_legacy",
                 jobId=job_id, iotStatus=iot_job.get("status"))
        except Exception as e:
            _log("warning", "iot_job_status_fetch_failed_legacy",
                 jobId=job_id, error=str(e))

    # Consent stats from deploymentId-index GSI
    try:
        resp = dynamo.Table(CONSENTS_TABLE).query(
            IndexName="deploymentId-index",
            KeyConditionExpression=Key("deploymentId").eq(job_id),
        )
        consents = resp.get("Items", [])
        stats = {"PENDING": 0, "ACCEPTED": 0, "DECLINED": 0}
        for c in consents:
            s = c.get("status", "PENDING")
            if s in stats:
                stats[s] += 1
        item["consentStats"] = stats
        _log("debug", "consent_stats_computed",
             jobId=job_id, totalConsents=len(consents), stats=stats)
    except Exception as e:
        _log("warning", "consent_stats_fetch_failed",
             jobId=job_id, error=str(e))

    return _response(200, json.loads(json.dumps(item, cls=_DecimalEncoder)))


def _list_jobs(event: dict) -> dict:
    """GET /ota/deployments — list OTA jobs, newest first."""
    params = event.get("queryStringParameters") or {}
    limit  = min(int(params.get("limit", 20)), 100)
    _log("debug", "list_jobs_start", limit=limit)

    result = dynamo.Table(OTA_JOBS_TABLE).scan()
    items  = result.get("Items", [])
    items.sort(key=lambda x: int(x.get("createdAt", 0)), reverse=True)
    items  = items[:limit]

    _log("info", "list_jobs_complete", returnedCount=len(items), limit=limit)

    jobs = [
        {
            "jobId":        i.get("jobId"),
            "packageName":  i.get("packageName"),
            "version":      i.get("version"),
            "deviceType":   i.get("deviceType"),
            "targetType":   i.get("targetType"),
            "targetId":     i.get("targetId"),
            "rolloutStage": i.get("rolloutStage"),
            "status":       i.get("status"),
            "createdBy":    i.get("createdBy"),
            "createdAt":    int(i["createdAt"]) if "createdAt" in i else None,
            "completedAt":  int(i["completedAt"]) if "completedAt" in i else None,
            "consentCount": int(i["consentCount"]) if "consentCount" in i else None,
        }
        for i in items
    ]
    return _response(200, {"jobs": jobs, "count": len(jobs)})


def _abort_job(job_id: str, claims: dict) -> dict:
    """POST /ota/deployments/{jobId}/abort."""
    actor = claims.get("email", claims.get("sub", "unknown"))
    _log("info", "abort_job_start", jobId=job_id, requestedBy=actor)

    try:
        job_item = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item", {})
        if not job_item:
            _log("warning", "abort_job_not_found", jobId=job_id, requestedBy=actor)
            return _response(404, {"error": f"Job {job_id} not found"})

        status      = job_item.get("status", "")
        target_type = job_item.get("targetType")
        target_id   = job_item.get("targetId")

        if status == "SUCCEEDED":
            _log("warning", "abort_job_rejected_already_succeeded",
                 jobId=job_id, requestedBy=actor)
            return _response(400, {"error": "Cannot abort a SUCCEEDED deployment. Use Rollback instead."})

        _log("info", "abort_job_current_state",
             jobId=job_id, currentStatus=status,
             targetType=target_type, targetId=target_id,
             requestedBy=actor)

        now_ms = int(time.time() * 1000)

        if status == "AWAITING_CONSENT":
            # Cancel all PENDING consent records for this deployment
            _log("info", "abort_cancelling_consent_records",
                 jobId=job_id, status=status)
            resp = dynamo.Table(CONSENTS_TABLE).query(
                IndexName="deploymentId-index",
                KeyConditionExpression=Key("deploymentId").eq(job_id),
            )
            cancelled = 0
            skipped   = 0
            for c in resp.get("Items", []):
                if c.get("status") == "PENDING":
                    dynamo.Table(CONSENTS_TABLE).update_item(
                        Key={"consentId": c["consentId"]},
                        UpdateExpression="SET #s = :s, cancelledAt = :ts, cancelledBy = :by",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":s":  "CANCELLED",
                            ":ts": now_ms,
                            ":by": actor,
                        },
                    )
                    cancelled += 1
                    _log("debug", "consent_record_cancelled",
                         jobId=job_id, consentId=c["consentId"],
                         deviceId=c.get("deviceId"), cancelledBy=actor)
                else:
                    skipped += 1
                    _log("debug", "consent_record_skip_not_pending",
                         jobId=job_id, consentId=c["consentId"],
                         existingStatus=c.get("status"))
            _log("info", "consent_records_cancelled",
                 jobId=job_id, cancelled=cancelled, skipped=skipped, requestedBy=actor)
        else:
            # Try to cancel the IoT Job (legacy or post-consent jobs)
            iot_job_id = job_item.get("iotJobId", job_id)
            _log("info", "abort_cancelling_iot_job",
                 jobId=job_id, iotJobId=iot_job_id,
                 currentStatus=status, requestedBy=actor)
            try:
                iot.cancel_job(jobId=iot_job_id, force=False)
                _log("info", "iot_job_cancelled",
                     jobId=job_id, iotJobId=iot_job_id)
            except ClientError as ce:
                _log("warning", "iot_job_cancel_failed",
                     jobId=job_id, iotJobId=iot_job_id,
                     awsError=ce.response["Error"]["Code"],
                     awsMessage=ce.response["Error"]["Message"])

        dynamo.Table(OTA_JOBS_TABLE).update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #s = :s, abortedAt = :ts, abortedBy = :by",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":  "CANCELLED",
                ":ts": now_ms,
                ":by": actor,
            },
        )
        _log("info", "deployment_record_marked_cancelled",
             jobId=job_id, abortedBy=actor)

        # Clear pendingJobId from device_data if THING-targeted
        if target_type == "THING" and target_id:
            dev_items = dynamo.Table(DEVICE_DATA_TABLE).query(
                KeyConditionExpression=Key("deviceId").eq(target_id)
            ).get("Items", [])
            if dev_items:
                mac = dev_items[0].get("macAddress", "")
                dynamo.Table(DEVICE_DATA_TABLE).update_item(
                    Key={"deviceId": target_id, "macAddress": mac},
                    UpdateExpression="SET pendingJobId = :null, lastUpdatedAt = :ts",
                    ExpressionAttributeValues={":null": None, ":ts": now_ms},
                )
                _log("debug", "device_pending_job_cleared",
                     deviceId=target_id, macAddress=mac)

        _audit("DEPLOYMENT_ABORTED", actor, {"jobId": job_id}, "SUCCESS",
               abortedBy=actor, targetType=target_type, targetId=target_id,
               previousStatus=status)
        _log("info", "abort_job_complete",
             jobId=job_id, abortedBy=actor, previousStatus=status)
        return _response(200, {"jobId": job_id, "status": "CANCELLED"})

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "abort_job_aws_error",
             jobId=job_id, awsError=code, awsMessage=msg)
        _audit("DEPLOYMENT_ABORT_FAILED", actor, {"jobId": job_id}, "FAILURE",
               awsError=code, awsMessage=msg)
        return _response(400, {"error": msg})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
