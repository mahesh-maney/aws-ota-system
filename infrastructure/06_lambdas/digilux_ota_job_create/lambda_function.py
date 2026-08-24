"""
digilux_ota_job_create
Creates an AWS IoT Job targeting a single device or a Thing Group.
Mandatory updates: admin triggers, device cannot decline.
POST /api/v1/ota/deployments
Admin-only.

Body:
{
  "packageName": "controller-app",
  "version": "2.0.0",
  "targetType": "THING" | "THING_GROUP",
  "targetId": "deviceId-uuid"  |  "DGX-Canary",
  "rolloutStage": "CANARY" | "UAT" | "PRODUCTION",
  "rolloutConfig": {}           # optional overrides
}
"""
import datetime
import json
import logging
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)


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


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

REGION = os.environ["REGION"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")
INVENTORY_TABLE = os.environ.get("INVENTORY_TABLE", "digilux_device_inventory")
OTA_JOBS_TABLE = os.environ.get("OTA_JOBS_TABLE", "digilux_ota_jobs")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
PRESIGN_EXPIRY_SEC = int(os.environ.get("PRESIGN_EXPIRY_SEC", "3600"))
BETA_USERS_TABLE = os.environ.get("BETA_USERS_TABLE", "digilux_ota_beta_users")

# Mapping: deviceType string → integer operation-type code sent to controller.
# Controller firmware maintains the reverse mapping on its end.
OPERATION_TYPE_MAP = {
    "Network_controller_firmware":          1,
    "Network_controller_zigbee_firmware":   2,
    "Network_controller_Z2M_Firmware":      3,
    "Network_controller_Miscellaneous":     4,
}

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot = boto3.client("iot", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

# Staged rollout defaults (startup scale)
ROLLOUT_CONFIGS = {
    "BETA": {
        "maximumPerMinute": 2,
    },
    "CUSTOM": {
        "maximumPerMinute": 2,
    },
    "CANARY": {
        "maximumPerMinute": 2,
    },
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

ABORT_CONFIG = {
    "criteriaList": [
        {
            "failureType": "FAILED",
            "action": "CANCEL",
            "thresholdPercentage": 10.0,
            "minNumberOfExecutedThings": 3,
        },
        {
            "failureType": "TIMED_OUT",
            "action": "CANCEL",
            "thresholdPercentage": 20.0,
            "minNumberOfExecutedThings": 3,
        },
    ]
}


def lambda_handler(event, context):
    method      = event.get("httpMethod", "POST").upper()
    path_params = event.get("pathParameters") or {}
    job_id_param = path_params.get("jobId")
    log.info(json.dumps({"msg": "request_received", "method": method,
                         "jobId": job_id_param, "path": event.get("path", "")}))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "ota-admin" not in claims.get("cognito:groups", ""):
            log.warning("Unauthorized access attempt — not in admin group")
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))
        log.debug(json.dumps({"msg": "actor_identified", "actor": caller}))

        if method == "GET" and job_id_param:
            log.info(f"GET job status request for jobId={job_id_param} by {caller}")
            return _get_job(job_id_param)
        if method == "GET":
            log.info(f"GET list deployments request by {caller}")
            return _list_jobs(event)
        if method == "POST" and job_id_param:
            log.info(f"POST abort request for jobId={job_id_param} by {caller}")
            return _abort_job(job_id_param, claims)

        body = json.loads(event.get("body") or "{}")
        log.debug(json.dumps({"msg": "request_body_parsed", "keys": list(body.keys())}))

        for field in ["packageName", "version"]:
            if not body.get(field):
                log.warning(f"Validation failed — missing required field: {field}")
                return _response(400, {"error": f"Missing required field: {field}"})

        pkg_name      = body["packageName"]
        version       = body["version"]
        rollout_stage = body.get("rolloutStage", "PRODUCTION").upper()

        if rollout_stage not in ROLLOUT_CONFIGS:
            log.warning(f"Invalid rolloutStage: {rollout_stage}")
            return _response(400, {"error": f"rolloutStage must be one of: {', '.join(ROLLOUT_CONFIGS)}"})

        # BETA: resolve targets from selected deviceIds (sent by UI)
        if rollout_stage == "BETA":
            target_ids = body.get("targetIds", [])
            if not target_ids:
                return _response(400, {"error": "No beta users selected. Select at least one beta user."})
            # Look up thingName for each selected deviceId from beta users table
            beta_table = dynamo.Table(BETA_USERS_TABLE)
            iot_targets = []
            resolved_emails = []
            for device_id in target_ids:
                result = beta_table.scan(
                    FilterExpression=boto3.dynamodb.conditions.Attr("deviceId").eq(device_id)
                )
                items = result.get("Items", [])
                if items and items[0].get("thingName"):
                    iot_targets.append(f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{items[0]['thingName']}")
                    resolved_emails.append(items[0].get("email", device_id))
                else:
                    log.warning(f"Beta user with deviceId={device_id} not found or has no thingName — skipping")
            if not iot_targets:
                return _response(400, {"error": "None of the selected beta users have a registered device."})
            target_type = "THING_LIST"
            target_id   = ",".join(resolved_emails)
            log.info(json.dumps({
                "msg": "deployment_request",
                "actor": caller, "packageName": pkg_name, "version": version,
                "rolloutStage": rollout_stage, "betaUserCount": len(iot_targets),
                "targets": iot_targets,
            }))
        elif rollout_stage == "CUSTOM":
            target_ids = body.get("targetIds", [])
            if not target_ids:
                return _response(400, {"error": "No device IDs provided. Add at least one device ID for a CUSTOM deployment."})
            # Validate package is tagged as CUSTOM
            pkg_release_type = pkg.get("releaseType", "")
            if pkg_release_type != "CUSTOM":
                return _response(400, {
                    "error": f"Package {pkg_name}@{version} is tagged as '{pkg_release_type}', not 'CUSTOM'. "
                             "Only CUSTOM-tagged artefacts can be used for a CUSTOM deployment."
                })
            # Resolve thingName for each deviceId from digilux_device_data
            device_table = dynamo.Table(os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data"))
            iot_targets = []
            resolved_ids = []
            for device_id in target_ids:
                items = device_table.query(
                    KeyConditionExpression=boto3.dynamodb.conditions.Key("deviceId").eq(device_id)
                ).get("Items", [])
                if not items:
                    log.warning(f"DeviceId {device_id} not found in device_data — skipping")
                    continue
                thing_name = items[0].get("thingName") or device_id
                iot_targets.append(f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}")
                resolved_ids.append(device_id)
            if not iot_targets:
                return _response(400, {"error": "None of the provided device IDs were found in the system."})
            target_type = "THING_LIST"
            target_id   = ",".join(resolved_ids)
            log.info(json.dumps({
                "msg": "deployment_request",
                "actor": caller, "packageName": pkg_name, "version": version,
                "rolloutStage": rollout_stage, "deviceCount": len(iot_targets),
                "targets": iot_targets,
            }))
        else:
            target_type = (body.get("targetType") or "").upper()
            target_id   = body.get("targetId", "")
            if not target_type or not target_id:
                return _response(400, {"error": "Missing required fields: targetType, targetId"})
            if target_type not in ("THING", "THING_GROUP"):
                log.warning(f"Invalid targetType: {target_type}")
                return _response(400, {"error": "targetType must be THING or THING_GROUP"})
            log.info(json.dumps({
                "msg": "deployment_request",
                "actor": caller, "packageName": pkg_name, "version": version,
                "targetType": target_type, "targetId": target_id, "rolloutStage": rollout_stage,
            }))

        # Look up package
        pkg_table = dynamo.Table(PACKAGES_TABLE)
        log.debug(f"Looking up package {pkg_name}@{version}")
        pkg = pkg_table.get_item(Key={"packageName": pkg_name, "version": version}).get("Item")
        if not pkg:
            log.warning(f"Package not found: {pkg_name}@{version}")
            return _response(404, {"error": f"Package {pkg_name}@{version} not found"})
        if pkg.get("status") != "ACTIVE":
            log.warning(f"Package {pkg_name}@{version} is {pkg.get('status')}, not ACTIVE")
            return _response(400, {"error": f"Package {pkg_name}@{version} is not ACTIVE"})

        # Resolve IoT targets for non-BETA stages
        if rollout_stage != "BETA":
            if target_type == "THING":
                log.debug(f"Looking up device inventory for deviceId={target_id}")
                inv = dynamo.Table(INVENTORY_TABLE).get_item(Key={"deviceId": target_id}).get("Item", {})
                if not inv:
                    log.warning(f"Device not found in inventory: {target_id}")
                    return _response(404, {
                        "error": f"Device {target_id} not found in OTA inventory. "
                                 "Ensure the OTA agent is running on the device."
                    })
                current_version = inv.get("installedVersions", {}).get(pkg_name)
                log.debug(f"Device {target_id} current {pkg_name} version: {current_version}")
                if current_version == version:
                    log.info(f"Deployment rejected — device {target_id} already has {pkg_name}@{version}")
                    _audit("DEPLOYMENT_REJECTED", caller,
                           {"packageName": pkg_name, "version": version, "deviceId": target_id},
                           "FAILURE", reason="already_installed", currentVersion=current_version)
                    return _response(400, {
                        "error": f"Device already has {pkg_name}@{version} installed. "
                                 "No deployment needed."
                    })
                thing_name = inv.get("thingName")
                if not thing_name:
                    log.error(f"Device {target_id} has no thingName in inventory")
                    return _response(404, {
                        "error": f"Device {target_id} has no thingName in inventory. "
                                 "Ensure the OTA agent is running on the device."
                    })
                log.info(f"Device resolved: deviceId={target_id} → thingName={thing_name}")
                iot_targets = [f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"]
            else:
                log.info(f"Group target: {target_id}")
                iot_targets = [f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thinggroup/{target_id}"]

        # Generate pre-signed GET URL for artifact download (embedded in IoT Job document)
        log.debug(f"Generating pre-signed GET URL for s3://{pkg['s3Bucket']}/{pkg['s3Key']}")
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": pkg["s3Bucket"], "Key": pkg["s3Key"]},
            ExpiresIn=PRESIGN_EXPIRY_SEC,
        )
        log.info(f"Pre-signed GET URL generated, expires in {PRESIGN_EXPIRY_SEC}s")

        artifact_size = pkg.get("artifactSize", 0)
        if isinstance(artifact_size, Decimal):
            artifact_size = int(artifact_size)

        device_type    = pkg.get("deviceType", "")
        operation_type = OPERATION_TYPE_MAP.get(device_type, 0)

        job_doc = {
            "operationType": operation_type,
            "packageName": pkg_name,
            "version": version,
            "artifact": {
                "presignedUrl": presigned_url,
                "sha256": pkg["sha256"],
                "signature": pkg["signature"],
                "size": artifact_size,
            },
            "rollback": True,
        }
        if body.get("parameters"):
            job_doc["parameters"] = body["parameters"]

        rollout_cfg = ROLLOUT_CONFIGS[rollout_stage].copy()
        if body.get("rolloutConfig"):
            rollout_cfg.update(body["rolloutConfig"])
            log.debug(f"Rollout config overridden: {body['rolloutConfig']}")

        job_id = f"digilux-ota-{pkg_name}-{version}-{int(time.time())}".replace(".", "-")
        log.info(json.dumps({
            "msg": "creating_iot_job",
            "jobId": job_id, "targets": iot_targets,
            "rolloutStage": rollout_stage, "artifactSize": artifact_size,
        }))

        create_args = {
            "jobId": job_id,
            "targets": iot_targets,
            "document": json.dumps(job_doc),
            "description": f"{pkg.get('deviceType', '')} update: {pkg_name} to {version}",
            "jobExecutionsRolloutConfig": rollout_cfg,
            "abortConfig": ABORT_CONFIG,
            "timeoutConfig": {"inProgressTimeoutInMinutes": 60},
            "tags": [
                {"Key": "Project", "Value": "digilux"},
                {"Key": "Component", "Value": "ota"},
                {"Key": "PackageName", "Value": pkg_name},
                {"Key": "Version", "Value": version},
            ],
        }

        iot_resp = iot.create_job(**create_args)
        log.info(f"IoT Job created: arn={iot_resp['jobArn']}")

        now_ms = int(time.time() * 1000)
        dynamo.Table(OTA_JOBS_TABLE).put_item(Item={
            "jobId": job_id,
            "iotJobArn": iot_resp["jobArn"],
            "packageName": pkg_name,
            "version": version,
            "deviceType": pkg.get("deviceType", ""),
            "targetType": target_type,
            "targetId": target_id,
            "rolloutStage": rollout_stage,
            "status": "QUEUED",
            "createdAt": now_ms,
            "createdBy": caller,
            "deviceStatuses": {},
        })
        log.info(f"Job record written to {OTA_JOBS_TABLE}")

        if target_type == "THING":
            dynamo.Table(INVENTORY_TABLE).update_item(
                Key={"deviceId": target_id},
                UpdateExpression="SET pendingJobId = :jid, lastUpdatedAt = :ts",
                ExpressionAttributeValues={":jid": job_id, ":ts": now_ms},
            )
            log.debug(f"Device {target_id} pendingJobId set to {job_id}")

        _audit("DEPLOYMENT_CREATED", caller,
               {"packageName": pkg_name, "version": version},
               "SUCCESS",
               jobId=job_id, iotJobArn=iot_resp["jobArn"],
               targetType=target_type, targetId=target_id,
               rolloutStage=rollout_stage, artifactSize=artifact_size,
               deviceType=pkg.get("deviceType", ""))

        log.info(json.dumps({
            "msg": "deployment_created",
            "jobId": job_id, "packageName": pkg_name, "version": version,
            "targetType": target_type, "targetId": target_id,
            "rolloutStage": rollout_stage, "createdBy": caller,
        }))

        return _response(201, {
            "jobId": job_id,
            "iotJobArn": iot_resp["jobArn"],
            "packageName": pkg_name,
            "version": version,
            "targetType": target_type,
            "targetId": target_id,
            "rolloutStage": rollout_stage,
            "status": "QUEUED",
        })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        log.error(json.dumps({"msg": "aws_client_error", "code": code, "error": msg}))
        return _response(500, {"error": f"AWS error: {msg}"})
    except Exception as e:
        log.exception(f"Unhandled error in job_create handler: {e}")
        return _response(500, {"error": "Internal server error"})


def _get_job(job_id: str) -> dict:
    """GET /api/v1/ota/deployments/{jobId} — job status from DynamoDB + live IoT status."""
    log.debug(f"Fetching job record from DynamoDB: {job_id}")
    item = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        log.warning(f"Job not found: {job_id}")
        return _response(404, {"error": f"Job {job_id} not found"})

    log.debug(f"Enriching with live IoT Job status for {job_id}")
    try:
        iot_job = iot.describe_job(jobId=job_id)["job"]
        item["iotStatus"]    = iot_job.get("jobProcessDetails", {})
        item["iotJobStatus"] = iot_job.get("status")
        log.info(json.dumps({
            "msg": "job_status_fetched", "jobId": job_id,
            "dbStatus": item.get("status"), "iotStatus": item["iotJobStatus"],
        }))
    except Exception as e:
        log.warning(f"Could not fetch live IoT status for {job_id}: {e}")

    return _response(200, json.loads(json.dumps(item, cls=_DecimalEncoder)))


def _list_jobs(event: dict) -> dict:
    """GET /api/v1/ota/deployments — list OTA jobs, newest first."""
    params = event.get("queryStringParameters") or {}
    limit  = min(int(params.get("limit", 20)), 100)

    result = dynamo.Table(OTA_JOBS_TABLE).scan()
    items  = result.get("Items", [])
    items.sort(key=lambda x: int(x.get("createdAt", 0)), reverse=True)
    items  = items[:limit]

    jobs = [
        {
            "jobId":        i.get("jobId"),
            "packageName":  i.get("packageName"),
            "version":      i.get("version"),
            "deviceType": i.get("deviceType"),
            "targetType":   i.get("targetType"),
            "targetId":     i.get("targetId"),
            "rolloutStage": i.get("rolloutStage"),
            "status":       i.get("status"),
            "createdBy":    i.get("createdBy"),
            "createdAt":    int(i["createdAt"]) if "createdAt" in i else None,
            "completedAt":  int(i["completedAt"]) if "completedAt" in i else None,
        }
        for i in items
    ]
    return _response(200, {"jobs": jobs, "count": len(jobs)})


def _abort_job(job_id: str, claims: dict) -> dict:
    """POST /api/v1/ota/deployments/{jobId}/abort."""
    actor = claims.get("email", claims.get("sub", "unknown"))
    log.info(f"Abort requested for jobId={job_id} by {actor}")
    try:
        # Fetch job record before cancelling — need targetType/targetId to clean up inventory
        log.debug(f"Fetching job record for abort: {job_id}")
        job_item = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item", {})
        target_type = job_item.get("targetType")
        target_id   = job_item.get("targetId")
        log.info(json.dumps({
            "msg": "abort_job_details",
            "jobId": job_id, "targetType": target_type, "targetId": target_id,
        }))

        iot.cancel_job(jobId=job_id, force=False)
        log.info(f"IoT Job {job_id} cancelled in IoT Core")

        now_ms = int(time.time() * 1000)
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
        log.debug(f"Job {job_id} status set to CANCELLED in {OTA_JOBS_TABLE}")

        # Clear pendingJobId from device inventory if this was a THING-targeted deployment
        if target_type == "THING" and target_id:
            log.info(f"Clearing pendingJobId for device {target_id} after abort")
            dynamo.Table(INVENTORY_TABLE).update_item(
                Key={"deviceId": target_id},
                UpdateExpression="SET pendingJobId = :null, lastUpdatedAt = :ts",
                ExpressionAttributeValues={":null": None, ":ts": now_ms},
            )
            log.info(f"Device {target_id} pendingJobId cleared after job abort")

        _audit("DEPLOYMENT_ABORTED", actor,
               {"jobId": job_id}, "SUCCESS",
               abortedBy=actor, targetType=target_type, targetId=target_id)
        log.info(f"Job {job_id} cancelled successfully by {actor}")
        return _response(200, {"jobId": job_id, "status": "CANCELLED"})
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        log.error(f"Failed to cancel job {job_id}: {msg}")
        _audit("DEPLOYMENT_ABORT_FAILED", actor,
               {"jobId": job_id}, "FAILURE", error=msg)
        return _response(400, {"error": msg})



def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
