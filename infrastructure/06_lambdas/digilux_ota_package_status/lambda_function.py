"""
digilux_ota_package_status
Returns the current status of a specific package version, and handles deletion.

GET    /api/v1/ota/packages/{packageName}/{version}  — poll upload / view status
DELETE /api/v1/ota/packages/{packageName}/{version}  — soft-delete package
Admin-only.

DELETE body (JSON):
  { "reason": "...", "force": false }
  force=true bypasses the ACTIVE guard (admin override).

On DELETE: S3 artifact is removed and DynamoDB status is set to DELETED.
Deleted versions are filtered out in the UI and may be re-uploaded.
"""
import datetime
import json
import logging
import os

import boto3

import _notif

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
COMPAT_TABLE    = os.environ.get("COMPAT_TABLE",    "digilux_ota_compatibility")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)


def lambda_handler(event, context):
    method       = event.get("httpMethod", "GET").upper()
    path_params  = event.get("pathParameters") or {}
    package_name = path_params.get("packageName", "").strip()
    version      = path_params.get("version", "").strip()

    log.info(json.dumps({"msg": "request", "method": method,
                         "packageName": package_name, "version": version}))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        caller = claims.get("email", claims.get("sub", "unknown"))
        if "ota-admin" not in claims.get("cognito:groups", ""):
            return _response(403, {"error": "Admin access required"})

        if not package_name or not version:
            return _response(400, {"error": "packageName and version path parameters are required"})

        if method == "DELETE":
            return _delete_package(event, package_name, version, caller)

        return _get_status(package_name, version)

    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _get_status(package_name, version):
    table  = dynamo.Table(PACKAGES_TABLE)
    result = table.get_item(Key={"packageName": package_name, "version": version})
    item   = result.get("Item")

    if not item:
        return _response(404, {"error": f"{package_name} v{version} not found"})

    resp = {
        "packageName":  item.get("packageName"),
        "version":      item.get("version"),
        "status":       item.get("status"),
        "activated":    item.get("activated", False),
        "uploadType":   item.get("uploadType", "SINGLE"),
        "deviceType":   item.get("deviceType"),
        "releaseType":  item.get("releaseType"),
        "fileName":     item.get("fileName"),
        "s3Key":        item.get("s3Key"),
        "releaseNotes": item.get("releaseNotes", ""),
        "createdBy":    item.get("createdBy"),
        "createdAt":    int(item["createdAt"]) if "createdAt" in item else None,
    }
    if "sha256"        in item: resp["sha256"]        = item["sha256"]
    if "artifactSize"  in item: resp["artifactSize"]  = int(item["artifactSize"])
    if "corruptReason" in item: resp["corruptReason"] = item["corruptReason"]
    if "totalSize"     in item: resp["totalSize"]     = int(item["totalSize"])

    return _response(200, resp)


def _delete_package(event, package_name, version, caller):
    body  = json.loads(event.get("body") or "{}")
    reason = str(body.get("reason") or "").strip()
    force  = bool(body.get("force", False))

    if not reason:
        return _response(400, {"error": "reason is required for deletion"})

    table  = dynamo.Table(PACKAGES_TABLE)
    result = table.get_item(Key={"packageName": package_name, "version": version})
    item   = result.get("Item")

    if not item:
        return _response(404, {"error": f"{package_name} v{version} not found"})

    if item.get("status") == "ACTIVE" and not force:
        return _response(409, {
            "error": f"{package_name} v{version} is ACTIVE. Pass force=true to delete anyway.",
        })

    # Delete S3 artifact if key is known
    s3_key = item.get("s3Key")
    s3_deleted = False
    if s3_key:
        try:
            s3.delete_object(Bucket=ARTIFACT_BUCKET, Key=s3_key)
            s3_deleted = True
            log.info(json.dumps({"msg": "s3_artifact_deleted", "bucket": ARTIFACT_BUCKET, "key": s3_key}))
        except Exception as e:
            log.warning(f"S3 delete failed (non-fatal): {e}")

    # Soft-delete: mark status DELETED in DynamoDB
    now_ms = int(__import__("time").time() * 1000)
    table.update_item(
        Key={"packageName": package_name, "version": version},
        UpdateExpression="SET #st = :s, deletedAt = :da, deletedBy = :db, deleteReason = :dr",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":s":  "DELETED",
            ":da": now_ms,
            ":db": caller,
            ":dr": reason,
        },
    )

    # Remove the corresponding compatibility record
    try:
        dynamo.Table(COMPAT_TABLE).delete_item(
            Key={"packageName": package_name, "version": version}
        )
        log.info(json.dumps({"msg": "compat_record_deleted", "packageName": package_name, "version": version}))
    except Exception as e:
        log.warning(f"Compatibility record delete failed (non-fatal): {e}")

    log.info(json.dumps({
        "msg": "package_deleted", "packageName": package_name,
        "version": version, "caller": caller, "s3Deleted": s3_deleted,
    }))
    _notif.notify_admin(
        *_notif.tmpl_package_deleted(package_name, version, item.get("deviceType", ""), caller, reason)
    )

    return _response(200, {
        "message":     f"{package_name} v{version} deleted.",
        "packageName": package_name,
        "version":     version,
        "status":      "DELETED",
        "s3Deleted":   s3_deleted,
    })


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
