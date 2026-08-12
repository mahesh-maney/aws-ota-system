"""
digilux_ota_upload_url
Step 1 of the upload flow. Admin calls this to get a pre-signed S3 PUT URL.
After uploading the binary to that URL, registration happens automatically
via artifact_processor.

POST /api/v1/ota/packages/upload-url   — get upload URL
GET  /api/v1/ota/packages              — list packages
Admin-only.

Request body (POST):
{
  "deviceType":   "Network_controller_zigbee_firmware",   # required
  "version":      "4.2.2",                                # required
  "releaseType":  "BETA" | "PROD",                        # required
  "releaseNotes": "Bug fixes"                             # optional
}

packageName and fileName are derived automatically:
  packageName = baseName from DEVICE_TYPE_MAP  (e.g. ZigbeeFirmware)
  fileName    = {baseName}-{version}{ext}       (e.g. ZigbeeFirmware-4.2.2.tar)

Packages are created with activated=False. An admin must explicitly call
PATCH /api/v1/ota/packages/{packageName}/{version}/activate to make them
visible to end users.

Response:
{
  "uploadUrl":   "https://...",
  "s3Key":       "...",
  "expiresIn":   3600,
  "packageName": "ZigbeeFirmware",
  "version":     "4.2.2",
  "deviceType":  "Network_controller_zigbee_firmware",
  "releaseType": "BETA",
  "activated":   false,
  "status":      "PENDING"
}
"""
import datetime
import json
import logging
import os
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
UPLOAD_EXPIRY   = int(os.environ.get("UPLOAD_EXPIRY_SEC", "3600"))

# Maps deviceType → packageName (baseName) and file extension.
# fileName is derived as: {baseName}-{version}{ext}
# S3 key:                 {deviceType}/{baseName}/{version}/{fileName}
DEVICE_TYPE_MAP = {
    "Network_controller_firmware": {
        "baseName": "HomeAssistantUtility",
        "ext":      ".jar",
    },
    "Network_controller_zigbee_firmware": {
        "baseName": "ZigbeeFirmware",
        "ext":      ".tar",
    },
    "Network_controller_Z2M_Firmware": {
        "baseName": "Z2MFirmware",
        "ext":      ".bin",
    },
    "Network_controller_Miscellaneous": {
        "baseName": "NetControllerMisc",
        "ext":      ".py",
    },
}

VALID_RELEASE_TYPES = {"BETA", "PROD"}

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3",         region_name=REGION)


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
    method = event.get("httpMethod", "POST").upper()
    path   = event.get("path", "")
    log.info(json.dumps({"msg": "request_received", "method": method, "path": path}))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "admin" not in claims.get("cognito:groups", ""):
            log.warning("Unauthorized — not in admin group")
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))

        if method == "GET":
            return _list_packages(event, caller)

        body = json.loads(event.get("body") or "{}")

        # ── Validate required fields ──────────────────────────────────────────
        for field in ["deviceType", "version", "releaseType"]:
            if not body.get(field):
                return _response(400, {"error": f"Missing required field: {field}"})

        device_type  = body["deviceType"].strip()
        version      = body["version"].strip()
        release_type = body["releaseType"].strip().upper()

        if device_type not in DEVICE_TYPE_MAP:
            return _response(400, {
                "error": f"Invalid deviceType. Must be one of: {', '.join(DEVICE_TYPE_MAP)}"
            })

        if release_type not in VALID_RELEASE_TYPES:
            return _response(400, {
                "error": f"Invalid releaseType. Must be one of: {', '.join(VALID_RELEASE_TYPES)}"
            })

        # ── Derive packageName and fileName from deviceType ───────────────────
        type_cfg     = DEVICE_TYPE_MAP[device_type]
        package_name = type_cfg["baseName"]
        file_name    = f"{package_name}-{version}{type_cfg['ext']}"
        s3_key       = f"{device_type}/{package_name}/{version}/{file_name}"

        log.info(json.dumps({
            "msg":         "upload_url_request",
            "deviceType":  device_type,
            "packageName": package_name,
            "version":     version,
            "releaseType": release_type,
            "fileName":    file_name,
        }))

        # ── Reject duplicate active versions ──────────────────────────────────
        table    = dynamo.Table(PACKAGES_TABLE)
        existing = table.get_item(
            Key={"packageName": package_name, "version": version}
        ).get("Item")
        if existing and existing.get("status") == "ACTIVE":
            _audit("PACKAGE_UPLOAD_REJECTED", caller,
                   {"packageName": package_name, "version": version},
                   "FAILURE", reason="already_active")
            return _response(409, {
                "error": f"{package_name} v{version} already exists and is ACTIVE. Use a new version number."
            })

        # ── Generate pre-signed PUT URL ───────────────────────────────────────
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      ARTIFACT_BUCKET,
                "Key":         s3_key,
                "ContentType": "application/octet-stream",
            },
            ExpiresIn=UPLOAD_EXPIRY,
            HttpMethod="PUT",
        )

        # ── Write PENDING record — artifact_processor promotes to ACTIVE ──────
        now_ms = int(time.time() * 1000)
        table.put_item(Item={
            "packageName":  package_name,
            "version":      version,
            "deviceType":   device_type,
            "releaseType":  release_type,
            "fileName":     file_name,
            "s3Key":        s3_key,
            "s3Bucket":     ARTIFACT_BUCKET,
            "status":       "PENDING",
            "activated":    False,
            "releaseNotes": body.get("releaseNotes", ""),
            "createdAt":    now_ms,
            "createdBy":    caller,
        })

        _audit("PACKAGE_UPLOAD_URL_REQUESTED", caller,
               {"packageName": package_name, "version": version},
               "SUCCESS", deviceType=device_type, releaseType=release_type, s3Key=s3_key)

        return _response(200, {
            "uploadUrl":   upload_url,
            "s3Key":       s3_key,
            "expiresIn":   UPLOAD_EXPIRY,
            "packageName": package_name,
            "version":     version,
            "deviceType":  device_type,
            "releaseType": release_type,
            "activated":   False,
            "status":      "PENDING",
            "instructions": (
                "PUT your binary to uploadUrl with Content-Type: application/octet-stream. "
                "Status becomes ACTIVE automatically within seconds. "
                "Then call PATCH /packages/{packageName}/{version}/activate to make it visible to users."
            ),
        })

    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _list_packages(event: dict, caller: str) -> dict:
    """GET /api/v1/ota/packages — list packages, optionally filtered."""
    from boto3.dynamodb.conditions import Attr
    params       = event.get("queryStringParameters") or {}
    status_f     = params.get("status", "ACTIVE").upper()
    device_type_f = params.get("deviceType")
    release_f    = (params.get("releaseType") or "").upper() or None

    table = dynamo.Table(PACKAGES_TABLE)

    if device_type_f:
        if device_type_f not in DEVICE_TYPE_MAP:
            return _response(400, {"error": f"Invalid deviceType filter: {device_type_f}"})
        from boto3.dynamodb.conditions import Key as DKey
        pkg_name = DEVICE_TYPE_MAP[device_type_f]["baseName"]
        result = table.query(KeyConditionExpression=DKey("packageName").eq(pkg_name))
    else:
        filter_expr = Attr("status").eq(status_f)
        if release_f:
            filter_expr = filter_expr & Attr("releaseType").eq(release_f)
        result = table.scan(FilterExpression=filter_expr)

    items = result.get("Items", [])
    packages = [
        {
            "packageName":  i.get("packageName"),
            "version":      i.get("version"),
            "deviceType":   i.get("deviceType"),
            "releaseType":  i.get("releaseType"),
            "fileName":     i.get("fileName"),
            "status":       i.get("status"),
            "activated":    i.get("activated", False),
            "artifactSize": int(i["artifactSize"]) if "artifactSize" in i else None,
            "releaseNotes": i.get("releaseNotes", ""),
            "createdBy":    i.get("createdBy"),
            "createdAt":    int(i["createdAt"]) if "createdAt" in i else None,
        }
        for i in items
    ]
    packages.sort(key=lambda x: (x["packageName"], x["version"]))
    log.info(json.dumps({"msg": "list_packages_result", "actor": caller, "count": len(packages)}))
    return _response(200, {"packages": packages, "count": len(packages)})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body),
    }
