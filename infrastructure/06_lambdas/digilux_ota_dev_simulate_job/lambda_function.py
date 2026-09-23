"""
digilux_ota_dev_simulate_job — DEV / TEST ONLY
================================================
Creates an IoT Job directly for a device, bypassing the full
upload → deployment → consent pipeline.

Intended for integration testing by the controller team.
DELETE this Lambda and its API route once integration is complete.

POST /api/v1/ota/dev/simulate-job
Request body:
  { "deviceId": "<uuid>" }          # required
  { "deviceId": "...", "version": "4.6.0" }  # optional: pin a specific version

Response 201:
  { jobId, deviceId, packageName, version, status, presignedUrl }
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION        = os.environ.get("REGION",           "ap-south-1")
ACCOUNT_ID    = os.environ.get("ACCOUNT_ID",        "986906626244")
DEVICE_TABLE  = os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data")
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE",   "digilux_ota_packages")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
IOT_JOB_TIMEOUT_MINUTES = int(os.environ.get("IOT_JOB_TIMEOUT_MINUTES", "1440"))

# Presign expiry tiers (mirrors user_consent Lambda)
_TIER1_MAX_MB = int(os.environ.get("PRESIGN_EXPIRY_TIER1_MAX_MB", "50"))
_TIER1_SEC    = int(os.environ.get("PRESIGN_EXPIRY_TIER1_SEC",    "3600"))
_TIER2_MAX_MB = int(os.environ.get("PRESIGN_EXPIRY_TIER2_MAX_MB", "200"))
_TIER2_SEC    = int(os.environ.get("PRESIGN_EXPIRY_TIER2_SEC",    "21600"))
_TIER3_MAX_MB = int(os.environ.get("PRESIGN_EXPIRY_TIER3_MAX_MB", "500"))
_TIER3_SEC    = int(os.environ.get("PRESIGN_EXPIRY_TIER3_SEC",    "86400"))
_TIER4_SEC    = int(os.environ.get("PRESIGN_EXPIRY_TIER4_SEC",    "172800"))

OPERATION_TYPE_MAP = {
    "Network_controller_firmware":              1,
    "Network_controller_zigbee_firmware":       2,
    "Network_controller_Z2M_Firmware":          3,
    "Network_controller_zigbee_stack_firmware": 4,
    "Network_controller_Miscellaneous":         5,
}

dynamodb = boto3.resource("dynamodb", region_name=REGION)
iot      = boto3.client("iot", region_name=REGION)
s3       = boto3.client("s3", region_name=REGION)


# ── helpers ───────────────────────────────────────────────────────────────────

def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _presign_expiry(size_bytes: int) -> int:
    mb = size_bytes / (1024 * 1024)
    if mb <= _TIER1_MAX_MB: return _TIER1_SEC
    if mb <= _TIER2_MAX_MB: return _TIER2_SEC
    if mb <= _TIER3_MAX_MB: return _TIER3_SEC
    return _TIER4_SEC


def _get_device(device_id: str) -> dict | None:
    table  = dynamodb.Table(DEVICE_TABLE)
    result = table.query(KeyConditionExpression=Key("deviceId").eq(device_id))
    items  = result.get("Items", [])
    return items[0] if items else None


def _get_latest_active_package(package_name: str, pinned_version: str | None) -> dict | None:
    table = dynamodb.Table(PACKAGES_TABLE)
    if pinned_version:
        resp = table.get_item(Key={"packageName": package_name, "version": pinned_version})
        pkg  = resp.get("Item")
        if not pkg:
            return None
        if pkg.get("status") != "ACTIVE" or not pkg.get("activated"):
            return None
        return pkg

    # Scan all versions for this package, filter ACTIVE + activated
    result = table.query(
        KeyConditionExpression=Key("packageName").eq(package_name),
        FilterExpression=Attr("status").eq("ACTIVE") & Attr("activated").eq(True),
    )
    candidates = result.get("Items", [])
    if not candidates:
        return None

    # Sort by version string descending — good enough for dev testing
    candidates.sort(key=lambda p: p.get("version", ""), reverse=True)
    return candidates[0]


def _presign(enc_key: str, expiry_sec: int) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": ARTIFACT_BUCKET, "Key": enc_key},
        ExpiresIn=expiry_sec,
    )


# ── handler ───────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    raw_body  = event.get("body") or "{}"
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        return _resp(400, {"error": "Request body must be valid JSON"})

    device_id       = body.get("deviceId", "").strip()
    pinned_version  = body.get("version", "").strip() or None

    if not device_id:
        return _resp(400, {"error": "deviceId is required"})

    # 1 ── Device lookup
    dev = _get_device(device_id)
    if not dev:
        return _resp(404, {"error": f"Device {device_id} not found"})

    thing_name = dev.get("thingName")
    if not thing_name:
        return _resp(409, {"error": "Device has no thingName — OTA agent not yet registered"})

    package_name = (dev.get("package") or {}).get("name")
    if not package_name:
        return _resp(409, {"error": "Device has no package name — device not fully registered"})

    # 2 ── Block if a job is already running
    existing_job = dev.get("pendingJobId")
    if existing_job:
        return _resp(409, {
            "error": "Device already has an active job. Cancel it first or wait for it to complete.",
            "pendingJobId": existing_job,
        })

    # 3 ── Package lookup
    pkg = _get_latest_active_package(package_name, pinned_version)
    if not pkg:
        label = f"{package_name}@{pinned_version}" if pinned_version else package_name
        return _resp(404, {"error": f"No ACTIVE package found for {label}"})

    version      = pkg["version"]
    enc_s3_key   = pkg.get("encS3Key") or pkg.get("s3Key", "")
    sha256       = pkg.get("sha256", "")
    signature    = pkg.get("signature", "")
    artifact_size = int(pkg.get("artifactSize", 0) or 0)
    device_type  = pkg.get("deviceType", "")

    if not enc_s3_key:
        return _resp(500, {"error": f"Package {package_name}@{version} has no encrypted artifact key"})

    # 4 ── Presigned URL
    expiry_sec   = _presign_expiry(artifact_size)
    presigned_url = _presign(enc_s3_key, expiry_sec)

    # 5 ── Build job document (identical structure to production job)
    operation_type = OPERATION_TYPE_MAP.get(device_type, 0)
    job_id = (
        f"digilux-ota-dev-{package_name}-{version}-{int(time.time())}"
        .replace(".", "-")
    )
    job_doc = {
        "operationType": operation_type,
        "packageName":   package_name,
        "version":       version,
        "artifact": {
            "presignedUrl": presigned_url,
            "sha256":       sha256,
            "signature":    signature,
            "size":         artifact_size,
        },
        "mandatory": True,
        "rollback":  True,
    }

    # 6 ── Create IoT Job
    thing_arn = f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"
    log.info(json.dumps({"event": "dev_simulate_job_create", "jobId": job_id,
                         "deviceId": device_id, "thingName": thing_name,
                         "packageName": package_name, "version": version}))
    iot.create_job(
        jobId=job_id,
        targets=[thing_arn],
        document=json.dumps(job_doc),
        description=f"[DEV] Simulated OTA: {package_name} → {version}",
        jobExecutionsRolloutConfig={"maximumPerMinute": 1},
        timeoutConfig={"inProgressTimeoutInMinutes": IOT_JOB_TIMEOUT_MINUTES},
        tags=[
            {"Key": "Project",     "Value": "digilux"},
            {"Key": "Component",   "Value": "ota-dev"},
            {"Key": "PackageName", "Value": package_name},
            {"Key": "Version",     "Value": version},
        ],
    )

    # 7 ── Set pendingJobId on device
    mac = dev.get("macAddress", "")
    dynamodb.Table(DEVICE_TABLE).update_item(
        Key={"deviceId": device_id, "macAddress": mac},
        UpdateExpression="SET pendingJobId = :j, lastUpdatedAt = :ts",
        ExpressionAttributeValues={
            ":j":  job_id,
            ":ts": int(time.time() * 1000),
        },
    )

    log.info(json.dumps({"event": "dev_simulate_job_done", "jobId": job_id,
                         "deviceId": device_id, "packageName": package_name,
                         "version": version}))

    return _resp(201, {
        "jobId":        job_id,
        "deviceId":     device_id,
        "packageName":  package_name,
        "version":      version,
        "status":       "QUEUED",
        "presignedUrl": presigned_url,
        "message":      f"[DEV] IoT Job created — controller can now download {package_name} {version}",
    })
