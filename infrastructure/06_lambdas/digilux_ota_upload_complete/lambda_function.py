"""
digilux_ota_upload_complete
Step 2 of the multipart upload flow — called after Flutter has PUT all chunks to S3.
Completes the S3 multipart upload, which triggers artifact_processor via S3 event.

POST /api/v1/ota/packages/upload-artefact/complete
Admin-only.

Request body:
{
  "packageName": "HomeAssistantUtility",
  "version":     "4.2.2",
  "parts": [
    { "partNumber": 1, "etag": "\"d8e8fca2dc0f896fd7cb4cb0031ba249\"" },
    { "partNumber": 2, "etag": "\"abc123...\"" }
  ]
}

On success: S3 assembles the object → s3:ObjectCreated fires →
artifact_processor verifies token + checksum → promotes to ACTIVE.

Poll GET /api/v1/ota/packages/{packageName}/{version} until status = ACTIVE | CORRUPTED.
"""
import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3",         region_name=REGION)


def lambda_handler(event, context):
    log.info(json.dumps({"msg": "upload_complete_request"}))

    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "ota-admin" not in claims.get("cognito:groups", ""):
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))
        body   = json.loads(event.get("body") or "{}")

        package_name = (body.get("packageName") or "").strip()
        version      = (body.get("version") or "").strip()
        parts        = body.get("parts") or []

        if not package_name or not version:
            return _response(400, {"error": "Missing required fields: packageName, version"})
        if not parts:
            return _response(400, {"error": "Missing required field: parts"})

        # Load the PENDING record
        table  = dynamo.Table(PACKAGES_TABLE)
        result = table.get_item(Key={"packageName": package_name, "version": version})
        item   = result.get("Item")

        if not item:
            return _response(404, {"error": f"{package_name} v{version} not found"})
        if item.get("status") != "PENDING":
            return _response(409, {
                "error": f"Package is not PENDING (status={item.get('status')})"
            })
        if item.get("uploadType") != "MULTIPART":
            return _response(400, {
                "error": "This package was initiated as SINGLE upload — use PUT to uploadUrl directly"
            })

        upload_id = item.get("uploadId")
        s3_key    = item.get("s3Key")

        if not upload_id or not s3_key:
            return _response(500, {"error": "Missing uploadId or s3Key in package record"})

        # Sort parts by partNumber and complete the multipart upload
        sorted_parts = sorted(parts, key=lambda p: int(p["partNumber"]))
        multipart_parts = [
            {"PartNumber": int(p["partNumber"]), "ETag": p["etag"]}
            for p in sorted_parts
        ]

        s3.complete_multipart_upload(
            Bucket=ARTIFACT_BUCKET,
            Key=s3_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": multipart_parts},
        )

        log.info(json.dumps({
            "msg":         "multipart_upload_completed",
            "packageName": package_name,
            "version":     version,
            "parts":       len(sorted_parts),
            "actor":       caller,
        }))

        return _response(200, {
            "packageName": package_name,
            "version":     version,
            "status":      "PENDING",
            "message":     (
                "Multipart upload complete. "
                "artifact_processor will verify and promote to ACTIVE within seconds. "
                f"Poll GET /packages/{package_name}/{version} for status."
            ),
        })

    except s3.exceptions.NoSuchUpload:
        return _response(404, {"error": "Multipart upload not found — already completed or expired"})
    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
