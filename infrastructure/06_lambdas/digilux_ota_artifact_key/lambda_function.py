"""
digilux_ota_artifact_key
Just-in-time AES decryption key delivery for OTA artifacts.

Supports two invocation paths:

  PATH A — MQTT (primary path for constrained devices, e.g. Nitin's controller)
  ──────────────────────────────────────────────────────────────────────────────
  Triggered by IoT Rule:
    SELECT *, topic(3) AS thingName
    FROM 'digilux/ota/+/key-delivery/request'

  Device publishes:
    Topic:   iot/device/{thingName}/ota/handshake/request
    Payload: { "jobId": "digilux-ota-..." }

  Lambda publishes response to:
    Topic:   iot/device/{thingName}/ota/handshake/response
    Payload: { "jobId": "...", "dataKey": "<base64>", "iv": "<base64>" }
             or { "error": "..." } on failure

  Auth: X.509 device certificate (mTLS) — IoT Core enforces topic-level ACL.
        thingName is proven by the certificate; cannot be spoofed.

  PATH B — HTTP (for non-constrained clients)
  ──────────────────────────────────────────────────────────────────────────────
  POST /api/v1/ota/device/artifact-key
  Auth: AWS_IAM (IoT Credentials Provider SigV4 — device X.509 cert → IAM role)
  Body: { "jobId": "digilux-ota-...", "thingName": "DGW-abc123" }

4-point verification (both paths):
  1. thingName is proven by cert (MQTT) or SigV4 IAM identity (HTTP)
  2. jobId exists in digilux_ota_jobs and targets thingName
  3. Job status is QUEUED or IN_PROGRESS
  4. Rate limit: max 5 requests per jobId+thingName within 24 hours

On success: KMS Decrypt → plaintext AES key (base64) + IV returned.
Plaintext key never stored anywhere — lives only in Lambda memory + response.
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
IOT_ENDPOINT      = os.environ.get("IOT_ENDPOINT",      "")   # e.g. abcdef1234.iot.ap-south-1.amazonaws.com
MAX_REQUESTS_PER_JOB = int(os.environ.get("MAX_REQUESTS_PER_JOB", "5"))
RATE_WINDOW_HOURS    = int(os.environ.get("RATE_WINDOW_HOURS",    "24"))

# MQTT topic pattern — IoT Rule triggers on the request topic
MQTT_RESPONSE_TOPIC = "iot/device/{thingName}/ota/handshake/response"

ACTOR = "artifact_key"

dynamo    = boto3.resource("dynamodb", region_name=REGION)
kms       = boto3.client("kms",        region_name=REGION)
_iot_data = None   # lazy — only initialised when MQTT path is used


def _get_iot_data_client():
    """Return a cached iot-data client. Lazy so HTTP-only invocations pay no init cost."""
    global _iot_data
    if _iot_data is None:
        kwargs = {"region_name": REGION}
        if IOT_ENDPOINT:
            kwargs["endpoint_url"] = f"https://{IOT_ENDPOINT}"
        _iot_data = boto3.client("iot-data", **kwargs)
    return _iot_data

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


def _is_mqtt_invocation(event: dict) -> bool:
    """
    IoT Rule invocations arrive as a flat dict (the message payload + any
    injected fields from the rule SQL). API Gateway invocations always
    have a 'requestContext' key. Use that to distinguish the two.
    """
    return "requestContext" not in event


def _mqtt_publish(thing_name: str, payload: dict) -> None:
    """Publish payload to the device's key-delivery response topic."""
    topic = MQTT_RESPONSE_TOPIC.format(thingName=thing_name)
    _log("debug", "mqtt_publish",
         thingName=thing_name, topic=topic, payloadKeys=list(payload.keys()))
    try:
        _get_iot_data_client().publish(
            topic=topic,
            qos=1,
            payload=json.dumps(payload),
        )
        _log("info", "mqtt_published", thingName=thing_name, topic=topic)
    except Exception as exc:
        _log("error", "mqtt_publish_failed",
             thingName=thing_name, topic=topic, error=str(exc))
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Shared core — used by both HTTP and MQTT paths
# ──────────────────────────────────────────────────────────────────────────────

def _process_key_request(job_id: str, thing_name: str, t_start: float) -> dict:
    """
    Validate the request and return the decrypted key payload.
    Raises ValueError for client errors (bad input / access denied).
    Raises RuntimeError for server errors (KMS failure etc.).
    Returns dict: { jobId, packageName, version, dataKey, iv }
    """
    # ── 1. Rate limit check ───────────────────────────────────────────────────
    is_limited, req_count = _check_rate_limit(job_id, thing_name)
    if is_limited:
        _log("warning", "rate_limit_exceeded",
             thingName=thing_name, jobId=job_id,
             requestCount=req_count, maxRequests=MAX_REQUESTS_PER_JOB)
        _audit("KEY_REQUEST_RATE_LIMITED", thing_name,
               {"jobId": job_id},
               "FAILURE", requestCount=req_count, maxRequests=MAX_REQUESTS_PER_JOB)
        raise ValueError(f"Rate limit exceeded — max {MAX_REQUESTS_PER_JOB} key requests per job")

    # ── 2. Job lookup + targeting check ──────────────────────────────────────
    job = _get_job(job_id)
    if not job:
        _log("warning", "job_not_found", jobId=job_id, thingName=thing_name)
        _audit("KEY_REQUEST_JOB_NOT_FOUND", thing_name, {"jobId": job_id}, "FAILURE")
        raise ValueError("Job not found")

    if job.get("targetType") != "THING":
        _log("warning", "job_target_type_not_thing",
             jobId=job_id, targetType=job.get("targetType"))
        raise ValueError("Forbidden — job is not targeted at a specific device")

    job_target_id  = job.get("targetId", "")
    job_thing_name = job.get("thingName", "")
    if not job_thing_name:
        from boto3.dynamodb.conditions import Key as DKey
        try:
            dev_resp = dynamo.Table(
                os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data")
            ).query(KeyConditionExpression=DKey("deviceId").eq(job_target_id))
            dev_items = dev_resp.get("Items", [])
            job_thing_name = dev_items[0].get("thingName", "") if dev_items else ""
        except Exception as e:
            _log("warning", "device_lookup_for_thing_failed",
                 deviceId=job_target_id, error=str(e))

    if job_thing_name and job_thing_name != thing_name:
        _log("warning", "job_thing_name_mismatch",
             callerThing=thing_name, jobTargetThing=job_thing_name, jobId=job_id)
        _audit("KEY_REQUEST_FORBIDDEN_JOB_MISMATCH", thing_name,
               {"jobId": job_id, "jobTargetThing": job_thing_name},
               "FAILURE", reason="job_not_targeted_at_thing")
        raise ValueError("Forbidden — job is not targeted at this device")

    # ── 3. Job status check ───────────────────────────────────────────────────
    job_status = job.get("status", "")
    if job_status not in _ACTIVE_JOB_STATUSES:
        _log("warning", "job_not_active",
             jobId=job_id, thingName=thing_name, jobStatus=job_status)
        _audit("KEY_REQUEST_JOB_NOT_ACTIVE", thing_name,
               {"jobId": job_id, "jobStatus": job_status}, "FAILURE")
        raise ValueError(
            f"Job is not active (status={job_status}). "
            "Key can only be retrieved for QUEUED or IN_PROGRESS jobs."
        )

    # ── 4. Package record + encrypted key ────────────────────────────────────
    package_name = job.get("packageName", "")
    version      = job.get("version", "")
    if not package_name or not version:
        _log("error", "job_missing_package_info",
             jobId=job_id, packageName=package_name, version=version)
        raise RuntimeError("Internal error — job record missing package info")

    pkg = _get_package(package_name, version)
    if not pkg:
        _log("error", "package_not_found_for_job",
             jobId=job_id, packageName=package_name, version=version)
        raise RuntimeError("Package not found")

    encrypted_data_key = pkg.get("encryptedDataKey", "")
    aes_iv_b64         = pkg.get("aesIv", "")

    if not encrypted_data_key:
        if pkg.get("wrappedDataKey"):
            _log("error", "legacy_wrapped_key_not_supported",
                 jobId=job_id, packageName=package_name, version=version)
            raise RuntimeError(
                "This package was uploaded with an older key format. "
                "Please ask your administrator to re-upload the package."
            )
        _log("error", "no_encrypted_data_key",
             jobId=job_id, packageName=package_name, version=version)
        raise RuntimeError("Internal error — package has no encryption key")

    # ── 5. KMS decrypt ────────────────────────────────────────────────────────
    _log("info", "kms_decrypt_start",
         jobId=job_id, thingName=thing_name,
         packageName=package_name, version=version, kmsKeyId=KMS_KEY_ID)
    t_kms = time.monotonic()
    try:
        plaintext_key_bytes = _kms_decrypt(encrypted_data_key)
    except ClientError as exc:
        err_code = exc.response["Error"]["Code"]
        _log("error", "kms_decrypt_failed",
             jobId=job_id, thingName=thing_name,
             awsError=err_code, awsMessage=exc.response["Error"]["Message"])
        _audit("KEY_REQUEST_KMS_FAILED", thing_name,
               {"jobId": job_id, "packageName": package_name, "version": version},
               "FAILURE", kmsError=err_code)
        raise RuntimeError("Key decryption failed — please try again")
    kms_ms = int((time.monotonic() - t_kms) * 1000)

    plaintext_key_b64 = base64.b64encode(plaintext_key_bytes).decode()

    # ── 6. Increment rate limit + audit ──────────────────────────────────────
    _increment_rate_limit(job_id, thing_name)

    total_ms = int((time.monotonic() - t_start) * 1000)
    _log("info", "artifact_key_delivered",
         jobId=job_id, thingName=thing_name,
         packageName=package_name, version=version,
         kmsMs=kms_ms, totalMs=total_ms, requestCount=req_count + 1)
    _audit("ARTIFACT_KEY_DELIVERED", thing_name,
           {"jobId": job_id, "packageName": package_name, "version": version},
           "SUCCESS", kmsMs=kms_ms, totalMs=total_ms, requestCount=req_count + 1)

    return {
        "jobId":       job_id,
        "packageName": package_name,
        "version":     version,
        "dataKey":     plaintext_key_b64,
        "iv":          aes_iv_b64,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    request_id = context.aws_request_id if context else None
    t_start    = time.monotonic()

    if _is_mqtt_invocation(event):
        return _handle_mqtt(event, request_id, t_start)
    else:
        return _handle_http(event, request_id, t_start)


# ──────────────────────────────────────────────────────────────────────────────
# MQTT path  (Nitin's controller)
# ──────────────────────────────────────────────────────────────────────────────

def _handle_mqtt(event: dict, request_id: str, t_start: float):
    """
    Invoked by IoT Rule on topic: digilux/ota/{thingName}/key-delivery/request
    IoT Rule SQL injects thingName from topic(3).
    Publishes result to:          digilux/ota/{thingName}/key-delivery/response
    """
    # thingName is injected by the IoT Rule SQL: SELECT *, topic(3) AS thingName
    thing_name = (event.get("thingName") or "").strip()
    job_id     = (event.get("jobId")     or "").strip()

    _log("info", "mqtt_key_request",
         thingName=thing_name, jobId=job_id, requestId=request_id)

    if not thing_name or not job_id:
        _log("error", "mqtt_missing_fields",
             thingName=thing_name, jobId=job_id,
             detail="IoT Rule must inject thingName via topic(3); device must send jobId")
        # Cannot publish error — no thingName to target
        return

    if not _THING_RE.match(thing_name) or not _JOB_ID_RE.match(job_id):
        _log("error", "mqtt_invalid_field_format",
             thingName=thing_name, jobId=job_id)
        _mqtt_publish(thing_name, {"error": "Invalid thingName or jobId format"})
        return

    try:
        result = _process_key_request(job_id, thing_name, t_start)
        _mqtt_publish(thing_name, result)
    except ValueError as e:
        # Client error — tell the device
        _log("warning", "mqtt_key_request_denied",
             thingName=thing_name, jobId=job_id, reason=str(e))
        _mqtt_publish(thing_name, {"error": str(e)})
    except Exception as e:
        # Server error — generic message to device, full detail in CloudWatch
        _log("error", "mqtt_key_request_failed",
             thingName=thing_name, jobId=job_id,
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in MQTT key request: {e}")
        _mqtt_publish(thing_name, {"error": "Key delivery failed — please retry"})


# ──────────────────────────────────────────────────────────────────────────────
# HTTP path  (non-constrained clients, SigV4)
# ──────────────────────────────────────────────────────────────────────────────

def _handle_http(event: dict, request_id: str, t_start: float):
    """
    Invoked by API Gateway.
    POST /api/v1/ota/device/artifact-key
    Auth: AWS IAM SigV4 (IoT Credentials Provider)
    """
    try:
        # ── Extract caller thingName from SigV4 IAM identity ─────────────────
        caller_thing = _extract_thing_name_from_iam(event)
        if not caller_thing:
            _log("warning", "iam_thing_name_missing", requestId=request_id)
            return _resp(403, {"error": "Forbidden — cannot determine device identity"})

        _log("info", "http_key_request",
             callerThing=caller_thing, requestId=request_id)

        # ── Parse body ────────────────────────────────────────────────────────
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

        # ── SigV4 identity must match the requested thingName ─────────────────
        if caller_thing != thing_name:
            _log("warning", "thing_name_mismatch",
                 callerThing=caller_thing, requestedThing=thing_name, jobId=job_id)
            _audit("KEY_REQUEST_FORBIDDEN_THING_MISMATCH", caller_thing,
                   {"jobId": job_id, "requestedThing": thing_name},
                   "FAILURE", reason="thing_name_mismatch")
            return _resp(403, {"error": "Forbidden — thingName does not match caller identity"})

        result = _process_key_request(job_id, thing_name, t_start)
        return _resp(200, result)

    except ValueError as e:
        return _resp(409, {"error": str(e)})
    except Exception as e:
        _log("error", "unhandled_exception",
             error=str(e), excType=type(e).__name__)
        log.exception(f"Unhandled error in HTTP key request: {e}")
        return _resp(500, {"error": "Internal server error"})
