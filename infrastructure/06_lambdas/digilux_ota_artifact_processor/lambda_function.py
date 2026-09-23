"""
digilux_ota_artifact_processor
Step 2 of the upload flow — triggered automatically by S3 on object creation.

Security validations (in order):
  1. Upload token — reads x-amz-meta-upload-token from S3 object metadata,
     compares against uploadToken stored in DynamoDB. Mismatch = rogue upload.
  2. Checksum     — if admin provided expectedChecksum at upload-artefact time,
     compares computed SHA256 against it. Mismatch = corrupted/tampered binary.

On any validation failure:
  - S3 object is deleted immediately
  - DynamoDB record is marked CORRUPTED with reason
  - Audit log written

On success: computes SHA256, signs with ECDSA, promotes PENDING → ACTIVE.

Triggered by: S3 Event Notification (s3:ObjectCreated:Put)
              on bucket: digilux-ota-artifacts
              prefixes: Network_controller_firmware/ (covers ALL new uploads — new key structure)
                        Legacy prefixes: Network_controller_zigbee_firmware/,
                        Network_controller_Z2M_Firmware/, Network_controller_Miscellaneous/

S3 key structures:
  New (2026-08-24+): Network_controller_firmware/{deviceType}/{version}/{fileName}
  Old (legacy):      {deviceType}/{packageName}/{version}/{fileName}
"""
import base64
import datetime
import hashlib
import json
import logging
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION            = os.environ["REGION"]
PACKAGES_TABLE    = os.environ.get("PACKAGES_TABLE",    "digilux_ota_packages")
ARTIFACT_BUCKET   = os.environ.get("ARTIFACT_BUCKET",   "digilux-ota-artifacts")
SIGNING_SECRET    = os.environ.get("SIGNING_SECRET",    "digilux-ota-signing-key")
MASTER_ENC_SECRET = os.environ.get("MASTER_ENC_SECRET", "digilux-ota-master-encryption-key")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)
sm     = boto3.client("secretsmanager", region_name=REGION)

SKIP_KEYS = {".keep"}

DEVICE_TYPE_TO_PACKAGE = {
    "Network_controller_firmware":              "HomeAssistantUtility",
    "Network_controller_zigbee_firmware":       "ZigbeeFirmware",
    "Network_controller_Z2M_Firmware":          "Z2MFirmware",
    "Network_controller_Miscellaneous":         "NetControllerMisc",
    "Network_controller_zigbee_stack_firmware": "ZigbeeStackFirmware",
}

ACTOR = "artifact_processor"


# ──────────────────────────────────────────────────────────────────────────────
# Audit + structured logging helpers
# ──────────────────────────────────────────────────────────────────────────────

def _audit(event: str, resource: dict, result: str, **extra) -> None:
    """Write a structured audit record to stdout (captured by CloudWatch Logs)."""
    print(json.dumps({
        "audit":    True,
        "event":    event,
        "ts":       datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor":    ACTOR,
        "resource": resource,
        "result":   result,
        **extra,
    }))


def _log(level: str, msg: str, **fields) -> None:
    """Emit a structured JSON log line at the given level."""
    record = {"msg": msg, **fields}
    getattr(log, level)(json.dumps(record, default=str))


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    record_count = len(event.get("Records", []))
    _log("info", "s3_event_received",
         recordCount=record_count,
         requestId=context.aws_request_id if context else None)

    for record in event.get("Records", []):
        bucket   = record["s3"]["bucket"]["name"]
        s3_key   = record["s3"]["object"]["key"]
        obj_size = record["s3"]["object"].get("size", 0)

        _log("info", "processing_s3_record",
             bucket=bucket, s3Key=s3_key, sizeBytes=obj_size)

        if any(s3_key.endswith(suffix) for suffix in SKIP_KEYS):
            _log("debug", "skipping_placeholder_key", s3Key=s3_key)
            continue

        try:
            _process_artifact(bucket, s3_key, obj_size)
        except Exception as e:
            _log("error", "unhandled_exception",
                 bucket=bucket, s3Key=s3_key, error=str(e), exc=type(e).__name__)
            log.exception(f"Unhandled exception processing s3://{bucket}/{s3_key}")
            _audit("PROCESSING_FAILED",
                   {"s3Key": s3_key, "bucket": bucket},
                   "FAILURE",
                   error=str(e), excType=type(e).__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Quarantine
# ──────────────────────────────────────────────────────────────────────────────

def _quarantine(bucket: str, s3_key: str, pkg_name: str, version: str,
                reason: str, detail: str) -> None:
    """Delete the rogue S3 object and mark the DynamoDB record as CORRUPTED."""
    _log("warning", "quarantine_triggered",
         packageName=pkg_name, version=version,
         reason=reason, detail=detail, s3Key=s3_key, bucket=bucket)

    try:
        s3.delete_object(Bucket=bucket, Key=s3_key)
        _log("info", "quarantine_s3_deleted",
             packageName=pkg_name, version=version, s3Key=s3_key)
    except ClientError as e:
        _log("error", "quarantine_s3_delete_failed",
             packageName=pkg_name, version=version, s3Key=s3_key,
             awsError=e.response["Error"]["Code"],
             awsMessage=e.response["Error"]["Message"])

    try:
        dynamo.Table(PACKAGES_TABLE).update_item(
            Key={"packageName": pkg_name, "version": version},
            UpdateExpression="SET #st = :corrupted, corruptReason = :reason, processedAt = :ts",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":corrupted": "CORRUPTED",
                ":reason":    detail,
                ":ts":        int(time.time() * 1000),
            },
            ConditionExpression="attribute_exists(packageName)",
        )
        _log("info", "quarantine_dynamodb_marked_corrupted",
             packageName=pkg_name, version=version, reason=reason)
    except ClientError as e:
        _log("error", "quarantine_dynamodb_update_failed",
             packageName=pkg_name, version=version,
             awsError=e.response["Error"]["Code"],
             awsMessage=e.response["Error"]["Message"])

    _audit(f"PACKAGE_{reason}",
           {"packageName": pkg_name, "version": version},
           "FAILURE",
           s3Key=s3_key, bucket=bucket, detail=detail)


# ──────────────────────────────────────────────────────────────────────────────
# Core processing pipeline
# ──────────────────────────────────────────────────────────────────────────────

def _process_artifact(bucket: str, s3_key: str, obj_size: int) -> None:
    pipeline_start = time.monotonic()

    # ── Parse S3 key → packageName + version ──────────────────────────────────
    parts = s3_key.split("/")
    if len(parts) < 4:
        _log("error", "invalid_s3_key_structure",
             s3Key=s3_key, partCount=len(parts),
             expected="at least 4 slash-separated parts")
        _audit("INVALID_S3_KEY",
               {"s3Key": s3_key}, "FAILURE",
               detail=f"Expected ≥4 parts, got {len(parts)}")
        return

    if parts[0] == "Network_controller_firmware" and parts[1] in DEVICE_TYPE_TO_PACKAGE:
        device_type = parts[1]
        version     = parts[2]
        pkg_name    = DEVICE_TYPE_TO_PACKAGE[device_type]
        key_structure = "new"
        _log("info", "s3_key_parsed_new_structure",
             deviceType=device_type, packageName=pkg_name, version=version,
             fileName=parts[3] if len(parts) > 3 else "")
    else:
        device_type   = parts[0]
        pkg_name      = parts[1]
        version       = parts[2]
        key_structure = "legacy"
        _log("info", "s3_key_parsed_legacy_structure",
             deviceType=device_type, packageName=pkg_name, version=version,
             fileName=parts[3] if len(parts) > 3 else "")

    resource = {"packageName": pkg_name, "version": version}

    # ── DynamoDB record lookup ─────────────────────────────────────────────────
    table = dynamo.Table(PACKAGES_TABLE)
    item  = table.get_item(Key={"packageName": pkg_name, "version": version}).get("Item")

    if not item:
        _log("warning", "orphan_s3_object_no_dynamo_record",
             packageName=pkg_name, version=version, s3Key=s3_key,
             detail="No matching DynamoDB record — deleting orphan object")
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
            _log("info", "orphan_s3_object_deleted",
                 packageName=pkg_name, version=version, s3Key=s3_key)
        except ClientError as e:
            _log("error", "orphan_s3_delete_failed",
                 packageName=pkg_name, version=version, s3Key=s3_key,
                 awsError=e.response["Error"]["Code"])
        _audit("PACKAGE_ORPHAN_DELETED", resource, "FAILURE",
               s3Key=s3_key, detail="No DynamoDB record matched this S3 object")
        return

    current_status = item.get("status", "UNKNOWN")
    _log("debug", "dynamo_record_found",
         packageName=pkg_name, version=version,
         currentStatus=current_status,
         releaseType=item.get("releaseType"),
         uploadedBy=item.get("uploadedBy"),
         createdAt=item.get("createdAt"))

    if current_status == "ACTIVE":
        _log("info", "skipping_already_active",
             packageName=pkg_name, version=version,
             detail="Duplicate S3 event — package already ACTIVE, nothing to do")
        _audit("PACKAGE_DUPLICATE_ACTIVE", resource, "SKIPPED",
               s3Key=s3_key, detail="Package already in ACTIVE state")
        return

    if current_status == "CORRUPTED":
        _log("warning", "skipping_already_corrupted",
             packageName=pkg_name, version=version,
             corruptReason=item.get("corruptReason"),
             detail="Package is CORRUPTED — deleting new upload attempt")
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
        except ClientError as e:
            _log("error", "corrupted_reattempt_delete_failed",
                 packageName=pkg_name, version=version,
                 awsError=e.response["Error"]["Code"])
        _audit("PACKAGE_CORRUPTED_REATTEMPT", resource, "SKIPPED",
               s3Key=s3_key, corruptReason=item.get("corruptReason"),
               detail="Upload attempt on already-CORRUPTED package — rejected")
        return

    _log("info", "starting_processing_pipeline",
         packageName=pkg_name, version=version,
         releaseType=item.get("releaseType"), sizeBytes=obj_size,
         keyStructure=key_structure)

    # ── 1. Upload token verification ───────────────────────────────────────────
    stored_token = item.get("uploadToken")
    if not stored_token:
        _log("warning", "upload_token_absent",
             packageName=pkg_name, version=version,
             detail="No uploadToken in DynamoDB record — token verification skipped. "
                    "This is lower-assurance. Verify the upload URL flow is enforcing tokens.")
        _audit("UPLOAD_TOKEN_ABSENT", resource, "WARN",
               s3Key=s3_key,
               detail="Package record has no uploadToken — cannot verify upload origin")
    else:
        _log("debug", "upload_token_verification_start",
             packageName=pkg_name, version=version,
             storedTokenPrefix=stored_token[:8] + "...")
        try:
            head     = s3.head_object(Bucket=bucket, Key=s3_key)
            s3_token = head.get("Metadata", {}).get("upload-token", "")
            _log("debug", "s3_object_metadata_fetched",
                 packageName=pkg_name, version=version,
                 contentType=head.get("ContentType"),
                 contentLength=head.get("ContentLength"),
                 tokenPresent=bool(s3_token))
        except ClientError as e:
            _log("error", "head_object_failed",
                 packageName=pkg_name, version=version, s3Key=s3_key,
                 awsError=e.response["Error"]["Code"],
                 awsMessage=e.response["Error"]["Message"])
            s3_token = ""

        if s3_token != stored_token:
            _log("error", "upload_token_mismatch",
                 packageName=pkg_name, version=version, s3Key=s3_key,
                 s3TokenPrefix=s3_token[:8] + "..." if s3_token else "(empty)",
                 storedTokenPrefix=stored_token[:8] + "...",
                 detail="Tokens differ — possible rogue/unauthorized upload attempt")
            _quarantine(bucket, s3_key, pkg_name, version,
                        "UPLOAD_TOKEN_MISMATCH",
                        "Token in S3 metadata does not match issued token. Possible unauthorized upload.")
            return

        _log("info", "upload_token_verified",
             packageName=pkg_name, version=version,
             detail="S3 metadata token matches DynamoDB token — upload origin confirmed")

    # ── 2. Download artifact + compute SHA256 ─────────────────────────────────
    _log("info", "download_start",
         packageName=pkg_name, version=version,
         s3Key=s3_key, expectedBytes=obj_size)
    t_dl = time.monotonic()
    raw_bytes = _download_artifact(bucket, s3_key)
    dl_ms     = int((time.monotonic() - t_dl) * 1000)
    actual_size = len(raw_bytes)

    if actual_size != obj_size and obj_size > 0:
        _log("warning", "download_size_mismatch",
             packageName=pkg_name, version=version,
             expectedBytes=obj_size, actualBytes=actual_size,
             detail="S3 event size differs from downloaded bytes — S3 event may be stale")

    t_hash = time.monotonic()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    hash_ms = int((time.monotonic() - t_hash) * 1000)

    _log("info", "sha256_computed",
         packageName=pkg_name, version=version,
         sha256=sha256, sizeBytes=actual_size,
         downloadMs=dl_ms, hashMs=hash_ms)

    # ── 3. Checksum validation ─────────────────────────────────────────────────
    expected_checksum = item.get("expectedChecksum")
    if not expected_checksum:
        _log("warning", "checksum_not_provided",
             packageName=pkg_name, version=version,
             detail="Admin did not supply expectedChecksum at upload time — "
                    "integrity check skipped. Recommend always supplying checksum.")
        _audit("CHECKSUM_SKIPPED", resource, "WARN",
               sha256=sha256, sizeBytes=actual_size,
               detail="No expectedChecksum in record — cannot verify admin-provided integrity")
    else:
        if sha256.lower() != expected_checksum.lower():
            _log("error", "checksum_mismatch",
                 packageName=pkg_name, version=version,
                 computedSha256=sha256,
                 expectedSha256=expected_checksum,
                 computedPrefix=sha256[:16],
                 expectedPrefix=expected_checksum[:16],
                 detail="SHA256 of downloaded file does not match admin-provided checksum")
            _quarantine(bucket, s3_key, pkg_name, version,
                        "CHECKSUM_MISMATCH",
                        f"SHA256 mismatch: computed {sha256[:16]}... expected {expected_checksum[:16]}...")
            return

        _log("info", "checksum_verified",
             packageName=pkg_name, version=version,
             sha256=sha256,
             detail="Computed SHA256 matches admin-provided checksum — artifact integrity confirmed")

    # ── 4. Validate tar + enrich manifest.json with per-file SHA256 + size ──────
    _log("info", "tar_validation_start",
         packageName=pkg_name, version=version, sizeBytes=actual_size)
    result = _enrich_tar_manifest(raw_bytes, pkg_name, version, bucket, s3_key)
    if result is None:
        return  # _quarantine already called inside _enrich_tar_manifest
    enriched_bytes, manifest = result

    # Recompute SHA256 over the enriched tar (manifest.json now has per-file checksums)
    sha256 = hashlib.sha256(enriched_bytes).hexdigest()
    actual_size = len(enriched_bytes)
    _log("info", "enriched_tar_sha256",
         packageName=pkg_name, version=version,
         sha256=sha256, enrichedBytes=actual_size)

    # ── 5. ECDSA sign (4-field input: version|size|packageName|sha256) ───────
    signing_input = f"{version}|{actual_size}|{pkg_name}|{sha256}"
    _log("info", "signing_start",
         packageName=pkg_name, version=version,
         signingSecret=SIGNING_SECRET,
         algorithm="ECDSA-SHA256",
         signingFields="version|size|packageName|sha256",
         signingInput=f"{version}|{actual_size}|{pkg_name}|{sha256[:16]}...")
    t_sign = time.monotonic()
    signature = _sign(signing_input)
    sign_ms   = int((time.monotonic() - t_sign) * 1000)
    _log("info", "signing_complete",
         packageName=pkg_name, version=version,
         signatureLength=len(signature), signMs=sign_ms,
         sigPrefix=signature[:16] + "...")
    _audit("ARTIFACT_SIGNED", resource, "SUCCESS",
           algorithm="ECDSA-SHA256",
           signingFields="version|size|packageName|sha256",
           sha256=sha256, artifactSize=actual_size,
           signatureLength=len(signature), signMs=sign_ms)

    # ── 6. AES-256-GCM encrypt enriched tar ───────────────────────────────────
    _log("info", "encryption_start",
         packageName=pkg_name, version=version,
         algorithm="AES-256-GCM", plaintextBytes=actual_size)
    t_enc = time.monotonic()
    aes_key_bytes, aes_iv_bytes, encrypted_bytes = _encrypt_artifact(enriched_bytes)
    enc_ms        = int((time.monotonic() - t_enc) * 1000)
    encrypted_size = len(encrypted_bytes)
    _log("info", "encryption_complete",
         packageName=pkg_name, version=version,
         plaintextBytes=actual_size, ciphertextBytes=encrypted_size,
         overheadBytes=encrypted_size - actual_size, encMs=enc_ms)
    _audit("ARTIFACT_ENCRYPTED", resource, "SUCCESS",
           algorithm="AES-256-GCM",
           plaintextBytes=actual_size, ciphertextBytes=encrypted_size,
           encMs=enc_ms)

    # ── 7. Double-encrypt AES key with master key ──────────────────────────────
    _log("info", "key_wrapping_start",
         packageName=pkg_name, version=version,
         masterKeySecret=MASTER_ENC_SECRET,
         detail="Wrapping artifact AES key with master key (defense in depth)")
    t_wrap = time.monotonic()
    aes_key_enc_b64, master_iv_b64 = _double_encrypt_aes_key(aes_key_bytes)
    aes_iv_b64 = base64.b64encode(aes_iv_bytes).decode()
    wrap_ms = int((time.monotonic() - t_wrap) * 1000)
    _log("info", "key_wrapping_complete",
         packageName=pkg_name, version=version, wrapMs=wrap_ms)

    # ── 8. Upload encrypted artifact + signature file to S3 ───────────────────
    enc_key = _enc_s3_key(s3_key)
    sig_key = _sig_s3_key(s3_key)

    _log("info", "s3_upload_start",
         packageName=pkg_name, version=version,
         encKey=enc_key, sigKey=sig_key,
         encryptedBytes=encrypted_size)
    t_up = time.monotonic()
    s3.put_object(Bucket=bucket, Key=enc_key, Body=encrypted_bytes,
                  ContentType="application/octet-stream")
    s3.put_object(Bucket=bucket, Key=sig_key, Body=signature.encode(),
                  ContentType="text/plain")
    up_ms = int((time.monotonic() - t_up) * 1000)
    _log("info", "s3_upload_complete",
         packageName=pkg_name, version=version,
         encKey=enc_key, sigKey=sig_key,
         encryptedBytes=encrypted_size, uploadMs=up_ms)
    _audit("ARTIFACT_S3_UPLOADED", resource, "SUCCESS",
           encKey=enc_key, sigKey=sig_key,
           encryptedBytes=encrypted_size, uploadMs=up_ms)

    # ── 9. Delete raw tar from S3 (only encrypted copy remains) ───────────────
    _log("info", "raw_artifact_delete_start",
         packageName=pkg_name, version=version,
         s3Key=s3_key,
         detail="Removing plaintext tar — only AES-encrypted copy will remain")
    try:
        s3.delete_object(Bucket=bucket, Key=s3_key)
        _log("info", "raw_artifact_deleted",
             packageName=pkg_name, version=version, s3Key=s3_key)
        _audit("RAW_ARTIFACT_DELETED", resource, "SUCCESS",
               s3Key=s3_key, replacedBy=enc_key)
    except ClientError as e:
        _log("error", "raw_artifact_delete_failed",
             packageName=pkg_name, version=version, s3Key=s3_key,
             awsError=e.response["Error"]["Code"],
             awsMessage=e.response["Error"]["Message"],
             detail="Raw tar not deleted — plaintext artifact remains in S3. Manual cleanup required.")
        _audit("RAW_ARTIFACT_DELETE_FAILED", resource, "WARN",
               s3Key=s3_key, awsError=e.response["Error"]["Code"],
               detail="Plaintext tar could not be deleted — manual cleanup required")

    # ── 10. Promote PENDING → ACTIVE in DynamoDB ──────────────────────────────
    now_ms = int(time.time() * 1000)
    _log("info", "dynamo_promote_start",
         packageName=pkg_name, version=version,
         fromStatus="PENDING", toStatus="ACTIVE",
         encKey=enc_key, sigKey=sig_key)
    try:
        table.update_item(
            Key={"packageName": pkg_name, "version": version},
            UpdateExpression=(
                "SET #st = :active, sha256 = :h, signature = :sig, "
                "artifactSize = :sz, processedAt = :ts, "
                "encS3Key = :encKey, sigS3Key = :sigKey, "
                "aesKeyEnc = :keyEnc, aesIv = :iv, masterIv = :miv "
                "REMOVE uploadToken, expectedChecksum"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":active": "ACTIVE",
                ":h":      sha256,
                ":sig":    signature,
                ":sz":     actual_size,  # enriched tar size (plaintext, post-manifest-injection)
                ":ts":     now_ms,
                ":encKey": enc_key,
                ":sigKey": sig_key,
                ":keyEnc": aes_key_enc_b64,
                ":iv":     aes_iv_b64,
                ":miv":    master_iv_b64,
            },
            ConditionExpression="attribute_exists(packageName)",
        )
    except ClientError as e:
        _log("error", "dynamo_promote_failed",
             packageName=pkg_name, version=version,
             awsError=e.response["Error"]["Code"],
             awsMessage=e.response["Error"]["Message"],
             detail="CRITICAL: artifact uploaded to S3 but DynamoDB not updated. "
                    "Package will remain PENDING. Re-trigger S3 event or update manually.")
        _audit("PACKAGE_PROMOTE_FAILED", resource, "FAILURE",
               awsError=e.response["Error"]["Code"],
               encKey=enc_key, sigKey=sig_key,
               detail="Encrypted artifact in S3 but DynamoDB promote failed — manual fix required")
        return

    pipeline_ms = int((time.monotonic() - pipeline_start) * 1000)
    _log("info", "package_activated",
         packageName=pkg_name, version=version,
         releaseType=item.get("releaseType"),
         sizeBytes=actual_size, encryptedBytes=encrypted_size,
         sha256=sha256, encKey=enc_key,
         tokenVerified=stored_token is not None,
         checksumVerified=expected_checksum is not None,
         pipelineMs=pipeline_ms)

    _audit("PACKAGE_REGISTERED_ACTIVE", resource, "SUCCESS",
           releaseType=item.get("releaseType"),
           sizeBytes=actual_size, encryptedBytes=encrypted_size,
           sha256=sha256, encKey=enc_key, sigKey=sig_key,
           signatureLength=len(signature),
           tokenVerified=stored_token is not None,
           checksumVerified=expected_checksum is not None,
           pipelineMs=pipeline_ms,
           uploadedBy=item.get("uploadedBy", "unknown"))

    # ── 11. Supersede older ACTIVE versions of same package + releaseType ──────
    release_type = item.get("releaseType")
    if release_type:
        _log("debug", "supersede_check_start",
             packageName=pkg_name, version=version, releaseType=release_type)
        _supersede_previous_versions(table, pkg_name, version, release_type, now_ms)
    else:
        _log("debug", "supersede_skipped",
             packageName=pkg_name, version=version,
             detail="No releaseType on package record — supersede step skipped")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _semver_tuple(version: str) -> tuple:
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _supersede_previous_versions(table, pkg_name: str, current_version: str,
                                  release_type: str, now_ms: int) -> None:
    """Mark older ACTIVE versions of the same package+releaseType as SUPERSEDED."""
    from boto3.dynamodb.conditions import Key, Attr

    result = table.query(
        KeyConditionExpression=Key("packageName").eq(pkg_name),
        FilterExpression=Attr("status").eq("ACTIVE") & Attr("releaseType").eq(release_type),
    )
    candidates = result.get("Items", [])
    _log("debug", "supersede_candidates_found",
         packageName=pkg_name, currentVersion=current_version,
         releaseType=release_type, candidateCount=len(candidates),
         candidateVersions=[i["version"] for i in candidates])

    current_semver = _semver_tuple(current_version)
    superseded = []

    for old_item in candidates:
        old_ver = old_item["version"]
        if old_ver == current_version:
            _log("debug", "supersede_skip_self",
                 packageName=pkg_name, version=old_ver)
            continue
        if _semver_tuple(old_ver) >= current_semver:
            _log("info", "supersede_skip_newer_or_equal",
                 packageName=pkg_name, version=old_ver,
                 currentVersion=current_version,
                 detail="Candidate semver >= current — not an older release, skipping")
            continue
        try:
            table.update_item(
                Key={"packageName": pkg_name, "version": old_ver},
                UpdateExpression="SET #st = :sup, supersededAt = :ts, supersededBy = :v",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":sup": "SUPERSEDED",
                    ":ts":  now_ms,
                    ":v":   current_version,
                },
                ConditionExpression=Attr("status").eq("ACTIVE"),
            )
            superseded.append(old_ver)
            _log("info", "version_superseded",
                 packageName=pkg_name, oldVersion=old_ver,
                 newVersion=current_version, releaseType=release_type)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                _log("debug", "supersede_condition_failed",
                     packageName=pkg_name, version=old_ver,
                     detail="Version already not ACTIVE — skipping (race condition or already handled)")
            else:
                _log("error", "supersede_update_failed",
                     packageName=pkg_name, version=old_ver,
                     awsError=e.response["Error"]["Code"],
                     awsMessage=e.response["Error"]["Message"])

    if superseded:
        _log("info", "versions_superseded_summary",
             packageName=pkg_name, currentVersion=current_version,
             releaseType=release_type, supersededCount=len(superseded),
             supersededVersions=superseded)
        _audit("VERSIONS_SUPERSEDED",
               {"packageName": pkg_name, "version": current_version},
               "SUCCESS",
               releaseType=release_type,
               supersededVersions=superseded, supersededCount=len(superseded))
    else:
        _log("debug", "no_versions_superseded",
             packageName=pkg_name, currentVersion=current_version,
             releaseType=release_type,
             detail="No older ACTIVE versions found to supersede")


def _download_artifact(bucket: str, s3_key: str) -> bytes:
    """Stream-download the full S3 object into memory."""
    _log("debug", "s3_get_object_start", bucket=bucket, s3Key=s3_key)
    obj = s3.get_object(Bucket=bucket, Key=s3_key)
    data = obj["Body"].read()
    _log("debug", "s3_get_object_complete",
         bucket=bucket, s3Key=s3_key, bytesRead=len(data))
    return data


def _enc_s3_key(s3_key: str) -> str:
    # Opaque UUID — leaks no package name, version, or device type
    return f"enc/{uuid.uuid4()}.enc"


def _sig_s3_key(s3_key: str) -> str:
    return f"sig/{uuid.uuid4()}.sig"


VALID_FILE_TYPES = {1, 2, 3, 4, 5, 6, 7, 8}


def _enrich_tar_manifest(raw_bytes: bytes, pkg_name: str, version: str,
                          bucket: str, s3_key: str) -> tuple | None:
    """
    Validate the tar, enrich manifest.json with per-file SHA256 + size,
    and repack the tar with the enriched manifest.

    Returns (enriched_tar_bytes, manifest) on success, or None on failure
    (_quarantine is called internally before returning None).
    """
    import io
    import tarfile

    _log("debug", "tar_open_start",
         packageName=pkg_name, version=version, sizeBytes=len(raw_bytes))
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tf:
            names = tf.getnames()
            _log("debug", "tar_contents",
                 packageName=pkg_name, version=version,
                 fileCount=len(names), files=names)

            # ── manifest.json must exist ──────────────────────────────────────
            if "manifest.json" not in names:
                _log("error", "tar_missing_manifest",
                     packageName=pkg_name, version=version, tarFiles=names)
                _quarantine(bucket, s3_key, pkg_name, version,
                            "INVALID_TAR_STRUCTURE",
                            "manifest.json not found inside tar archive")
                return None

            f        = tf.extractfile("manifest.json")
            manifest = json.loads(f.read().decode("utf-8"))

            _log("debug", "manifest_parsed",
                 packageName=pkg_name, version=version,
                 manifestPackage=manifest.get("packageName"),
                 manifestVersion=manifest.get("version"),
                 manifestFileCount=len(manifest.get("files", [])))

            # ── required top-level fields ─────────────────────────────────────
            for field in ("packageName", "version", "files"):
                if field not in manifest:
                    _log("error", "manifest_missing_required_field",
                         packageName=pkg_name, version=version,
                         missingField=field, presentFields=list(manifest.keys()))
                    _quarantine(bucket, s3_key, pkg_name, version,
                                "INVALID_MANIFEST",
                                f"Required field '{field}' missing from manifest.json")
                    return None

            # ── cross-check packageName / version ─────────────────────────────
            if manifest.get("packageName") != pkg_name:
                _log("warning", "manifest_package_name_mismatch",
                     packageName=pkg_name, version=version,
                     manifestPackage=manifest.get("packageName"))
            if manifest.get("version") != version:
                _log("warning", "manifest_version_mismatch",
                     packageName=pkg_name, version=version,
                     manifestVersion=manifest.get("version"))

            # ── per-file validation + SHA256/size injection ───────────────────
            enriched_files = []
            for entry in manifest["files"]:
                if not isinstance(entry, dict):
                    _log("error", "manifest_file_entry_not_object",
                         packageName=pkg_name, version=version, entry=str(entry),
                         detail="Each files[] entry must be a JSON object with at least 'name' and 'type'")
                    _quarantine(bucket, s3_key, pkg_name, version,
                                "INVALID_MANIFEST",
                                f"files[] entry is not a JSON object: {entry!r}")
                    return None

                fname = entry.get("name")
                ftype = entry.get("type")

                if not fname:
                    _log("error", "manifest_file_entry_missing_name",
                         packageName=pkg_name, version=version, entry=entry)
                    _quarantine(bucket, s3_key, pkg_name, version,
                                "INVALID_MANIFEST",
                                "A files[] entry is missing the 'name' field")
                    return None

                if fname not in names:
                    _log("error", "manifest_file_missing_from_tar",
                         packageName=pkg_name, version=version,
                         fileName=fname, tarFiles=names)
                    _quarantine(bucket, s3_key, pkg_name, version,
                                "MANIFEST_FILE_MISSING",
                                f"File '{fname}' listed in manifest.json not found in tar")
                    return None

                if ftype not in VALID_FILE_TYPES:
                    _log("warning", "manifest_unknown_file_type",
                         packageName=pkg_name, version=version,
                         fileName=fname, fileType=ftype,
                         knownTypes=sorted(VALID_FILE_TYPES),
                         detail="Unknown type — file will be included but controller will skip it")

                file_bytes = tf.extractfile(fname).read()
                file_sha256 = hashlib.sha256(file_bytes).hexdigest()
                file_size   = len(file_bytes)

                enriched_entry = dict(entry)
                enriched_entry["sha256"] = file_sha256
                enriched_entry["size"]   = file_size
                enriched_files.append(enriched_entry)

                _log("debug", "manifest_file_enriched",
                     packageName=pkg_name, version=version,
                     fileName=fname, fileType=ftype,
                     sha256=file_sha256, sizeBytes=file_size)

            manifest["files"] = enriched_files
            _log("info", "manifest_enrichment_complete",
                 packageName=pkg_name, version=version,
                 fileCount=len(enriched_files))

            # ── repack tar with enriched manifest.json ────────────────────────
            enriched_manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
            out_buf = io.BytesIO()
            with tarfile.open(fileobj=out_buf, mode="w:gz") as out_tf:
                # Write all original files except manifest.json
                for member in tf.getmembers():
                    if member.name == "manifest.json":
                        continue
                    file_obj = tf.extractfile(member)
                    if file_obj is not None:
                        out_tf.addfile(member, file_obj)

                # Write enriched manifest.json
                minfo = tarfile.TarInfo(name="manifest.json")
                minfo.size = len(enriched_manifest_bytes)
                out_tf.addfile(minfo, io.BytesIO(enriched_manifest_bytes))

            enriched_tar_bytes = out_buf.getvalue()
            _log("info", "tar_repacked",
                 packageName=pkg_name, version=version,
                 originalBytes=len(raw_bytes),
                 enrichedBytes=len(enriched_tar_bytes))
            _audit("MANIFEST_ENRICHED",
                   {"packageName": pkg_name, "version": version},
                   "SUCCESS",
                   fileCount=len(enriched_files),
                   originalBytes=len(raw_bytes),
                   enrichedBytes=len(enriched_tar_bytes),
                   files=[{"name": f["name"], "type": f.get("type"), "sha256": f["sha256"], "size": f["size"]}
                          for f in enriched_files])

            return enriched_tar_bytes, manifest

    except tarfile.TarError as e:
        _log("error", "tar_open_failed",
             packageName=pkg_name, version=version,
             error=str(e), detail="Not a valid tar archive")
        _quarantine(bucket, s3_key, pkg_name, version,
                    "INVALID_TAR", f"Not a valid tar archive: {e}")
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _log("error", "manifest_json_parse_failed",
             packageName=pkg_name, version=version,
             error=str(e), detail="manifest.json is not valid JSON or not UTF-8")
        _quarantine(bucket, s3_key, pkg_name, version,
                    "INVALID_MANIFEST_JSON", f"manifest.json parse error: {e}")
        return None


def _encrypt_artifact(raw_bytes: bytes) -> tuple:
    """AES-256-GCM encrypt raw_bytes. Returns (key_bytes, iv_bytes, ciphertext)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _log("debug", "aes_encrypt_start",
         plaintextBytes=len(raw_bytes), keyBits=256, mode="GCM")
    aes_key   = os.urandom(32)   # 256-bit key, unique per artifact
    aes_iv    = os.urandom(12)   # 96-bit GCM nonce
    aesgcm    = AESGCM(aes_key)
    encrypted = aesgcm.encrypt(aes_iv, raw_bytes, None)
    _log("debug", "aes_encrypt_complete",
         plaintextBytes=len(raw_bytes), ciphertextBytes=len(encrypted),
         gcmTagBytes=16)
    return aes_key, aes_iv, encrypted


def _double_encrypt_aes_key(aes_key: bytes) -> tuple:
    """Wrap aes_key with the master AES key from Secrets Manager."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _log("debug", "key_wrap_start",
         masterKeySecret=MASTER_ENC_SECRET,
         aesKeyBytes=len(aes_key), mode="AES-256-GCM")
    secret     = sm.get_secret_value(SecretId=MASTER_ENC_SECRET)
    master_key = base64.b64decode(json.loads(secret["SecretString"])["key"])
    master_iv  = os.urandom(12)
    aesgcm     = AESGCM(master_key)
    enc        = aesgcm.encrypt(master_iv, aes_key, None)
    _log("debug", "key_wrap_complete",
         wrappedKeyBytes=len(enc))
    return base64.b64encode(enc).decode(), base64.b64encode(master_iv).decode()


def _sign(sha256_hex: str) -> str:
    """ECDSA-sign the sha256 hex string using the private key from Secrets Manager."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    _log("debug", "ecdsa_sign_start",
         signingSecret=SIGNING_SECRET, algorithm="ECDSA", hash="SHA256",
         inputLength=len(sha256_hex))
    secret   = sm.get_secret_value(SecretId=SIGNING_SECRET)
    key_data = json.loads(secret["SecretString"])
    priv_key = serialization.load_pem_private_key(
        key_data["privateKey"].encode(), password=None
    )
    sig_bytes = priv_key.sign(sha256_hex.encode(), ec.ECDSA(hashes.SHA256()))
    _log("debug", "ecdsa_sign_complete",
         signatureBytes=len(sig_bytes),
         signatureB64Length=len(base64.b64encode(sig_bytes)))
    return base64.b64encode(sig_bytes).decode()
