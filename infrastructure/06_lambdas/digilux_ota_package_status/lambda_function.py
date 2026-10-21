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
import time

import boto3

import _notif

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
COMPAT_TABLE    = os.environ.get("COMPAT_TABLE",    "digilux_ota_compatibility")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)


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
            log.warning(json.dumps({
                "msg": "unauthorized_access", "method": method,
                "packageName": package_name, "version": version,
                "caller": caller, "groups": claims.get("cognito:groups", ""),
            }))
            _audit("UNAUTHORIZED_ACCESS", caller,
                   {"packageName": package_name, "version": version, "method": method},
                   "FAILURE", reason="not_in_ota_admin_group",
                   groups=claims.get("cognito:groups", ""))
            return _response(403, {"error": "Admin access required"})

        if not package_name or not version:
            log.warning(json.dumps({
                "msg": "missing_path_params", "method": method,
                "packageName": package_name, "version": version, "caller": caller,
            }))
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
        log.warning(json.dumps({"msg": "package_not_found",
                                "packageName": package_name, "version": version}))
        return _response(404, {"error": f"{package_name} v{version} not found"})

    log.debug(json.dumps({"msg": "package_status_returned",
                          "packageName": package_name, "version": version,
                          "status": item.get("status")}))
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
        log.warning(json.dumps({
            "msg": "delete_missing_reason",
            "packageName": package_name, "version": version, "caller": caller,
        }))
        return _response(400, {"error": "reason is required for deletion"})

    table  = dynamo.Table(PACKAGES_TABLE)
    result = table.get_item(Key={"packageName": package_name, "version": version})
    item   = result.get("Item")

    if not item:
        log.warning(json.dumps({
            "msg": "delete_package_not_found",
            "packageName": package_name, "version": version, "caller": caller,
        }))
        return _response(404, {"error": f"{package_name} v{version} not found"})

    if item.get("status") == "ACTIVE" and not force:
        log.warning(json.dumps({
            "msg": "delete_blocked_active",
            "packageName": package_name, "version": version, "caller": caller,
        }))
        _audit("PACKAGE_DELETE_BLOCKED", caller,
               {"packageName": package_name, "version": version}, "FAILURE",
               reason="package_is_active", hint="set force=true to override")
        return _response(409, {
            "error": f"{package_name} v{version} is ACTIVE. Pass force=true to delete anyway.",
        })

    log.info(json.dumps({
        "msg": "delete_package_start",
        "packageName": package_name, "version": version,
        "caller": caller, "force": force, "reason": reason,
    }))

    # Delete S3 artifact if key is known
    s3_key = item.get("s3Key")
    s3_deleted = False
    if s3_key:
        try:
            s3.delete_object(Bucket=ARTIFACT_BUCKET, Key=s3_key)
            s3_deleted = True
            log.info(json.dumps({"msg": "s3_artifact_deleted",
                                 "bucket": ARTIFACT_BUCKET, "key": s3_key}))
        except Exception as e:
            log.warning(json.dumps({"msg": "s3_artifact_delete_failed",
                                    "bucket": ARTIFACT_BUCKET, "key": s3_key, "error": str(e)}))

    # Soft-delete: mark status DELETED in DynamoDB
    now_ms = int(time.time() * 1000)
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
        log.info(json.dumps({"msg": "compat_record_deleted",
                             "packageName": package_name, "version": version}))
    except Exception as e:
        log.warning(json.dumps({"msg": "compat_record_delete_failed",
                                "packageName": package_name, "version": version,
                                "error": str(e)}))

    log.info(json.dumps({
        "msg": "package_deleted", "packageName": package_name,
        "version": version, "caller": caller, "s3Deleted": s3_deleted,
        "previousStatus": item.get("status"), "force": force,
    }))
    _audit("PACKAGE_DELETED", caller,
           {"packageName": package_name, "version": version}, "SUCCESS",
           deviceType=item.get("deviceType", ""), s3Deleted=s3_deleted,
           previousStatus=item.get("status"), force=force, reason=reason)

    try:
        _notif.notify_admin(
            *_notif.tmpl_package_deleted(package_name, version, item.get("deviceType", ""), caller, reason)
        )
    except Exception as e:
        log.warning(json.dumps({"msg": "notify_admin_failed",
                                "packageName": package_name, "version": version, "error": str(e)}))

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
