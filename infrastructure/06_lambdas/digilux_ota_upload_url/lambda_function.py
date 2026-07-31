"""
digilux_ota_upload_url
Step 1 of the cleaner upload flow.
Admin calls this to get a pre-signed S3 PUT URL.
After uploading the binary to that URL, registration happens automatically.

POST /api/v1/ota/packages/upload-url
Admin-only.

Request body:
{
  "packageName": "controller-app",
  "version": "2.0.0",
  "packageType": "CONTROLLER_APP",
  "fileName": "controller-app.tar.gz",       # optional — defaults to artifact.bin
  "releaseNotes": "...",                      # optional
  "compatibleModels": ["DGX-1000"],           # optional
  "minHwRevision": "1.0",                     # optional
  "dependsOn": {"some-pkg": "1.0.0"},         # optional
  "incompatibleWith": {}                      # optional
}

Response:
{
  "uploadUrl": "https://...",        # PUT this URL with the binary
  "s3Key": "application/...",
  "expiresIn": 3600,
  "packageName": "controller-app",
  "version": "2.0.0",
  "status": "PENDING"               # becomes ACTIVE automatically after upload
}
"""
import datetime
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")
COMPAT_TABLE    = os.environ.get("COMPAT_TABLE",   "digilux_ota_compatibility")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET","digilux-ota-artifacts")
UPLOAD_EXPIRY   = int(os.environ.get("UPLOAD_EXPIRY_SEC", "3600"))


def _audit(event: str, actor: str, resource: dict, result: str, **extra) -> None:
    """Emit a structured audit record — filterable in CloudWatch Logs Insights
    with: filter audit = 1"""
    print(json.dumps({
        "audit": True,
        "event": event,
        "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor": actor,
        "resource": resource,
        "result": result,
        **extra,
    }))


# Maps packageType → S3 folder prefix
TYPE_PREFIX = {
    "CONTROLLER_FIRMWARE": "firmware",
    "CONTROLLER_APP":      "application",
    "DRIVER":              "drivers",
    "ZIGBEE_DEVICE":       "zigbee-devices",
    "CONFIG":              "config",
    "RULES":               "rules",
}

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)


def lambda_handler(event, context):
    method = event.get("httpMethod", "POST").upper()
    path   = event.get("path", "")
    log.info(json.dumps({"msg": "request_received", "method": method, "path": path}))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "admin" not in claims.get("cognito:groups", ""):
            log.warning("Unauthorized access attempt — not in admin group")
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))
        log.debug(json.dumps({"msg": "actor_identified", "actor": caller}))

        if method == "GET":
            return _list_packages(event, caller)

        body = json.loads(event.get("body") or "{}")
        log.debug(json.dumps({"msg": "request_body_parsed", "keys": list(body.keys())}))

        for field in ["packageName", "version", "packageType"]:
            if not body.get(field):
                log.warning(f"Validation failed — missing required field: {field}")
                return _response(400, {"error": f"Missing required field: {field}"})

        pkg_name  = body["packageName"].strip()
        version   = body["version"].strip()
        pkg_type  = body["packageType"].strip().upper()
        file_name = body.get("fileName", "artifact.bin").strip()

        log.info(json.dumps({
            "msg": "upload_url_request",
            "packageName": pkg_name, "version": version,
            "packageType": pkg_type, "fileName": file_name,
        }))

        if pkg_type not in TYPE_PREFIX:
            log.warning(f"Invalid packageType: {pkg_type}")
            return _response(400, {"error": f"Invalid packageType. Must be one of: {', '.join(TYPE_PREFIX)}"})

        # Reject duplicate active versions
        table = dynamo.Table(PACKAGES_TABLE)
        log.debug(f"Checking for existing package {pkg_name}@{version}")
        existing = table.get_item(Key={"packageName": pkg_name, "version": version}).get("Item")
        if existing and existing.get("status") == "ACTIVE":
            log.warning(f"Duplicate upload rejected: {pkg_name}@{version} already ACTIVE")
            _audit("PACKAGE_UPLOAD_REJECTED", caller,
                   {"packageName": pkg_name, "version": version},
                   "FAILURE", reason="already_active")
            return _response(409, {
                "error": f"Package {pkg_name}@{version} already exists and is ACTIVE. Use a new version number."
            })

        prefix = TYPE_PREFIX[pkg_type]
        s3_key = f"{prefix}/{pkg_name}/{version}/{file_name}"
        log.debug(f"S3 key resolved: s3://{ARTIFACT_BUCKET}/{s3_key}")

        # Generate pre-signed PUT URL
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": ARTIFACT_BUCKET,
                "Key":    s3_key,
                "ContentType": "application/octet-stream",
            },
            ExpiresIn=UPLOAD_EXPIRY,
            HttpMethod="PUT",
        )
        log.info(f"Pre-signed PUT URL generated for {s3_key}, expires in {UPLOAD_EXPIRY}s")

        now_ms = int(time.time() * 1000)

        # Write PENDING record — processor Lambda promotes it to ACTIVE after upload
        table.put_item(Item={
            "packageName":  pkg_name,
            "version":      version,
            "packageType":  pkg_type,
            "s3Key":        s3_key,
            "s3Bucket":     ARTIFACT_BUCKET,
            "status":       "PENDING",
            "releaseNotes": body.get("releaseNotes", ""),
            "createdAt":    now_ms,
            "createdBy":    caller,
        })
        log.info(f"Package {pkg_name}@{version} record created with status=PENDING")

        # Write compatibility metadata immediately (doesn't need the binary)
        compat_item = {
            "packageName":      pkg_name,
            "version":          version,
            "compatibleModels": body.get("compatibleModels", []),
            "minHwRevision":    body.get("minHwRevision", ""),
            "dependsOn":        body.get("dependsOn", {}),
            "incompatibleWith": body.get("incompatibleWith", {}),
        }
        dynamo.Table(COMPAT_TABLE).put_item(Item=compat_item)
        log.debug(json.dumps({"msg": "compat_record_written", **compat_item}))

        _audit("PACKAGE_UPLOAD_URL_REQUESTED", caller,
               {"packageName": pkg_name, "version": version, "packageType": pkg_type},
               "SUCCESS",
               s3Key=s3_key,
               compatibleModels=body.get("compatibleModels", []),
               minHwRevision=body.get("minHwRevision", ""))

        log.info(f"Upload URL flow complete for {pkg_name}@{version}")
        return _response(200, {
            "uploadUrl":   upload_url,
            "s3Key":       s3_key,
            "expiresIn":   UPLOAD_EXPIRY,
            "packageName": pkg_name,
            "version":     version,
            "packageType": pkg_type,
            "status":      "PENDING",
            "instructions": (
                "PUT your binary to uploadUrl with Content-Type: application/octet-stream. "
                "The package will be registered automatically within seconds of upload."
            ),
        })

    except Exception as e:
        log.exception(f"Unhandled error in upload_url handler: {e}")
        return _response(500, {"error": "Internal server error"})


def _list_packages(event: dict, caller: str) -> dict:
    """GET /api/v1/ota/packages — list all packages, optionally filtered by status."""
    params     = event.get("queryStringParameters") or {}
    status_filter = params.get("status", "ACTIVE").upper()
    pkg_name_filter = params.get("packageName")
    print(json.dumps({
        "msg": "list_packages_request",
        "actor": caller,
        "statusFilter": status_filter,
        "packageNameFilter": pkg_name_filter,
    }))

    table = dynamo.Table(PACKAGES_TABLE)

    if pkg_name_filter:
        # Query by packageName (hash key) to list all versions of one package
        from boto3.dynamodb.conditions import Key as DKey
        result = table.query(
            KeyConditionExpression=DKey("packageName").eq(pkg_name_filter)
        )
    else:
        result = table.scan(
            FilterExpression="#s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status_filter},
        )

    items = result.get("Items", [])
    # Strip large fields not needed in list view
    packages = [
        {
            "packageName":  i.get("packageName"),
            "version":      i.get("version"),
            "packageType":  i.get("packageType"),
            "status":       i.get("status"),
            "artifactSize": int(i["artifactSize"]) if "artifactSize" in i else None,
            "releaseNotes": i.get("releaseNotes", ""),
            "createdBy":    i.get("createdBy"),
            "createdAt":    int(i["createdAt"]) if "createdAt" in i else None,
        }
        for i in items
    ]
    packages.sort(key=lambda x: (x["packageName"], x["version"]), reverse=False)
    log.info(json.dumps({"msg": "list_packages_result", "actor": caller, "count": len(packages),
                         "statusFilter": status_filter, "packageNameFilter": pkg_name_filter}))
    return _response(200, {"packages": packages, "count": len(packages)})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
