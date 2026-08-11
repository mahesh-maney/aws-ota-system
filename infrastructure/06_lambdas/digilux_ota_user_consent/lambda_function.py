"""
digilux_ota_user_consent
Records end-user consent and triggers a device OTA update.

POST /api/v1/ota/my/updates/consent

Auth: Cognito ID token (any authenticated user).
The device must be owned by the calling user.
The admin must have already uploaded and activated the target package version.

Body:
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0"
}

Security checks (in order):
  1. Valid Cognito JWT with sub claim present
  2. deviceId format validation (non-empty UUID string)
  3. Device ownership — deviceId must exist in digilux_device_data under this userId
  4. Package packageName@version must exist and be ACTIVE
  5. Device must be registered in OTA inventory
  6. Requested version must be newer than currently installed
  7. No other update already in-progress (pendingJobId must be null)
  8. Rate limit — max 1 consent per device per RATE_LIMIT_MINUTES (default 5 min)
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
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION              = os.environ["REGION"]
ACCOUNT_ID          = os.environ["ACCOUNT_ID"]
DEVICE_DATA_TABLE   = os.environ.get("DEVICE_DATA_TABLE",   "digilux_device_data")
PACKAGES_TABLE      = os.environ.get("PACKAGES_TABLE",       "digilux_ota_packages")
OTA_JOBS_TABLE      = os.environ.get("OTA_JOBS_TABLE",       "digilux_ota_jobs")
CONSENTS_TABLE      = os.environ.get("CONSENTS_TABLE",       "digilux_ota_user_consents")
ARTIFACT_BUCKET    = os.environ.get("ARTIFACT_BUCKET",    "digilux-ota-artifacts")
RATE_LIMIT_MINUTES = int(os.environ.get("RATE_LIMIT_MINUTES", "5"))

# Pre-signed URL expiry tiers — configured via ota.env / Lambda env vars
_TIER1_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER1_MAX_MB", "50"))
_TIER1_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER1_SEC",    "3600"))
_TIER2_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER2_MAX_MB", "200"))
_TIER2_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER2_SEC",    "21600"))
_TIER3_MAX_MB  = int(os.environ.get("PRESIGN_EXPIRY_TIER3_MAX_MB", "500"))
_TIER3_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER3_SEC",    "86400"))
_TIER4_SEC     = int(os.environ.get("PRESIGN_EXPIRY_TIER4_SEC",    "172800"))

# IoT Job in-progress timeout — configurable via ota.config / Lambda env vars
IOT_JOB_TIMEOUT_MINUTES = int(os.environ.get("IOT_JOB_TIMEOUT_MINUTES", "1440"))  # 24hr default

# CloudFront signed URL config
CLOUDFRONT_DOMAIN             = os.environ.get("CLOUDFRONT_DOMAIN", "")
CLOUDFRONT_KEY_PAIR_ID        = os.environ.get("CLOUDFRONT_KEY_PAIR_ID", "")
CLOUDFRONT_PRIVATE_KEY_SECRET = os.environ.get("CLOUDFRONT_PRIVATE_KEY_SECRET", "digilux-ota-cloudfront-key")

# Cache private key across Lambda invocations (avoid Secrets Manager call every request)
_cf_private_key_cache = None

CONSENTS_USER_INDEX = os.environ.get("CONSENTS_USER_INDEX", "userId-deviceId-index")

# Body size guard
_MAX_BODY_BYTES = 2048
# Loose UUID format check (36-char hex with dashes)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Version must be non-empty printable string, max 32 chars
_VERSION_RE = re.compile(r"^[a-zA-Z0-9.\-_]{1,32}$")

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot",        region_name=REGION)
s3     = boto3.client("s3",         region_name=REGION)


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
    """Load and cache CloudFront RSA private key from Secrets Manager."""
    global _cf_private_key_cache
    if _cf_private_key_cache:
        return _cf_private_key_cache
    sm = boto3.client("secretsmanager", region_name=REGION)
    pem = sm.get_secret_value(SecretId=CLOUDFRONT_PRIVATE_KEY_SECRET)["SecretString"]
    _cf_private_key_cache = serialization.load_pem_private_key(pem.encode(), password=None)
    return _cf_private_key_cache


def _cf_b64(data: bytes) -> str:
    """CloudFront-safe base64: replace +/= with -/~/_ per CloudFront spec."""
    return base64.b64encode(data).decode().replace("+", "-").replace("=", "_").replace("/", "~")


def _cloudfront_signed_url(s3_key: str, expiry_sec: int) -> str:
    """
    Generate a CloudFront signed URL for the given S3 key.
    Falls back to S3 pre-signed URL if CloudFront is not configured.
    """
    if not CLOUDFRONT_DOMAIN or not CLOUDFRONT_KEY_PAIR_ID:
        log.warning("CloudFront not configured — falling back to S3 pre-signed URL")
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

    signed = (
        f"{resource_url}"
        f"?Policy={_cf_b64(policy.encode())}"
        f"&Signature={_cf_b64(signature)}"
        f"&Key-Pair-Id={CLOUDFRONT_KEY_PAIR_ID}"
    )
    return signed


def _get_artifact_url(pkg: dict, expiry_sec: int) -> str:
    """Return CloudFront signed URL if configured, else S3 pre-signed URL."""
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
        "audit": True,
        "event": event,
        "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor": actor,
        "resource": resource,
        "result": result,
        **extra,
    }))


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_device_item(device_id: str) -> dict | None:
    """
    Query digilux_device_data by deviceId (hash key). Returns the first item or None.
    The item carries userId (for ownership check), macAddress (for UpdateItem),
    and OTA fields (installedVersions, pendingJobId, thingName).
    """
    tbl  = dynamo.Table(DEVICE_DATA_TABLE)
    resp = tbl.query(KeyConditionExpression=Key("deviceId").eq(device_id))
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_package(package_name: str, version: str) -> dict | None:
    tbl = dynamo.Table(PACKAGES_TABLE)
    return tbl.get_item(Key={"packageName": package_name, "version": version}).get("Item")


def _check_rate_limit(user_id: str, device_id: str) -> bool:
    """
    Return True if a consent for this user+device was recorded within
    the last RATE_LIMIT_MINUTES. Uses the consents table GSI.
    """
    cutoff_ms = int((time.time() - RATE_LIMIT_MINUTES * 60) * 1000)
    tbl = dynamo.Table(CONSENTS_TABLE)
    resp = tbl.query(
        IndexName=CONSENTS_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id) & Key("deviceId").eq(device_id),
        FilterExpression="#ca > :cutoff",
        ExpressionAttributeNames={"#ca": "consentedAt"},
        ExpressionAttributeValues={":cutoff": cutoff_ms},
        Limit=1,
    )
    return len(resp.get("Items", [])) > 0


def _write_consent(consent_id: str, user_id: str, device_id: str,
                   package_name: str, version: str, job_id: str) -> None:
    now_ms = int(time.time() * 1000)
    ttl    = int(time.time()) + 86400  # 24-hour audit retention
    dynamo.Table(CONSENTS_TABLE).put_item(Item={
        "consentId":   consent_id,
        "userId":      user_id,
        "deviceId":    device_id,
        "packageName": package_name,
        "version":     version,
        "jobId":       job_id,
        "status":      "QUEUED",
        "consentedAt": now_ms,
        "ttl":         ttl,
    })
    log.info(f"Consent record written: consentId={consent_id}, jobId={job_id}")


# ──────────────────────────────────────────────────────────────────────────────
# IoT Job creation (mirrors admin job_create, with user-initiated tag)
# ──────────────────────────────────────────────────────────────────────────────

def _create_iot_job(pkg: dict, package_name: str, version: str,
                    thing_name: str, device_id: str,
                    user_id: str, job_id: str, expiry_sec: int) -> str:
    presigned_url = _get_artifact_url(pkg, expiry_sec)
    log.info(f"Artifact URL generated for {package_name}@{version}, expires in {expiry_sec}s")

    artifact_size = pkg.get("artifactSize", 0)
    if isinstance(artifact_size, Decimal):
        artifact_size = int(artifact_size)

    job_doc = {
        "operation":    pkg["packageType"],
        "packageName":  package_name,
        "version":      version,
        "artifact": {
            "presignedUrl": presigned_url,
            "sha256":       pkg["sha256"],
            "signature":    pkg["signature"],
            "size":         artifact_size,
        },
        "mandatory":    True,
        "rollback":     True,
        "initiatedBy":  "USER",
        "userId":       user_id,
    }

    iot_target = f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"
    iot_resp   = iot.create_job(
        jobId=job_id,
        targets=[iot_target],
        document=json.dumps(job_doc),
        description=f"User-initiated update: {package_name} to {version}",
        # Single device — simple rollout, no abort criteria
        jobExecutionsRolloutConfig={"maximumPerMinute": 1},
        timeoutConfig={"inProgressTimeoutInMinutes": IOT_JOB_TIMEOUT_MINUTES},
        tags=[
            {"Key": "Project",      "Value": "digilux"},
            {"Key": "Component",    "Value": "ota"},
            {"Key": "PackageName",  "Value": package_name},
            {"Key": "Version",      "Value": version},
            {"Key": "InitiatedBy",  "Value": "user"},
        ],
    )
    log.info(f"IoT Job created: arn={iot_resp['jobArn']}")
    return iot_resp["jobArn"]


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
        log.info(json.dumps({"msg": "consent_request", "userId": user_id, "email": email}))

        # ── 2. Body size guard ────────────────────────────────────────────────
        raw_body = event.get("body") or ""
        if len(raw_body) > _MAX_BODY_BYTES:
            log.warning(f"Request body too large: {len(raw_body)} bytes")
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
            log.warning(f"Invalid deviceId format: {device_id}")
            return _resp(400, {"error": "Invalid deviceId format"})

        if not package_name or len(package_name) > 64:
            log.warning(f"Invalid packageName: {package_name!r}")
            return _resp(400, {"error": "Invalid packageName"})

        if not _VERSION_RE.match(version):
            log.warning(f"Invalid version format: {version!r}")
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
            _audit("CONSENT_REJECTED", user_id,
                   {"deviceId": device_id, "packageName": package_name, "version": version},
                   "FAILURE", reason="device_not_owned_by_user")
            # Return 404, not 403 — don't leak that the device exists
            return _resp(404, {"error": "Device not found"})

        mac_address = dev["macAddress"]
        log.info(f"Ownership confirmed: userId={user_id} owns deviceId={device_id}")

        # ── 6. Package lookup — must exist and be ACTIVE ──────────────────────
        pkg = _get_package(package_name, version)
        if not pkg:
            log.warning(f"Package not found: {package_name}@{version}")
            return _resp(404, {"error": f"Package {package_name}@{version} not found"})

        if pkg.get("status") != "ACTIVE":
            log.warning(f"Package {package_name}@{version} status={pkg.get('status')}")
            return _resp(400, {"error": f"Package {package_name}@{version} is not available for installation"})

        # ── 7. OTA agent must have registered (thingName present) ────────────
        thing_name = dev.get("thingName")
        if not thing_name:
            log.warning(f"Device {device_id} has no thingName — OTA agent not yet started")
            return _resp(409, {
                "error": "Device OTA agent has not started yet. "
                         "Please ensure the device is online and the OTA agent is running."
            })

        # ── 8. Version check — must be strictly newer than installed ──────────
        installed_ver = (dev.get("installedVersions") or {}).get(package_name)
        if installed_ver and not _is_newer(version, installed_ver):
            log.info(json.dumps({
                "msg":              "version_not_newer",
                "deviceId":         device_id,
                "packageName":      package_name,
                "installedVersion": installed_ver,
                "requestedVersion": version,
            }))
            if installed_ver == version:
                return _resp(409, {
                    "error": f"{package_name} version {version} is already installed on this device."
                })
            return _resp(409, {
                "error": f"Requested version {version} is not newer than the installed version {installed_ver}."
            })

        # ── 9. No active update already in progress ───────────────────────────
        pending_job_id = dev.get("pendingJobId")
        if pending_job_id:
            log.info(f"Device {device_id} already has a pending job: {pending_job_id}")
            return _resp(409, {
                "error": "An update is already in progress on this device.",
                "pendingJobId": pending_job_id,
            })

        # ── 10. Rate limit — prevent consent spam ─────────────────────────────
        if _check_rate_limit(user_id, device_id):
            log.warning(json.dumps({
                "msg":      "rate_limit_exceeded",
                "userId":   user_id,
                "deviceId": device_id,
            }))
            _audit("CONSENT_RATE_LIMITED", user_id,
                   {"deviceId": device_id, "packageName": package_name, "version": version},
                   "FAILURE", reason="rate_limit")
            return _resp(429, {
                "error": f"Too many update requests. Please wait {RATE_LIMIT_MINUTES} minutes before retrying."
            })

        # ── 11. Create IoT Job ────────────────────────────────────────────────
        consent_id = str(uuid.uuid4())
        job_id     = f"digilux-ota-{package_name}-{version}-{int(time.time())}".replace(".", "-")

        log.info(json.dumps({
            "msg":         "creating_user_ota_job",
            "consentId":   consent_id,
            "jobId":       job_id,
            "userId":      user_id,
            "deviceId":    device_id,
            "packageName": package_name,
            "version":     version,
            "thingName":   thing_name,
        }))

        artifact_size_pre = pkg.get("artifactSize", 0)
        if isinstance(artifact_size_pre, Decimal):
            artifact_size_pre = int(artifact_size_pre)
        expiry_sec = _presign_expiry(artifact_size_pre)
        log.info(f"Presign expiry for {package_name}@{version} ({artifact_size_pre} bytes): {expiry_sec}s")

        iot_job_arn = _create_iot_job(pkg, package_name, version,
                                      thing_name, device_id, user_id, job_id, expiry_sec)

        # ── 12. Persist records ───────────────────────────────────────────────
        now_ms = int(time.time() * 1000)

        # Consent audit record
        _write_consent(consent_id, user_id, device_id, package_name, version, job_id)

        # Jobs table (same schema as admin jobs for status_handler compatibility)
        artifact_size = pkg.get("artifactSize", 0)
        if isinstance(artifact_size, Decimal):
            artifact_size = int(artifact_size)

        dynamo.Table(OTA_JOBS_TABLE).put_item(Item={
            "jobId":        job_id,
            "iotJobArn":    iot_job_arn,
            "packageName":  package_name,
            "version":      version,
            "packageType":  pkg.get("packageType", ""),
            "targetType":   "THING",
            "targetId":     device_id,
            "rolloutStage": "USER_INITIATED",
            "status":       "QUEUED",
            "createdAt":    now_ms,
            "createdBy":    f"user:{user_id}",
            "initiatedBy":  "USER",
            "consentId":    consent_id,
            "deviceStatuses": {},
        })
        log.info(f"Job record written to {OTA_JOBS_TABLE}")

        # Device data — set pendingJobId (composite key: deviceId + macAddress)
        dynamo.Table(DEVICE_DATA_TABLE).update_item(
            Key={"deviceId": device_id, "macAddress": mac_address},
            UpdateExpression="SET pendingJobId = :jid, lastUpdatedAt = :ts",
            ExpressionAttributeValues={":jid": job_id, ":ts": now_ms},
        )
        log.debug(f"Device {device_id} pendingJobId set to {job_id}")

        _audit("USER_CONSENT_ACCEPTED", user_id,
               {"deviceId": device_id, "packageName": package_name, "version": version},
               "SUCCESS",
               consentId=consent_id, jobId=job_id, iotJobArn=iot_job_arn,
               thingName=thing_name, artifactSize=artifact_size)

        log.info(json.dumps({
            "msg":         "user_ota_consent_accepted",
            "consentId":   consent_id,
            "jobId":       job_id,
            "userId":      user_id,
            "deviceId":    device_id,
            "packageName": package_name,
            "version":     version,
        }))

        return _resp(202, {
            "consentId":   consent_id,
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

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        log.error(json.dumps({"msg": "aws_client_error", "code": code, "error": msg}))
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        log.exception(f"Unhandled error in user_consent handler: {e}")
        return _resp(500, {"error": "Internal server error"})
