"""
digilux_ota_package_status
Returns the current status of a specific package version.
Used by the Flutter app to poll upload progress after PUT/complete.

GET /api/v1/ota/packages/{packageName}/{version}
Admin-only.

Response:
{
  "packageName":  "HomeAssistantUtility",
  "version":      "4.2.2",
  "status":       "PENDING" | "ACTIVE" | "CORRUPTED",
  "activated":    false,
  "uploadType":   "SINGLE" | "MULTIPART",
  "deviceType":   "Network_controller_firmware",
  "releaseType":  "PROD",
  "fileName":     "HomeAssistantUtility-4.2.2.jar",
  "sha256":       "c320c4...",          # present after ACTIVE
  "artifactSize": 17825792,             # present after ACTIVE
  "corruptReason":"checksum_mismatch",  # present if CORRUPTED
  "releaseNotes": "Bug fixes",
  "createdBy":    "admin@digilux.com",
  "createdAt":    1723556789000
}
"""
import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION         = os.environ["REGION"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")

dynamo = boto3.resource("dynamodb", region_name=REGION)


def lambda_handler(event, context):
    path_params  = event.get("pathParameters") or {}
    package_name = path_params.get("packageName", "").strip()
    version      = path_params.get("version", "").strip()

    log.info(json.dumps({
        "msg": "package_status_request",
        "packageName": package_name,
        "version": version,
    }))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "ota-admin" not in claims.get("cognito:groups", ""):
            return _response(403, {"error": "Admin access required"})

        if not package_name or not version:
            return _response(400, {"error": "packageName and version path parameters are required"})

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

        # Fields present only after artifact_processor runs
        if "sha256" in item:
            resp["sha256"] = item["sha256"]
        if "artifactSize" in item:
            resp["artifactSize"] = int(item["artifactSize"])
        if "corruptReason" in item:
            resp["corruptReason"] = item["corruptReason"]
        if "totalSize" in item:
            resp["totalSize"] = int(item["totalSize"])

        return _response(200, resp)

    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
