"""
digilux_ota_user_update_status
Returns the status of a user-initiated OTA update.

GET /api/v1/ota/my/updates/{jobId}/status

Auth: Cognito ID token (any authenticated user).
Security: the jobId must belong to a consent record owned by the calling userId.
Users cannot query the status of other users' jobs.
"""
import datetime
import json
import logging
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION          = os.environ["REGION"]
OTA_JOBS_TABLE  = os.environ.get("OTA_JOBS_TABLE",  "digilux_ota_jobs")
CONSENTS_TABLE  = os.environ.get("CONSENTS_TABLE",   "digilux_ota_user_consents")

CONSENTS_JOB_INDEX = os.environ.get("CONSENTS_JOB_INDEX", "jobId-index")

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot",        region_name=REGION)

_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9\-_]{1,128}$")


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


def _get_consent_by_job_id(job_id: str) -> dict | None:
    """Look up the consent record for a given jobId via GSI."""
    tbl  = dynamo.Table(CONSENTS_TABLE)
    resp = tbl.query(
        IndexName=CONSENTS_JOB_INDEX,
        KeyConditionExpression=Key("jobId").eq(job_id),
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_job(job_id: str) -> dict | None:
    return dynamo.Table(OTA_JOBS_TABLE).get_item(Key={"jobId": job_id}).get("Item")


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    try:
        # ── Auth ──────────────────────────────────────────────────────────────
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            log.warning("Missing sub claim in token")
            return _resp(401, {"error": "Unauthorized — invalid token"})

        # ── Path parameter ────────────────────────────────────────────────────
        path_params = event.get("pathParameters") or {}
        job_id      = (path_params.get("jobId") or "").strip()

        if not job_id or not _JOB_ID_RE.match(job_id):
            log.warning(f"Invalid or missing jobId: {job_id!r}")
            return _resp(400, {"error": "Invalid or missing jobId"})

        log.info(json.dumps({"msg": "status_request", "userId": user_id, "jobId": job_id}))

        # ── Ownership check via consent record ────────────────────────────────
        # Never return job data without confirming the jobId belongs to this user.
        consent = _get_consent_by_job_id(job_id)
        if not consent:
            # Could be an admin-created job — we don't expose those here
            log.warning(f"No consent record found for jobId={job_id}")
            return _resp(404, {"error": "Update not found"})

        if consent.get("userId") != user_id:
            log.warning(json.dumps({
                "msg":           "ownership_mismatch",
                "requestUserId": user_id,
                "consentUserId": consent.get("userId"),
                "jobId":         job_id,
            }))
            # Return 404 — don't reveal that the job exists for another user
            return _resp(404, {"error": "Update not found"})

        # ── Fetch job record from DynamoDB ────────────────────────────────────
        job = _get_job(job_id)
        if not job:
            log.error(f"Consent exists but job record missing for jobId={job_id}")
            return _resp(404, {"error": "Update not found"})

        # ── Enrich with live IoT status ───────────────────────────────────────
        iot_status     = {}
        iot_job_status = None
        try:
            iot_job        = iot.describe_job(jobId=job_id)["job"]
            iot_status     = iot_job.get("jobProcessDetails", {})
            iot_job_status = iot_job.get("status")
        except Exception as e:
            log.warning(f"Could not fetch live IoT status for {job_id}: {e}")

        # ── Build user-friendly response (no internal ARNs or infra details) ──
        device_statuses = job.get("deviceStatuses") or {}
        # Resolve per-device status for the user's device
        device_id         = job.get("targetId", "")
        this_device_status = device_statuses.get(device_id, {})

        status_map = {
            "QUEUED":      "Your device is queued for the update.",
            "IN_PROGRESS": "Your device is downloading and installing the update.",
            "SUCCEEDED":   "The update was installed successfully.",
            "FAILED":      "The update failed. Your device may have rolled back to the previous version.",
            "CANCELLED":   "The update was cancelled.",
            "REJECTED":    "Your device rejected the update.",
        }
        job_status   = job.get("status", "UNKNOWN")
        status_label = status_map.get(job_status, "Unknown status")

        response_body = {
            "jobId":         job_id,
            "consentId":     consent.get("consentId"),
            "packageName":   job.get("packageName"),
            "version":       job.get("version"),
            "status":        job_status,
            "statusMessage": status_label,
            "consentedAt":   int(consent.get("consentedAt", 0)),
            "createdAt":     int(job["createdAt"]) if "createdAt" in job else None,
            "completedAt":   int(job["completedAt"]) if "completedAt" in job else None,
            "progress":      this_device_status.get("progress"),
            "statusDetail":  this_device_status.get("statusDetail"),
        }

        # Only include IoT counters if available (useful for debugging via support)
        if iot_status:
            response_body["deviceProgress"] = {
                "succeeded":  iot_status.get("numberOfSucceededThings", 0),
                "failed":     iot_status.get("numberOfFailedThings", 0),
                "inProgress": iot_status.get("numberOfInProgressThings", 0),
                "queued":     iot_status.get("numberOfQueuedThings", 0),
            }

        log.info(json.dumps({
            "msg":       "status_fetched",
            "userId":    user_id,
            "jobId":     job_id,
            "jobStatus": job_status,
        }))

        return _resp(200, json.loads(json.dumps(response_body, cls=_Dec)))

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        log.error(json.dumps({"msg": "aws_client_error", "code": code, "error": msg}))
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        log.exception(f"Unhandled error in user_update_status: {e}")
        return _resp(500, {"error": "Internal server error"})
