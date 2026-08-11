"""
digilux_ota_user_get_download_link
Returns a pre-signed S3 download URL for a firmware package AND simultaneously
publishes the download payload to the device's OTA MQTT topic so the device
can begin downloading immediately without waiting for an IoT Job.

POST /api/v1/ota/my/updates/download-link

Auth: Cognito ID token (any authenticated user).
The device must be owned by the calling user.

Body:
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0"
}

Response 200:
{
  "downloadUrl":   "<pre-signed S3 URL, 1-hour TTL>",
  "sha256":        "<hex digest>",
  "signature":     "<ECDSA P-256 base64 signature>",
  "packageName":   "controller-app",
  "version":       "4.0.0",
  "size":          12345678,
  "packageType":   "deb",
  "expiresAt":     "<ISO-8601>",
  "mqttDelivered": true,
  "message":       "Download URL sent to device via MQTT. Device will begin download shortly."
}

Flow:
  1. Validate JWT → extract userId (sub claim)
  2. Validate request body fields and formats
  3. Verify device ownership (userId-index GSI on digilux_device_data)
  4. Fetch package from digilux_ota_packages — must be ACTIVE
  5. Fetch device inventory — must be registered; get thingName
  6. Version must be newer than currently installed
  7. No other update already in-progress (pendingJobId must be null)
  8. Generate pre-signed S3 GET URL
  9. Publish download payload to iot/device/{deviceId}/ota MQTT topic
 10. Return payload to Flutter app

Security checks (same as consent, without IoT Job overhead):
  1. Valid Cognito JWT with sub claim
  2. UUID format validation for deviceId
  3. Device ownership — deviceId in digilux_device_data under this userId
  4. Package ACTIVE
  5. Device registered in OTA inventory
  6. Version strictly newer than installed
  7. No pending job already in-progress
"""
import datetime
import json
import logging
import os
import re
import time

from decimal import Decimal

import base64

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION             = os.environ["REGION"]
DEVICE_DATA_TABLE  = os.environ.get("DEVICE_DATA_TABLE",  "digilux_device_data")
PACKAGES_TABLE     = os.environ.get("PACKAGES_TABLE",      "digilux_ota_packages")
ARTIFACT_BUCKET    = os.environ.get("ARTIFACT_BUCKET",     "digilux-ota-artifacts")
# Pre-signed URL expiry tiers — configured via ota.env / Lambda env vars
_TIER1_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER1_MAX_MB", "50"))
_TIER1_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER1_SEC",    "3600"))
_TIER2_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER2_MAX_MB", "200"))
_TIER2_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER2_SEC",    "21600"))
_TIER3_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER3_MAX_MB", "500"))
_TIER3_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER3_SEC",    "86400"))
_TIER4_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER4_SEC",    "172800"))

# CloudFront signed URL config
CLOUDFRONT_DOMAIN             = os.environ.get("CLOUDFRONT_DOMAIN", "")
CLOUDFRONT_KEY_PAIR_ID        = os.environ.get("CLOUDFRONT_KEY_PAIR_ID", "")
CLOUDFRONT_PRIVATE_KEY_SECRET = os.environ.get("CLOUDFRONT_PRIVATE_KEY_SECRET", "digilux-ota-cloudfront-key")

_cf_private_key_cache = None

# OTA MQTT topic template — same topic the existing IoT Job agent already listens on
OTA_MQTT_TOPIC_TEMPLATE = os.environ.get(
    "OTA_MQTT_TOPIC_TEMPLATE",
    "iot/device/{deviceId}/ota",
)

_MAX_BODY_BYTES = 2048
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"^[a-zA-Z0-9.\-_]{1,32}$")

dynamo   = boto3.resource("dynamodb", region_name=REGION)
s3       = boto3.client("s3",         region_name=REGION)
iot_data = boto3.client("iot-data",   region_name=REGION)


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


def _presign_expiry(artifact_size_bytes: int) -> int:
    """Return pre-signed URL TTL in seconds based on artifact size tiers."""
    size_mb = artifact_size_bytes / (1024 * 1024)
    if size_mb <= _TIER1_MAX_MB:
        return _TIER1_SEC
    elif size_mb <= _TIER2_MAX_MB:
        return _TIER2_SEC
    elif size_mb <= _TIER3_MAX_MB:
        return _TIER3_SEC
    return _TIER4_SEC


def _get_cf_private_key():
    global _cf_private_key_cache
    if _cf_private_key_cache:
        return _cf_private_key_cache
    sm = boto3.client("secretsmanager", region_name=REGION)
    pem = sm.get_secret_value(SecretId=CLOUDFRONT_PRIVATE_KEY_SECRET)["SecretString"]
    _cf_private_key_cache = serialization.load_pem_private_key(pem.encode(), password=None)
    return _cf_private_key_cache


def _cf_b64(data: bytes) -> str:
    return base64.b64encode(data).decode().replace("+", "-").replace("=", "_").replace("/", "~")


def _cloudfront_signed_url(s3_key: str, expiry_sec: int) -> str | None:
    if not CLOUDFRONT_DOMAIN or not CLOUDFRONT_KEY_PAIR_ID:
        return None
    expire_epoch = int(time.time()) + expiry_sec
    resource_url = f"https://{CLOUDFRONT_DOMAIN}/{s3_key}"
    policy = json.dumps(
        {"Statement": [{"Resource": resource_url, "Condition": {"DateLessThan": {"AWS:EpochTime": expire_epoch}}}]},
        separators=(",", ":"),
    )
    private_key = _get_cf_private_key()
    signature = private_key.sign(policy.encode(), padding.PKCS1v15(), hashes.SHA1())
    return (
        f"{resource_url}"
        f"?Policy={_cf_b64(policy.encode())}"
        f"&Signature={_cf_b64(signature)}"
        f"&Key-Pair-Id={CLOUDFRONT_KEY_PAIR_ID}"
    )


def _get_artifact_url(pkg: dict, expiry_sec: int) -> str:
    cf_url = _cloudfront_signed_url(pkg["s3Key"], expiry_sec)
    if cf_url:
        return cf_url
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": pkg["s3Bucket"], "Key": pkg["s3Key"]},
        ExpiresIn=expiry_sec,
    )


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


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_device_item(device_id: str) -> dict | None:
    """
    Query digilux_device_data by deviceId (hash key). Returns the first item or None.
    The item carries userId (for ownership check) and OTA fields
    (installedVersions, pendingJobId, thingName).
    """
    tbl  = dynamo.Table(DEVICE_DATA_TABLE)
    resp = tbl.query(KeyConditionExpression=Key("deviceId").eq(device_id))
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_package(package_name: str, version: str) -> dict | None:
    tbl = dynamo.Table(PACKAGES_TABLE)
    return tbl.get_item(Key={"packageName": package_name, "version": version}).get("Item")


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    try:
        # ── 1. Auth ───────────────────────────────────────────────────────────
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            log.warning("Missing sub claim in token")
            return _resp(401, {"error": "Unauthorized — invalid token"})

        email = claims.get("email", user_id)
        log.info(json.dumps({"msg": "download_link_request", "userId": user_id, "email": email}))

        # ── 2. Body size guard ────────────────────────────────────────────────
        raw_body = event.get("body") or ""
        if len(raw_body) > _MAX_BODY_BYTES:
            return _resp(400, {"error": "Request body too large"})

        body = json.loads(raw_body or "{}")

        # ── 3. Required field presence ────────────────────────────────────────
        for field in ("deviceId", "packageName", "version"):
            if not body.get(field):
                log.warning(f"Missing required field: {field}")
                return _resp(400, {"error": f"Missing required field: {field}"})

        device_id    = body["deviceId"].strip()
        package_name = body["packageName"].strip()
        version      = body["version"].strip()

        # ── 4. Input format validation ────────────────────────────────────────
        if not _UUID_RE.match(device_id):
            return _resp(400, {"error": "Invalid deviceId format"})

        if not package_name or len(package_name) > 64:
            return _resp(400, {"error": "Invalid packageName"})

        if not _VERSION_RE.match(version):
            return _resp(400, {"error": "Invalid version format"})

        # ── 5. Device lookup + ownership check (single query) ────────────────
        log.info(f"Looking up device: userId={user_id}, deviceId={device_id}")
        dev = _get_device_item(device_id)
        if not dev or dev.get("userId") != user_id:
            log.warning(json.dumps({
                "msg":      "ownership_check_failed",
                "userId":   user_id,
                "deviceId": device_id,
            }))
            _audit("DOWNLOAD_LINK_REJECTED", user_id,
                   {"deviceId": device_id, "packageName": package_name, "version": version},
                   "FAILURE", reason="device_not_owned_by_user")
            return _resp(404, {"error": "Device not found"})

        # ── 6. Package lookup — must exist and be ACTIVE ──────────────────────
        pkg = _get_package(package_name, version)
        if not pkg:
            return _resp(404, {"error": f"Package {package_name}@{version} not found"})

        if pkg.get("status") != "ACTIVE":
            return _resp(400, {
                "error": f"Package {package_name}@{version} is not available for installation"
            })

        # ── 7. OTA agent must have registered (thingName present) ────────────
        thing_name = dev.get("thingName")
        if not thing_name:
            log.warning(f"Device {device_id} has no thingName — OTA agent not yet started")
            return _resp(409, {
                "error": (
                    "Device OTA agent has not started yet. "
                    "Please ensure the device is online and the OTA agent is running."
                )
            })

        # ── 8. Version check — must be strictly newer ─────────────────────────
        installed_ver = (dev.get("installedVersions") or {}).get(package_name)
        if installed_ver and not _is_newer(version, installed_ver):
            if installed_ver == version:
                return _resp(409, {
                    "error": f"{package_name} version {version} is already installed on this device."
                })
            return _resp(409, {
                "error": (
                    f"Requested version {version} is not newer "
                    f"than the installed version {installed_ver}."
                )
            })

        # ── 9. No active update already in-progress ───────────────────────────
        pending_job_id = dev.get("pendingJobId")
        if pending_job_id:
            return _resp(409, {
                "error": "An update is already in progress on this device.",
                "pendingJobId": pending_job_id,
            })

        # ── 10. Generate pre-signed S3 GET URL ───────────────────────────────
        artifact_size = pkg.get("artifactSize", 0)
        if isinstance(artifact_size, Decimal):
            artifact_size = int(artifact_size)

        expiry_sec = _presign_expiry(artifact_size)
        log.info(f"Presign expiry for {package_name}@{version} ({artifact_size} bytes): {expiry_sec}s")

        presigned_url = _get_artifact_url(pkg, expiry_sec)
        expires_at = (
            datetime.datetime.utcnow() + datetime.timedelta(seconds=expiry_sec)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        log.info(
            f"Pre-signed URL generated for {package_name}@{version}, "
            f"expires in {expiry_sec}s"
        )

        # ── 11. Build MQTT payload ────────────────────────────────────────────
        mqtt_payload = {
            "operation":    "DOWNLOAD_AND_INSTALL",
            "packageName":  package_name,
            "version":      version,
            "packageType":  pkg.get("packageType", ""),
            "downloadUrl":  presigned_url,
            "sha256":       pkg.get("sha256", ""),
            "signature":    pkg.get("signature", ""),
            "size":         artifact_size,
            "expiresAt":    expires_at,
            "initiatedBy":  "USER_APP",
            "userId":       user_id,
            "mandatory":    False,  # user-requested; device can defer if mid-task
            "rollback":     True,
        }

        # ── 12. Publish to device MQTT OTA topic ──────────────────────────────
        topic = OTA_MQTT_TOPIC_TEMPLATE.format(deviceId=device_id)
        mqtt_delivered = False

        try:
            iot_data.publish(
                topic=topic,
                qos=1,
                payload=json.dumps(mqtt_payload),
            )
            mqtt_delivered = True
            log.info(json.dumps({
                "msg":         "mqtt_published",
                "topic":       topic,
                "deviceId":    device_id,
                "packageName": package_name,
                "version":     version,
            }))
        except ClientError as mqtt_err:
            # Non-fatal — app still gets the URL and can pass it to the device
            # through another channel or retry.
            log.warning(json.dumps({
                "msg":   "mqtt_publish_failed",
                "error": str(mqtt_err),
                "topic": topic,
            }))

        _audit("DOWNLOAD_LINK_ISSUED", user_id,
               {"deviceId": device_id, "packageName": package_name, "version": version},
               "SUCCESS",
               mqttDelivered=mqtt_delivered, artifactSize=artifact_size,
               thingName=thing_name)

        log.info(json.dumps({
            "msg":           "download_link_issued",
            "userId":        user_id,
            "deviceId":      device_id,
            "packageName":   package_name,
            "version":       version,
            "mqttDelivered": mqtt_delivered,
        }))

        # ── 13. Return payload to Flutter app ─────────────────────────────────
        message = (
            "Download URL sent to device via MQTT. Device will begin download shortly."
            if mqtt_delivered
            else (
                "Download URL generated. Device may be offline — "
                "pass the URL directly to the device when it comes online."
            )
        )

        return _resp(200, {
            "downloadUrl":   presigned_url,
            "sha256":        pkg.get("sha256", ""),
            "signature":     pkg.get("signature", ""),
            "packageName":   package_name,
            "version":       version,
            "size":          artifact_size,
            "packageType":   pkg.get("packageType", ""),
            "expiresAt":     expires_at,
            "mqttDelivered": mqtt_delivered,
            "message":       message,
        })

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        log.error(json.dumps({"msg": "aws_client_error", "code": code, "error": msg}))
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        log.exception(f"Unhandled error in user_get_download_link handler: {e}")
        return _resp(500, {"error": "Internal server error"})
