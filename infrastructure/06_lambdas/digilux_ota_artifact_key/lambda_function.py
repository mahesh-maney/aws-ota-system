"""
digilux_ota_artifact_key
Just-in-time AES decryption key delivery for OTA artifacts.

POST /api/v1/ota/device/artifact-key
Auth: AWS_IAM (IoT Credentials Provider SigV4 — device X.509 cert → IAM role)

Request body:
  { "jobId": "digilux-ota-...", "thingName": "DGW-abc123" }

4-point verification before releasing the key:
  1. thingName matches the caller's IAM identity (from SigV4 context)
  2. jobId exists in digilux_ota_jobs and targets thingName
  3. Job status is QUEUED or IN_PROGRESS (not COMPLETED, CANCELLED, FAILED)
  4. Rate limit: max 5 requests per jobId+thingName within 24 hours

On success:
  - Calls KMS Decrypt on the encryptedDataKey stored in digilux_ota_packages
  - Returns plaintext AES key (base64) + IV
  - Writes rate-limit record to digilux_ota_key_requests
  - Never stores plaintext key anywhere

Security guarantees:
  - Plaintext key only exists in Lambda memory + HTTPS response
  - encryptedDataKey in DynamoDB is useless without KMS access
  - Only authenticated IoT Things (valid X.509 cert) can call this endpoint
  - thingName verification prevents one device accessing another device's key
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION            = os.environ["REGION"]
PACKAGES_TABLE    = os.environ.get("PACKAGES_TABLE",    "digilux_ota_packages")
OTA_JOBS_TABLE    = os.environ.get("OTA_JOBS_TABLE",    "digilux_ota_jobs")
KEY_REQUESTS_TABLE = os.environ.get("KEY_REQUESTS_TABLE", "digilux_ota_key_requests")
KMS_KEY_ID        = os.environ.get("KMS_KEY_ID",        "alias/digilux-ota-data-key")
MAX_REQUESTS_PER_JOB = int(os.environ.get("MAX_REQUESTS_PER_JOB", "5"))
RATE_WINDOW_HOURS    = int(os.environ.get("RATE_WINDOW_HOURS",    "24"))

ACTOR = "artifact_key"

dynamo = boto3.resource("dynamodb", region_name=REGION)
kms    = boto3.client("kms",        region_name=REGION)

_ACTIVE_JOB_STATUSES = {"QUEUED", "IN_PROGRESS"}

_JOB_ID_RE    = re.compile(r"^[a-zA-Z0-9.\-_]{1,128}$")
_THING_RE     = re.compile(r"^[a-zA-Z0-9.\-_:]{1,128}$")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _log(level: str, msg: str, **fields) -> None:
    getattr(log, level)(json.dumps({"msg": msg, **fields}, default=str))


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


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _extract_thing_name_from_iam(event: dict) -> str | None:
    """
    Extract the IoT Thing name from the SigV4 caller identity.
    When a device calls via IoT Credentials Provider, the principal ARN is:
      arn:aws:iot:REGION:ACCOUNT:thing/THING_NAME
    The IAM assumed-role ARN is:
      arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/THING_NAME
    API Gateway puts this in requestContext.identity.userArn.
    """
    ctx = event.get("requestContext", {})
    identity = ctx.get("identity", {})

    # userArn for IoT: arn:aws:sts::986906626244:assumed-role/digilux-iot-device-role/<thingName>
    user_arn = identity.get("userArn", "")
    if "assumed-role" in user_arn:
        # Last segment after the final slash is the session name = thingName
        parts = user_arn.rsplit("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return None


def _check_rate_limit(job_id: str, thing_name: str) -> tuple[bool, int]:
    """
    Returns (is_limited, current_count).
    Reads the rate-limit record from digilux_ota_key_requests.
    """
    pk = f"{job_id}#{thing_name}"
    try:
        resp = dynamo.Table(KEY_REQUESTS_TABLE).get_item(Key={"pk": pk})
        item = resp.get("Item")
        if not item:
            return False, 0
        count = int(item.get("requestCount", 0))
        return count >= MAX_REQUESTS_PER_JOB, count
    except Exception as e:
        _log("warning", "rate_limit_check_failed", pk=pk, error=str(e))
        # Fail open — don't block legitimate devices due to rate limit table issues
        return False, 0


def _increment_rate_limit(job_id: str, thing_name: str) -> None:
    """Atomically increment the request count. TTL = now + RATE_WINDOW_HOURS."""
    pk    = f"{job_id}#{thing_name}"
    ttl   = int(time.time()) + RATE_WINDOW_HOURS * 3600
    try:
        dynamo.Table(KEY_REQUESTS_TABLE).update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "SET requestCount = if_not_exists(requestCount, :zero) + :one, "
                "#ttl = :ttl, jobId = :jid, thingName = :tn, lastRequestAt = :ts"
            ),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one":  1,
                ":ttl":  ttl,
                ":jid":  job_id,
                ":tn":   thing_name,
                ":ts":   int(time.time() * 1000),
            },
        )
    except Exception as e:
        _log("warning", "rate_limit_increment_failed", pk=pk, error=str(e))


def _get_job(job_id: str) -> dict | None:
    try:
        resp = dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id})
        return resp.get("Item")
    except Exception as e:
        _log("error", "job_lookup_failed", jobId=job_id, error=str(e))
        return None


def _get_package(package_name: str, version: str) -> dict | None:
    try:
        resp = dynamo.Table(PACKAGES_TABLE).get_item(
            Key={"packageName": package_name, "version": version}
        )
        return resp.get("Item")
    except Exception as e:
        _log("error", "package_lookup_failed",
             packageName=package_name, version=version, error=str(e))
        return None


def _kms_decrypt(encrypted_blob_b64: str) -> bytes:
    """Decrypt a KMS-encrypted blob. Returns plaintext bytes."""
    ciphertext = base64.b64decode(encrypted_blob_b64)
    resp = kms.decrypt(
        KeyId=KMS_KEY_ID,
        CiphertextBlob=ciphertext,
    )
    return resp["Plaintext"]


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    request_id = context.aws_request_id if context else None
    t_start    = time.monotonic()

    try:
        # ── 1. Extract caller thingName from IAM SigV4 identity ──────────────
        caller_thing = _extract_thing_name_from_iam(event)
        if not caller_thing:
            _log("warning", "iam_thing_name_missing",
                 requestId=request_id,
                 detail="Could not determine Thing name from IAM identity")
            return _resp(403, {"error": "Forbidden — cannot determine device identity"})

        _log("info", "artifact_key_request",
             callerThing=caller_thing, requestId=request_id)

        # ── 2. Parse and validate request body ───────────────────────────────
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return _resp(400, {"error": "Invalid JSON body"})

        job_id     = (body.get("jobId")     or "").strip()
        thing_name = (body.get("thingName") or "").strip()

        if not job_id or not thing_name:
            return _resp(400, {"error": "Missing required fields: jobId, thingName"})

        if not _JOB_ID_RE.match(job_id):
            return _resp(400, {"error": "Invalid jobId format"})

        if not _THING_RE.match(thing_name):
            return _resp(400, {"error": "Invalid thingName format"})

        # ── 3. Verify: caller thingName == requested thingName ────────────────
        # Prevents device A from requesting a key for device B's job
        if caller_thing != thing_name:
            _log("warning", "thing_name_mismatch",
                 callerThing=caller_thing, requestedThing=thing_name,
                 jobId=job_id,
                 detail="Device tried to request key for a different device's job")
            _audit("KEY_REQUEST_FORBIDDEN_THING_MISMATCH", caller_thing,
                   {"jobId": job_id, "requestedThing": thing_name},
                   "FAILURE", reason="thing_name_mismatch")
            return _resp(403, {"error": "Forbidden — thingName does not match caller identity"})

        # ── 4. Rate limit check ───────────────────────────────────────────────
        is_limited, req_count = _check_rate_limit(job_id, thing_name)
        if is_limited:
            _log("warning", "rate_limit_exceeded",
                 thingName=thing_name, jobId=job_id,
                 requestCount=req_count, maxRequests=MAX_REQUESTS_PER_JOB)
            _audit("KEY_REQUEST_RATE_LIMITED", thing_name,
                   {"jobId": job_id},
                   "FAILURE", requestCount=req_count, maxRequests=MAX_REQUESTS_PER_JOB)
            return _resp(429, {
                "error": f"Rate limit exceeded — max {MAX_REQUESTS_PER_JOB} key requests per job"
            })

        # ── 5. Verify: jobId exists and targets this thing ────────────────────
        job = _get_job(job_id)
        if not job:
            _log("warning", "job_not_found", jobId=job_id, thingName=thing_name)
            _audit("KEY_REQUEST_JOB_NOT_FOUND", thing_name,
                   {"jobId": job_id}, "FAILURE")
            return _resp(404, {"error": "Job not found"})

        # job.targetId is deviceId for THING jobs — check via thingName
        job_target_id = job.get("targetId", "")   # deviceId
        # targetType verification: must be THING
        if job.get("targetType") != "THING":
            _log("warning", "job_target_type_not_thing",
                 jobId=job_id, targetType=job.get("targetType"))
            return _resp(403, {"error": "Forbidden — job is not targeted at a specific device"})

        # Cross-check: verify thingName in job metadata if stored, else use IoT
        # The job record has thingName embedded from job_create (initiatedBy=USER or ADMIN)
        # We look it up from the job's targetId → device_data to get thingName
        job_thing_name = job.get("thingName", "")
        if not job_thing_name:
            # Fall back: look up the device to get its thingName
            from boto3.dynamodb.conditions import Key as DKey
            try:
                dev_resp = dynamo.Table(
                    os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data")
                ).query(
                    KeyConditionExpression=DKey("deviceId").eq(job_target_id)
                )
                dev_items = dev_resp.get("Items", [])
                job_thing_name = dev_items[0].get("thingName", "") if dev_items else ""
            except Exception as e:
                _log("warning", "device_lookup_for_thing_failed",
                     deviceId=job_target_id, error=str(e))

        if job_thing_name and job_thing_name != thing_name:
            _log("warning", "job_thing_name_mismatch",
                 callerThing=thing_name, jobTargetThing=job_thing_name,
                 jobId=job_id,
                 detail="Job is not targeted at the requesting thing")
            _audit("KEY_REQUEST_FORBIDDEN_JOB_MISMATCH", thing_name,
                   {"jobId": job_id, "jobTargetThing": job_thing_name},
                   "FAILURE", reason="job_not_targeted_at_thing")
            return _resp(403, {"error": "Forbidden — job is not targeted at this device"})

        # ── 6. Verify: job status is active (QUEUED or IN_PROGRESS) ──────────
        job_status = job.get("status", "")
        if job_status not in _ACTIVE_JOB_STATUSES:
            _log("warning", "job_not_active",
                 jobId=job_id, thingName=thing_name, jobStatus=job_status)
            _audit("KEY_REQUEST_JOB_NOT_ACTIVE", thing_name,
                   {"jobId": job_id, "jobStatus": job_status}, "FAILURE")
            return _resp(409, {
                "error": f"Job is not active (status={job_status}). "
                          "Key can only be retrieved for QUEUED or IN_PROGRESS jobs."
            })

        # ── 7. Fetch package record to get encryptedDataKey ───────────────────
        package_name = job.get("packageName", "")
        version      = job.get("version", "")
        if not package_name or not version:
            _log("error", "job_missing_package_info",
                 jobId=job_id, packageName=package_name, version=version)
            return _resp(500, {"error": "Internal error — job record missing package info"})

        pkg = _get_package(package_name, version)
        if not pkg:
            _log("error", "package_not_found_for_job",
                 jobId=job_id, packageName=package_name, version=version)
            return _resp(404, {"error": "Package not found"})

        encrypted_data_key = pkg.get("encryptedDataKey", "")
        aes_iv_b64         = pkg.get("aesIv", "")

        if not encrypted_data_key:
            # Legacy package: may have wrappedDataKey (old key-server format) — not supported
            if pkg.get("wrappedDataKey"):
                _log("error", "legacy_wrapped_key_not_supported",
                     jobId=job_id, packageName=package_name, version=version,
                     detail="Package uses legacy wrappedDataKey format — re-upload required")
                return _resp(410, {
                    "error": "This package was uploaded with an older key format. "
                             "Please ask your administrator to re-upload the package."
                })
            _log("error", "no_encrypted_data_key",
                 jobId=job_id, packageName=package_name, version=version)
            return _resp(500, {"error": "Internal error — package has no encryption key"})

        # ── 8. Decrypt via KMS ────────────────────────────────────────────────
        _log("info", "kms_decrypt_start",
             jobId=job_id, thingName=thing_name,
             packageName=package_name, version=version,
             kmsKeyId=KMS_KEY_ID)
        t_kms = time.monotonic()
        try:
            plaintext_key_bytes = _kms_decrypt(encrypted_data_key)
        except ClientError as exc:
            err_code = exc.response["Error"]["Code"]
            _log("error", "kms_decrypt_failed",
                 jobId=job_id, thingName=thing_name,
                 awsError=err_code,
                 awsMessage=exc.response["Error"]["Message"])
            _audit("KEY_REQUEST_KMS_FAILED", thing_name,
                   {"jobId": job_id, "packageName": package_name, "version": version},
                   "FAILURE", kmsError=err_code)
            return _resp(500, {"error": "Key decryption failed — please try again"})
        kms_ms = int((time.monotonic() - t_kms) * 1000)

        plaintext_key_b64 = base64.b64encode(plaintext_key_bytes).decode()

        # ── 9. Increment rate limit counter ───────────────────────────────────
        _increment_rate_limit(job_id, thing_name)

        total_ms = int((time.monotonic() - t_start) * 1000)
        _log("info", "artifact_key_delivered",
             jobId=job_id, thingName=thing_name,
             packageName=package_name, version=version,
             kmsMs=kms_ms, totalMs=total_ms,
             requestCount=req_count + 1)
        _audit("ARTIFACT_KEY_DELIVERED", thing_name,
               {"jobId": job_id, "packageName": package_name, "version": version},
               "SUCCESS",
               kmsMs=kms_ms, totalMs=total_ms, requestCount=req_count + 1)

        return _resp(200, {
            "jobId":       job_id,
            "packageName": package_name,
            "version":     version,
            "dataKey":     plaintext_key_b64,
            "iv":          aes_iv_b64,
        })

    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in artifact_key handler: {e}")
        return _resp(500, {"error": "Internal server error"})
