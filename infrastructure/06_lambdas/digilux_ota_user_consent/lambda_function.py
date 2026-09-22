"""
digilux_ota_user_consent
Handles user consent for OTA updates.

POST /api/v1/ota/my/updates/consent

Auth: Cognito ID token (any authenticated user).

Body: { "deviceId": "...", "packageName": "...", "version": "...", "accepted": true | false }
  - "accepted" is optional and defaults to true.
  - If a PENDING admin-initiated consent record exists for this device + package + version,
    it is resolved (ACCEPTED or DECLINED).
  - If no pending consent record exists, the update is applied immediately (user-initiated).
"""
import datetime
import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal

import base64
import urllib.parse

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTOR = "user_consent"

REGION              = os.environ["REGION"]
ACCOUNT_ID          = os.environ["ACCOUNT_ID"]
DEVICE_DATA_TABLE   = os.environ.get("DEVICE_DATA_TABLE",   "digilux_device_data")
PACKAGES_TABLE      = os.environ.get("PACKAGES_TABLE",       "digilux_ota_packages")
OTA_JOBS_TABLE      = os.environ.get("OTA_JOBS_TABLE",       "digilux_ota_jobs")
CONSENTS_TABLE      = os.environ.get("CONSENTS_TABLE",       "digilux_ota_user_consents")
ARTIFACT_BUCKET    = os.environ.get("ARTIFACT_BUCKET",    "digilux-ota-artifacts")
RATE_LIMIT_MINUTES = int(os.environ.get("RATE_LIMIT_MINUTES", "5"))

SES_SENDER         = os.environ.get("SES_SENDER", "noreply@iot.digilux.co.in")
SES_REGION         = os.environ.get("SES_REGION", REGION)

# Pre-signed URL expiry tiers
_TIER1_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER1_MAX_MB", "50"))
_TIER1_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER1_SEC",    "3600"))
_TIER2_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER2_MAX_MB", "200"))
_TIER2_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER2_SEC",    "21600"))
_TIER3_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER3_MAX_MB", "500"))
_TIER3_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER3_SEC",    "86400"))
_TIER4_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER4_SEC",    "172800"))

IOT_JOB_TIMEOUT_MINUTES = int(os.environ.get("IOT_JOB_TIMEOUT_MINUTES", "1440"))

# CloudFront — disabled (cost decision 2026-09). Keep vars for safe no-op if re-enabled.
CLOUDFRONT_DOMAIN             = os.environ.get("CLOUDFRONT_DOMAIN", "")
CLOUDFRONT_KEY_PAIR_ID        = os.environ.get("CLOUDFRONT_KEY_PAIR_ID", "")
CLOUDFRONT_PRIVATE_KEY_SECRET = os.environ.get("CLOUDFRONT_PRIVATE_KEY_SECRET", "digilux-ota-cloudfront-key")

_cf_private_key_cache = None

CONSENTS_USER_INDEX = os.environ.get("CONSENTS_USER_INDEX", "userId-deviceId-index")

OPERATION_TYPE_MAP = {
    "Network_controller_firmware":        1,
    "Network_controller_zigbee_firmware": 2,
    "Network_controller_Z2M_Firmware":    3,
    "Network_controller_Miscellaneous":   4,
}

_MAX_BODY_BYTES = 2048
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"^[a-zA-Z0-9.\-_]{1,32}$")

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot",        region_name=REGION)
s3     = boto3.client("s3",         region_name=REGION)
ses    = boto3.client("ses",        region_name=SES_REGION)


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
    getattr(log, level)(json.dumps(record))


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


def _cloudfront_signed_url(s3_key: str, expiry_sec: int) -> str:
    if not CLOUDFRONT_DOMAIN or not CLOUDFRONT_KEY_PAIR_ID:
        return None
    expire_epoch = int(time.time()) + expiry_sec
    resource_url = f"https://{CLOUDFRONT_DOMAIN}/{s3_key}"
    policy = json.dumps({
        "Statement": [{
            "Resource": resource_url,
            "Condition": {"DateLessThan": {"AWS:EpochTime": expire_epoch}},
        }]
    }, separators=(",", ":"))
    private_key = _get_cf_private_key()
    signature   = private_key.sign(policy.encode(), padding.PKCS1v15(), hashes.SHA1())
    return (
        f"{resource_url}"
        f"?Policy={_cf_b64(policy.encode())}"
        f"&Signature={_cf_b64(signature)}"
        f"&Key-Pair-Id={CLOUDFRONT_KEY_PAIR_ID}"
    )


def _get_artifact_url(pkg: dict, expiry_sec: int) -> str:
    """
    Generate a download URL for the encrypted artifact.
    Prefers CloudFront signed URL if CLOUDFRONT_DOMAIN and CLOUDFRONT_KEY_PAIR_ID are set
    (currently disabled — cost decision 2026-09).
    Falls back to S3 presigned URL.

    Uses encS3Key (UUID-based opaque path, set by artifact_processor).
    Falls back to legacy s3Key for packages processed before UUID masking.
    """
    # encS3Key is the UUID-based opaque key (e.g. enc/<uuid>.enc)
    # s3Key is the legacy readable path — kept for backward compat with older packages
    enc_key = pkg.get("encS3Key") or pkg.get("s3Key", "")
    bucket  = ARTIFACT_BUCKET  # always use env var — never trust pkg["s3Bucket"]

    if not enc_key:
        _log("error", "artifact_key_missing",
             packageName=pkg.get("packageName"), version=pkg.get("version"),
             detail="Package record has neither encS3Key nor s3Key — cannot generate download URL")
        raise ValueError(f"Package {pkg.get('packageName')}@{pkg.get('version')} has no artifact S3 key")

    _log("debug", "artifact_url_generation_start",
         encKey=enc_key, expirySeconds=expiry_sec,
         isOpaque=enc_key.startswith("enc/"),
         bucket=bucket)

    cf_url = _cloudfront_signed_url(enc_key, expiry_sec)
    if cf_url:
        _log("info", "artifact_url_cloudfront_used",
             encKey=enc_key, expirySeconds=expiry_sec,
             detail="CloudFront signed URL generated (should be disabled — check env vars)")
        _audit("ARTIFACT_URL_CLOUDFRONT", ACTOR,
               {"encKey": enc_key}, "WARN",
               detail="CloudFront URL generated — expected to be disabled")
        return cf_url

    t_presign = time.monotonic()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": enc_key},
        ExpiresIn=expiry_sec,
    )
    presign_ms = int((time.monotonic() - t_presign) * 1000)
    _log("info", "artifact_url_s3_presigned",
         encKey=enc_key, bucket=bucket,
         expirySeconds=expiry_sec, presignMs=presign_ms,
         isOpaque=enc_key.startswith("enc/"))
    _audit("ARTIFACT_URL_PRESIGNED", ACTOR,
           {"encKey": enc_key, "bucket": bucket},
           "SUCCESS",
           expirySeconds=expiry_sec, presignMs=presign_ms,
           isOpaque=enc_key.startswith("enc/"))
    return url


def _get_device_item(device_id: str) -> dict | None:
    _log("debug", "device_item_lookup", deviceId=device_id)
    resp  = dynamo.Table(DEVICE_DATA_TABLE).query(
        KeyConditionExpression=Key("deviceId").eq(device_id)
    )
    items = resp.get("Items", [])
    if not items:
        _log("debug", "device_item_not_found", deviceId=device_id)
        return None
    _log("debug", "device_item_found",
         deviceId=device_id, userId=items[0].get("userId"),
         thingName=items[0].get("thingName"))
    return items[0]


def _get_package(package_name: str, version: str) -> dict | None:
    _log("debug", "package_lookup", packageName=package_name, version=version)
    item = dynamo.Table(PACKAGES_TABLE).get_item(
        Key={"packageName": package_name, "version": version}
    ).get("Item")
    if item:
        _log("debug", "package_lookup_found",
             packageName=package_name, version=version,
             status=item.get("status"),
             hasEncKey=bool(item.get("encS3Key")),
             hasLegacyKey=bool(item.get("s3Key")))
    else:
        _log("debug", "package_lookup_not_found",
             packageName=package_name, version=version)
    return item


def _check_rate_limit(user_id: str, device_id: str) -> bool:
    cutoff_ms = int((time.time() - RATE_LIMIT_MINUTES * 60) * 1000)
    _log("debug", "rate_limit_check",
         userId=user_id, deviceId=device_id,
         windowMinutes=RATE_LIMIT_MINUTES)
    resp = dynamo.Table(CONSENTS_TABLE).query(
        IndexName=CONSENTS_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id) & Key("deviceId").eq(device_id),
        FilterExpression="#ca > :cutoff",
        ExpressionAttributeNames={"#ca": "consentedAt"},
        ExpressionAttributeValues={":cutoff": cutoff_ms},
        Limit=1,
    )
    is_limited = len(resp.get("Items", [])) > 0
    _log("debug", "rate_limit_result",
         userId=user_id, deviceId=device_id, isRateLimited=is_limited)
    return is_limited


def _get_user_email(user_id: str) -> str | None:
    """Get user email from Cognito user pool (best-effort)."""
    _log("debug", "user_email_lookup_start", userId=user_id)
    try:
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
        if not pool_id:
            _log("warning", "cognito_pool_id_missing",
                 detail="COGNITO_USER_POOL_ID env var not set — cannot look up user email")
            return None
        resp = cognito.admin_get_user(UserPoolId=pool_id, Username=user_id)
        for attr in resp.get("UserAttributes", []):
            if attr["Name"] == "email":
                _log("debug", "user_email_found",
                     userId=user_id, emailMasked=attr["Value"][:3] + "***")
                return attr["Value"]
        _log("debug", "user_email_not_in_attributes", userId=user_id)
    except Exception as e:
        _log("warning", "user_email_lookup_failed",
             userId=user_id, error=str(e),
             detail="Best-effort email lookup failed — will use fallback")
    return None


def _send_decline_email(user_email: str, package_name: str, version: str,
                         device_id: str, reason: str) -> None:
    """Send SES email notifying user that OTA update was not applied."""
    if not user_email:
        _log("warning", "decline_email_skipped",
             packageName=package_name, version=version, deviceId=device_id,
             detail="No email address available — skipping decline notification")
        return
    _log("info", "decline_email_send_start",
         packageName=package_name, version=version,
         deviceId=device_id, recipient=user_email[:3] + "***")
    try:
        subject = f"Digilux OTA Update Not Applied — {package_name} v{version}"
        body_text = (
            f"Hi,\n\n"
            f"The firmware update {package_name} v{version} for device {device_id} "
            f"was not applied.\n\n"
            f"Reason: {reason}\n\n"
            f"If this was a mistake, please open the Digilux app to update your device.\n\n"
            f"— Digilux Team"
        )
        body_html = (
            f"<p>Hi,</p>"
            f"<p>The firmware update <strong>{package_name} v{version}</strong> "
            f"for device <code>{device_id}</code> was <strong>not applied</strong>.</p>"
            f"<p><strong>Reason:</strong> {reason}</p>"
            f"<p>If this was a mistake, please open the Digilux app to update your device.</p>"
            f"<p>— Digilux Team</p>"
        )
        ses.send_email(
            Source=SES_SENDER,
            Destination={"ToAddresses": [user_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": body_text, "Charset": "UTF-8"},
                    "Html": {"Data": body_html, "Charset": "UTF-8"},
                },
            },
        )
        _log("info", "decline_email_sent",
             packageName=package_name, version=version,
             deviceId=device_id, recipient=user_email[:3] + "***")
        _audit("DECLINE_EMAIL_SENT", ACTOR,
               {"packageName": package_name, "version": version, "deviceId": device_id},
               "SUCCESS", recipientMasked=user_email[:3] + "***")
    except Exception as e:
        _log("warning", "decline_email_failed",
             packageName=package_name, version=version,
             deviceId=device_id, error=str(e),
             detail="Non-critical: email send failed — consent is still DECLINED in DB")
        _audit("DECLINE_EMAIL_FAILED", ACTOR,
               {"packageName": package_name, "version": version, "deviceId": device_id},
               "WARN", error=str(e))


def _create_iot_job(pkg: dict, package_name: str, version: str,
                    thing_name: str, device_id: str,
                    user_id: str, job_id: str, expiry_sec: int) -> str:
    _log("info", "iot_job_create_start",
         jobId=job_id, packageName=package_name, version=version,
         thingName=thing_name, deviceId=device_id,
         expirySeconds=expiry_sec)

    t_url = time.monotonic()
    presigned_url = _get_artifact_url(pkg, expiry_sec)
    url_ms = int((time.monotonic() - t_url) * 1000)
    _log("info", "presigned_url_generated",
         jobId=job_id, packageName=package_name, version=version,
         expirySeconds=expiry_sec, urlMs=url_ms,
         detail="URL path is opaque UUID — no package info leaked")

    artifact_size = pkg.get("artifactSize", 0)
    if isinstance(artifact_size, Decimal):
        artifact_size = int(artifact_size)

    device_type    = pkg.get("deviceType", "")
    operation_type = OPERATION_TYPE_MAP.get(device_type, 0)

    _log("debug", "iot_job_document_built",
         jobId=job_id, packageName=package_name, version=version,
         operationType=operation_type, deviceType=device_type,
         artifactSize=artifact_size,
         sha256=pkg.get("sha256", "")[:16] + "...",
         signatureLength=len(pkg.get("signature", "")))

    job_doc = {
        "operationType": operation_type,
        "packageName":   package_name,
        "version":       version,
        "artifact": {
            "presignedUrl": presigned_url,
            "sha256":       pkg["sha256"],
            "signature":    pkg["signature"],
            "size":         artifact_size,
        },
        "mandatory": True,
        "rollback":  True,
    }

    iot_target = f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"
    _log("debug", "iot_create_job_call",
         jobId=job_id, target=iot_target,
         timeoutMinutes=IOT_JOB_TIMEOUT_MINUTES)

    t_iot = time.monotonic()
    iot_resp = iot.create_job(
        jobId=job_id,
        targets=[iot_target],
        document=json.dumps(job_doc),
        description=f"User-consented update: {package_name} to {version}",
        jobExecutionsRolloutConfig={"maximumPerMinute": 1},
        timeoutConfig={"inProgressTimeoutInMinutes": IOT_JOB_TIMEOUT_MINUTES},
        tags=[
            {"Key": "Project",     "Value": "digilux"},
            {"Key": "Component",   "Value": "ota"},
            {"Key": "PackageName", "Value": package_name},
            {"Key": "Version",     "Value": version},
            {"Key": "InitiatedBy", "Value": "user"},
        ],
    )
    iot_ms = int((time.monotonic() - t_iot) * 1000)

    job_arn = iot_resp["jobArn"]
    _log("info", "iot_job_created",
         jobId=job_id, jobArn=job_arn, thingName=thing_name,
         packageName=package_name, version=version,
         iotMs=iot_ms, urlMs=url_ms,
         artifactSize=artifact_size, deviceType=device_type)
    _audit("IOT_JOB_CREATED", user_id,
           {"jobId": job_id, "packageName": package_name, "version": version},
           "SUCCESS",
           jobArn=job_arn, thingName=thing_name, deviceId=device_id,
           artifactSize=artifact_size, expirySeconds=expiry_sec,
           iotMs=iot_ms)
    return job_arn


# ──────────────────────────────────────────────────────────────────────────────
# Consent handler
# ──────────────────────────────────────────────────────────────────────────────

def _find_pending_consent(user_id: str, device_id: str,
                           package_name: str, version: str) -> dict | None:
    """Find a PENDING admin-initiated consent record for this user/device/package/version."""
    _log("debug", "pending_consent_lookup",
         userId=user_id, deviceId=device_id,
         packageName=package_name, version=version)
    resp = dynamo.Table(CONSENTS_TABLE).query(
        IndexName=CONSENTS_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id) & Key("deviceId").eq(device_id),
        FilterExpression=(
            Attr("status").eq("PENDING") &
            Attr("packageName").eq(package_name) &
            Attr("version").eq(version)
        ),
    )
    items = resp.get("Items", [])
    if items:
        _log("debug", "pending_consent_found",
             userId=user_id, deviceId=device_id,
             consentId=items[0]["consentId"],
             deploymentId=items[0].get("deploymentId"))
    else:
        _log("debug", "pending_consent_not_found",
             userId=user_id, deviceId=device_id,
             packageName=package_name, version=version,
             detail="No admin-initiated PENDING consent — will try user-initiated path")
    return items[0] if items else None


def _handle_consent(user_id: str, email: str, body: dict) -> dict:
    device_id    = body.get("deviceId", "").strip()
    package_name = body.get("packageName", "").strip()
    version      = body.get("version", "").strip()
    accepted     = body.get("accepted", True)

    _log("info", "handle_consent_start",
         userId=user_id, deviceId=device_id,
         packageName=package_name, version=version,
         accepted=accepted)

    for field, val in [("deviceId", device_id), ("packageName", package_name), ("version", version)]:
        if not val:
            _log("warning", "missing_required_field",
                 field=field, userId=user_id)
            return _resp(400, {"error": f"Missing required field: {field}"})
    if not _UUID_RE.match(device_id):
        _log("warning", "invalid_device_id_format",
             deviceId=device_id, userId=user_id)
        return _resp(400, {"error": "Invalid deviceId format"})
    if len(package_name) > 64:
        _log("warning", "invalid_package_name",
             packageName=package_name[:20], userId=user_id)
        return _resp(400, {"error": "Invalid packageName"})
    if not _VERSION_RE.match(version):
        _log("warning", "invalid_version_format",
             version=version, userId=user_id)
        return _resp(400, {"error": "Invalid version format"})
    if not isinstance(accepted, bool):
        _log("warning", "invalid_accepted_field",
             accepted=accepted, acceptedType=type(accepted).__name__, userId=user_id)
        return _resp(400, {"error": "Field 'accepted' must be a boolean"})

    # Device ownership check
    dev = _get_device_item(device_id)
    if not dev or dev.get("userId") != user_id:
        _log("warning", "device_ownership_check_failed",
             userId=user_id, deviceId=device_id,
             deviceOwner=dev.get("userId") if dev else None,
             deviceExists=dev is not None,
             detail="Device not found or belongs to a different user")
        _audit("CONSENT_REJECTED", user_id,
               {"deviceId": device_id, "packageName": package_name, "version": version},
               "FAILURE", reason="device_not_owned_by_user",
               deviceExists=dev is not None)
        return _resp(404, {"error": "Device not found"})

    _log("debug", "device_ownership_verified",
         userId=user_id, deviceId=device_id,
         thingName=dev.get("thingName"),
         hasPendingJob=bool(dev.get("pendingJobId")))

    now_ms      = int(time.time() * 1000)
    mac_address = dev.get("macAddress", "")

    # Look for an admin-initiated PENDING consent record
    consent = _find_pending_consent(user_id, device_id, package_name, version)

    if consent:
        consent_id    = consent["consentId"]
        deployment_id = consent.get("deploymentId", "")
        _log("info", "admin_initiated_consent_found",
             userId=user_id, deviceId=device_id,
             consentId=consent_id, deploymentId=deployment_id,
             packageName=package_name, version=version,
             accepted=accepted)

        if not accepted:
            # ── DECLINE ───────────────────────────────────────────────────────
            _log("info", "consent_decline_start",
                 userId=user_id, deviceId=device_id,
                 consentId=consent_id, deploymentId=deployment_id)
            dynamo.Table(CONSENTS_TABLE).update_item(
                Key={"consentId": consent_id},
                UpdateExpression="SET #s = :s, declinedAt = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "DECLINED", ":ts": now_ms},
            )
            _audit("CONSENT_DECLINED", user_id,
                   {"consentId": consent_id, "deviceId": device_id,
                    "packageName": package_name, "version": version},
                   "SUCCESS", deploymentId=deployment_id)
            _log("info", "consent_declined",
                 userId=user_id, deviceId=device_id,
                 consentId=consent_id, deploymentId=deployment_id,
                 detail="Device will NOT receive firmware update")
            user_email = _get_user_email(user_id) or email
            _send_decline_email(user_email, package_name, version, device_id,
                                reason="User declined the update")
            return _resp(200, {
                "status":  "DECLINED",
                "message": "Update declined. No firmware changes will be made to your device.",
            })

        # ── ACCEPT — admin-initiated path ─────────────────────────────────────
        thing_name = dev.get("thingName")
        if not thing_name:
            _log("warning", "accept_blocked_no_thing_name",
                 userId=user_id, deviceId=device_id, consentId=consent_id,
                 detail="OTA agent not yet registered — no thingName")
            return _resp(409, {"error": "Device OTA agent has not started yet."})

        pkg = _get_package(package_name, version)
        if not pkg or pkg.get("status") != "ACTIVE":
            _log("warning", "accept_blocked_package_unavailable",
                 userId=user_id, deviceId=device_id, consentId=consent_id,
                 packageName=package_name, version=version,
                 packageStatus=pkg.get("status") if pkg else "NOT_FOUND")
            return _resp(400, {"error": f"Package {package_name}@{version} is no longer available."})

        if dev.get("pendingJobId"):
            _log("warning", "accept_blocked_pending_job",
                 userId=user_id, deviceId=device_id, consentId=consent_id,
                 existingJobId=dev["pendingJobId"],
                 detail="Another update is already in progress on this device")
            return _resp(409, {
                "error": "An update is already in progress on this device.",
                "pendingJobId": dev["pendingJobId"],
            })

        artifact_size = pkg.get("artifactSize", 0)
        if isinstance(artifact_size, Decimal):
            artifact_size = int(artifact_size)
        expiry_sec = _presign_expiry(artifact_size)
        job_id     = f"digilux-ota-{package_name}-{version}-{int(time.time())}".replace(".", "-")

        _log("info", "consent_accept_creating_iot_job",
             userId=user_id, deviceId=device_id, consentId=consent_id,
             deploymentId=deployment_id, jobId=job_id,
             packageName=package_name, version=version,
             thingName=thing_name, expirySeconds=expiry_sec,
             artifactSizeBytes=artifact_size)

        t_job = time.monotonic()
        iot_job_arn = _create_iot_job(pkg, package_name, version,
                                       thing_name, device_id, user_id, job_id, expiry_sec)
        job_ms = int((time.monotonic() - t_job) * 1000)

        # ── Write results to DynamoDB ─────────────────────────────────────────
        _log("debug", "consent_accept_writing_dynamo",
             consentId=consent_id, jobId=job_id, deploymentId=deployment_id)
        dynamo.Table(CONSENTS_TABLE).update_item(
            Key={"consentId": consent_id},
            UpdateExpression="SET #s = :s, acceptedAt = :ts, jobId = :jid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "ACCEPTED", ":ts": now_ms, ":jid": job_id},
        )
        dynamo.Table(OTA_JOBS_TABLE).put_item(Item={
            "jobId":          job_id,
            "iotJobArn":      iot_job_arn,
            "packageName":    package_name,
            "version":        version,
            "deviceType":     pkg.get("deviceType", ""),
            "targetType":     "THING",
            "targetId":       device_id,
            "rolloutStage":   "USER_CONSENTED",
            "status":         "QUEUED",
            "createdAt":      now_ms,
            "createdBy":      f"user:{user_id}",
            "initiatedBy":    "USER",
            "consentId":      consent_id,
            "deploymentId":   deployment_id,
            "deviceStatuses": {},
        })
        dynamo.Table(DEVICE_DATA_TABLE).update_item(
            Key={"deviceId": device_id, "macAddress": mac_address},
            UpdateExpression="SET pendingJobId = :jid, lastUpdatedAt = :ts",
            ExpressionAttributeValues={":jid": job_id, ":ts": now_ms},
        )

        _audit("CONSENT_ACCEPTED", user_id,
               {"consentId": consent_id, "deviceId": device_id,
                "packageName": package_name, "version": version},
               "SUCCESS",
               jobId=job_id, iotJobArn=iot_job_arn,
               deploymentId=deployment_id, thingName=thing_name,
               artifactSizeBytes=artifact_size, expirySeconds=expiry_sec,
               jobCreateMs=job_ms)
        _log("info", "consent_accepted_job_created",
             userId=user_id, deviceId=device_id,
             consentId=consent_id, deploymentId=deployment_id,
             jobId=job_id, iotJobArn=iot_job_arn,
             packageName=package_name, version=version,
             jobCreateMs=job_ms)
        return _resp(202, {
            "jobId":       job_id,
            "deviceId":    device_id,
            "packageName": package_name,
            "version":     version,
            "status":      "QUEUED",
            "message": (
                "Update accepted. Your device will download and install the update shortly. "
                "Use the status endpoint to track progress."
            ),
        })

    # ── No admin-initiated consent found — user-initiated path ────────────────
    _log("info", "user_initiated_consent_path",
         userId=user_id, deviceId=device_id,
         packageName=package_name, version=version, accepted=accepted)

    if not accepted:
        _log("info", "user_initiated_decline_no_pending",
             userId=user_id, deviceId=device_id,
             packageName=package_name, version=version)
        return _resp(404, {"error": "No pending update request found for this device."})

    pkg = _get_package(package_name, version)
    if not pkg:
        _log("warning", "user_initiated_package_not_found",
             userId=user_id, packageName=package_name, version=version)
        return _resp(404, {"error": f"Package {package_name}@{version} not found"})
    if pkg.get("status") != "ACTIVE":
        _log("warning", "user_initiated_package_not_active",
             userId=user_id, packageName=package_name, version=version,
             packageStatus=pkg.get("status"))
        return _resp(400, {"error": f"Package {package_name}@{version} is not available"})

    thing_name = dev.get("thingName")
    if not thing_name:
        _log("warning", "user_initiated_no_thing_name",
             userId=user_id, deviceId=device_id,
             detail="OTA agent not yet started — no thingName in device_data")
        return _resp(409, {
            "error": "Device OTA agent has not started yet. "
                     "Please ensure the device is online and the OTA agent is running."
        })

    installed_ver = dev.get("globalInstalledVersion") or ""
    if installed_ver and not _is_newer(version, installed_ver):
        if installed_ver == version:
            _log("info", "user_initiated_already_installed",
                 userId=user_id, deviceId=device_id,
                 packageName=package_name, version=version,
                 installedVersion=installed_ver)
            return _resp(409, {
                "error": f"{package_name} version {version} is already installed on this device."
            })
        _log("info", "user_initiated_version_not_newer",
             userId=user_id, deviceId=device_id,
             packageName=package_name,
             requestedVersion=version, installedVersion=installed_ver)
        return _resp(409, {
            "error": f"Requested version {version} is not newer than installed version {installed_ver}."
        })

    if dev.get("pendingJobId"):
        _log("warning", "user_initiated_blocked_pending_job",
             userId=user_id, deviceId=device_id,
             existingJobId=dev["pendingJobId"])
        return _resp(409, {
            "error": "An update is already in progress on this device.",
            "pendingJobId": dev["pendingJobId"],
        })

    consent_id    = str(uuid.uuid4())
    job_id        = f"digilux-ota-{package_name}-{version}-{int(time.time())}".replace(".", "-")
    artifact_size = pkg.get("artifactSize", 0)
    if isinstance(artifact_size, Decimal):
        artifact_size = int(artifact_size)
    expiry_sec = _presign_expiry(artifact_size)

    _log("info", "user_initiated_creating_iot_job",
         userId=user_id, deviceId=device_id, consentId=consent_id,
         jobId=job_id, packageName=package_name, version=version,
         thingName=thing_name, expirySeconds=expiry_sec,
         installedVersion=installed_ver, artifactSizeBytes=artifact_size)

    t_job = time.monotonic()
    iot_job_arn = _create_iot_job(pkg, package_name, version,
                                   thing_name, device_id, user_id, job_id, expiry_sec)
    job_ms = int((time.monotonic() - t_job) * 1000)

    # ── Write results to DynamoDB ─────────────────────────────────────────────
    _log("debug", "user_initiated_writing_dynamo",
         consentId=consent_id, jobId=job_id)
    dynamo.Table(CONSENTS_TABLE).put_item(Item={
        "consentId":   consent_id,
        "userId":      user_id,
        "deviceId":    device_id,
        "packageName": package_name,
        "version":     version,
        "jobId":       job_id,
        "status":      "ACCEPTED",
        "consentedAt": now_ms,
    })
    dynamo.Table(OTA_JOBS_TABLE).put_item(Item={
        "jobId":          job_id,
        "iotJobArn":      iot_job_arn,
        "packageName":    package_name,
        "version":        version,
        "deviceType":     pkg.get("deviceType", ""),
        "targetType":     "THING",
        "targetId":       device_id,
        "rolloutStage":   "USER_INITIATED",
        "status":         "QUEUED",
        "createdAt":      now_ms,
        "createdBy":      f"user:{user_id}",
        "initiatedBy":    "USER",
        "consentId":      consent_id,
        "deviceStatuses": {},
    })
    dynamo.Table(DEVICE_DATA_TABLE).update_item(
        Key={"deviceId": device_id, "macAddress": mac_address},
        UpdateExpression="SET pendingJobId = :jid, lastUpdatedAt = :ts",
        ExpressionAttributeValues={":jid": job_id, ":ts": now_ms},
    )

    _audit("USER_CONSENT_ACCEPTED", user_id,
           {"deviceId": device_id, "packageName": package_name, "version": version},
           "SUCCESS",
           consentId=consent_id, jobId=job_id, iotJobArn=iot_job_arn,
           thingName=thing_name, artifactSizeBytes=artifact_size,
           expirySeconds=expiry_sec, jobCreateMs=job_ms)
    _log("info", "user_initiated_job_created",
         userId=user_id, deviceId=device_id,
         consentId=consent_id, jobId=job_id, iotJobArn=iot_job_arn,
         packageName=package_name, version=version, jobCreateMs=job_ms)

    return _resp(202, {
        "jobId":       job_id,
        "deviceId":    device_id,
        "packageName": package_name,
        "version":     version,
        "status":      "QUEUED",
        "message": (
            "Update accepted. Your device will download and install the update shortly. "
            "Use the status endpoint to track progress."
        ),
    })


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    handler_start = time.monotonic()
    request_id    = context.aws_request_id if context else None

    try:
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            _log("warning", "missing_sub_claim",
                 requestId=request_id,
                 detail="JWT sub claim absent — rejecting with 401")
            return _resp(401, {"error": "Unauthorized — invalid token"})

        email = claims.get("email", user_id)
        _log("info", "consent_request",
             userId=user_id, email=email[:3] + "***",
             requestId=request_id)

        raw_body = event.get("body") or ""
        if len(raw_body) > _MAX_BODY_BYTES:
            _log("warning", "request_body_too_large",
                 bodyLength=len(raw_body), maxBytes=_MAX_BODY_BYTES,
                 userId=user_id)
            return _resp(400, {"error": "Request body too large"})

        body = json.loads(raw_body or "{}")

        result = _handle_consent(user_id, email, body)

        handler_ms = int((time.monotonic() - handler_start) * 1000)
        _log("info", "consent_handler_complete",
             userId=user_id, statusCode=result.get("statusCode"),
             handlerMs=handler_ms)
        return result

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        _log("error", "aws_client_error",
             awsError=code, awsMessage=msg)
        _audit("CONSENT_AWS_ERROR", ACTOR, {}, "FAILURE",
               awsError=code, awsMessage=msg)
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in user_consent handler: {e}")
        _audit("CONSENT_UNHANDLED_ERROR", ACTOR, {}, "FAILURE",
               error=str(e), excType=type(e).__name__)
        return _resp(500, {"error": "Internal server error"})
