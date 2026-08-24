"""
digilux_ota_upload_url
Step 1 of the upload flow. Returns either a single presigned PUT URL (small files)
or a multipart upload session with per-chunk presigned URLs (large files).

POST /api/v1/ota/packages/upload-artefact   — initiate upload
GET  /api/v1/ota/packages                   — list packages
Admin-only.

Request body (POST):
{
  "deviceType":   "Network_controller_zigbee_firmware",   # required
  "version":      "4.2.2",                                # required
  "releaseType":  "UAT" | "PROD",                        # required
  "checksum":     "a1b2c3d4...",                          # required SHA256 hex — verified on upload
  "totalSize":    10485760,                               # optional bytes — triggers multipart if > 10MB
  "fileName":     "custom-name.tar",                      # optional override
  "releaseNotes": "Bug fixes"                             # optional
}

Upload modes:
  SINGLE    — totalSize not provided or <= 10MB
              Response includes uploadUrl (PUT directly to S3)
  MULTIPART — totalSize > 10MB
              Response includes uploadId + chunkUrls (one per 10MB chunk)
              Call POST /upload-artefact/complete after all chunks are PUT

Security:
  - uploadToken (UUID) stored in S3 object metadata (x-amz-meta-upload-token).
  - artifact_processor verifies token + checksum before promoting to ACTIVE.

Packages start as PENDING. Poll GET /packages/{packageName}/{version} for status.
After ACTIVE, call PATCH /packages/{packageName}/{version}/activate to publish.
"""
import datetime
import json
import logging
import math
import os
import time
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
UPLOAD_EXPIRY   = int(os.environ.get("UPLOAD_EXPIRY_SEC", "3600"))  # 1 hour for large uploads

CHUNK_SIZE           = 10 * 1024 * 1024   # 10 MB per chunk
MULTIPART_THRESHOLD  = 10 * 1024 * 1024   # use multipart when totalSize > 10 MB

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
    "Network_controller_zigbee_stack_firmware": {
        "baseName": "ZigbeeStackFirmware",
        "ext":      ".bin",
    },
}

VALID_RELEASE_TYPES = {"UAT", "PROD", "BETA", "CUSTOM"}

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
        if "ota-admin" not in claims.get("cognito:groups", ""):
            log.warning("Unauthorized — not in admin group")
            return _response(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))

        if method == "GET":
            return _list_packages(event, caller)

        body = json.loads(event.get("body") or "{}")

        # ── Validate required fields ──────────────────────────────────────────
        for field in ["deviceType", "version", "releaseType", "checksum"]:
            if not body.get(field):
                return _response(400, {"error": f"Missing required field: {field}"})

        device_type  = body["deviceType"].strip()
        version      = body["version"].strip()
        release_type = body["releaseType"].strip().upper()
        checksum     = body["checksum"].strip().lower()
        total_size   = body.get("totalSize") or body.get("totalsize") or None  # bytes, optional
        file_name_override = (body.get("fileName") or "").strip() or None

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
        # fileName: use caller-provided name if given, otherwise auto-derive
        file_name    = file_name_override or f"{package_name}-{version}{type_cfg['ext']}"
        s3_key       = f"Network_controller_firmware/{device_type}/{version}/{file_name}"

        log.info(json.dumps({
            "msg":             "upload_url_request",
            "deviceType":      device_type,
            "packageName":     package_name,
            "version":         version,
            "releaseType":     release_type,
            "fileName":        file_name,
            "fileNameOverride": file_name_override is not None,
            "checksumProvided": checksum is not None,
            "totalSize":       total_size,
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

        # ── Generate upload token — stored in S3 metadata, verified by artifact_processor ─
        upload_token = str(uuid.uuid4())
        use_multipart = total_size is not None and int(total_size) > MULTIPART_THRESHOLD

        # ── Write PENDING record ──────────────────────────────────────────────
        now_ms = int(time.time() * 1000)
        item = {
            "packageName":      package_name,
            "version":          version,
            "deviceType":       device_type,
            "releaseType":      release_type,
            "fileName":         file_name,
            "s3Key":            s3_key,
            "s3Bucket":         ARTIFACT_BUCKET,
            "status":           "PENDING",
            "activated":        False,
            "uploadToken":      upload_token,
            "uploadType":       "MULTIPART" if use_multipart else "SINGLE",
            "expectedChecksum": checksum,
            "releaseNotes":     body.get("releaseNotes", ""),
            "createdAt":        now_ms,
            "createdBy":        caller,
        }
        if total_size is not None:
            item["totalSize"] = int(total_size)

        if use_multipart:
            return _handle_multipart(
                item, table, caller, package_name, version,
                device_type, release_type, s3_key, file_name,
                upload_token, int(total_size), checksum,
            )
        else:
            return _handle_single(
                item, table, caller, package_name, version,
                device_type, release_type, s3_key, file_name,
                upload_token, total_size, checksum,
            )

    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _handle_single(item, table, caller, package_name, version,
                   device_type, release_type, s3_key, file_name,
                   upload_token, total_size, checksum):
    """Single presigned PUT URL — for files <= 10 MB or when totalSize is not provided."""
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket":      ARTIFACT_BUCKET,
            "Key":         s3_key,
            "ContentType": "application/octet-stream",
            "Metadata":    {"upload-token": upload_token},
        },
        ExpiresIn=UPLOAD_EXPIRY,
        HttpMethod="PUT",
    )

    table.put_item(Item=item)

    _audit("PACKAGE_UPLOAD_URL_REQUESTED", caller,
           {"packageName": package_name, "version": version},
           "SUCCESS", uploadType="SINGLE",
           deviceType=device_type, releaseType=release_type,
           s3Key=s3_key, checksum=checksum[:16] + "...")

    resp = {
        "uploadType":   "SINGLE",
        "uploadUrl":    upload_url,
        "uploadToken":  upload_token,
        "s3Key":        s3_key,
        "fileName":     file_name,
        "expiresIn":    UPLOAD_EXPIRY,
        "packageName":  package_name,
        "version":      version,
        "deviceType":   device_type,
        "releaseType":  release_type,
        "status":       "PENDING",
        "activated":    False,
        "instructions": (
            "PUT binary to uploadUrl with headers: "
            "Content-Type: application/octet-stream AND "
            "x-amz-meta-upload-token: <uploadToken>. "
            "Then poll GET /packages/{packageName}/{version} until status=ACTIVE."
        ),
    }
    if total_size is not None:
        resp["totalSize"] = int(total_size)
    return _response(200, resp)


def _handle_multipart(item, table, caller, package_name, version,
                      device_type, release_type, s3_key, file_name,
                      upload_token, total_size, checksum):
    """S3 multipart upload — for files > 10 MB. Returns per-chunk presigned URLs."""
    # Initiate multipart upload — token stored in S3 metadata, verified by artifact_processor
    mp = s3.create_multipart_upload(
        Bucket=ARTIFACT_BUCKET,
        Key=s3_key,
        ContentType="application/octet-stream",
        Metadata={"upload-token": upload_token},
    )
    upload_id   = mp["UploadId"]
    num_parts   = math.ceil(total_size / CHUNK_SIZE)

    # Generate a presigned URL for each part
    chunk_urls = []
    for part_number in range(1, num_parts + 1):
        url = s3.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket":     ARTIFACT_BUCKET,
                "Key":        s3_key,
                "UploadId":   upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=UPLOAD_EXPIRY,
        )
        chunk_urls.append({"partNumber": part_number, "url": url})

    # Persist uploadId so complete endpoint can finish the upload
    item["uploadId"] = upload_id
    table.put_item(Item=item)

    _audit("PACKAGE_UPLOAD_URL_REQUESTED", caller,
           {"packageName": package_name, "version": version},
           "SUCCESS", uploadType="MULTIPART",
           deviceType=device_type, releaseType=release_type,
           s3Key=s3_key, parts=num_parts, checksum=checksum[:16] + "...")

    return _response(200, {
        "uploadType":  "MULTIPART",
        "uploadId":    upload_id,
        "packageName": package_name,
        "version":     version,
        "deviceType":  device_type,
        "releaseType": release_type,
        "fileName":    file_name,
        "s3Key":       s3_key,
        "status":      "PENDING",
        "activated":   False,
        "chunkSize":   CHUNK_SIZE,
        "totalChunks": num_parts,
        "totalSize":   total_size,
        "expiresIn":   UPLOAD_EXPIRY,
        "chunkUrls":   chunk_urls,
        "instructions": (
            f"PUT each chunk to its chunkUrl (no auth headers needed). "
            f"Collect the ETag from each response. "
            f"Then POST /packages/upload-artefact/complete with uploadId + parts array. "
            f"Poll GET /packages/{package_name}/{version} until status=ACTIVE."
        ),
    })


def _list_packages(event: dict, caller: str) -> dict:
    """GET /api/v1/ota/packages — list packages, optionally filtered."""
    from boto3.dynamodb.conditions import Attr
    params        = event.get("queryStringParameters") or {}
    status_f      = params.get("status", "ACTIVE").upper()
    device_type_f = params.get("deviceType")
    release_f     = (params.get("releaseType") or "").upper() or None

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
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
