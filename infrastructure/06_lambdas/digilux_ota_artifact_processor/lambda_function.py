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

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION         = os.environ["REGION"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
ARTIFACT_BUCKET= os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
SIGNING_SECRET    = os.environ.get("SIGNING_SECRET",    "digilux-ota-signing-key")
MASTER_ENC_SECRET = os.environ.get("MASTER_ENC_SECRET", "digilux-ota-master-encryption-key")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)
sm     = boto3.client("secretsmanager", region_name=REGION)

SKIP_KEYS = {".keep"}

# Used by the new S3 key structure to map deviceType → packageName
# New structure: Network_controller_firmware/{deviceType}/{version}/{fileName}
DEVICE_TYPE_TO_PACKAGE = {
    "Network_controller_firmware":              "HomeAssistantUtility",
    "Network_controller_zigbee_firmware":       "ZigbeeFirmware",
    "Network_controller_Z2M_Firmware":          "Z2MFirmware",
    "Network_controller_Miscellaneous":         "NetControllerMisc",
    "Network_controller_zigbee_stack_firmware": "ZigbeeStackFirmware",
}


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
    record_count = len(event.get("Records", []))
    log.info(json.dumps({"msg": "s3_event_received", "recordCount": record_count}))

    for record in event.get("Records", []):
        bucket   = record["s3"]["bucket"]["name"]
        s3_key   = record["s3"]["object"]["key"]
        obj_size = record["s3"]["object"].get("size", 0)

        log.info(json.dumps({
            "msg": "processing_s3_record",
            "bucket": bucket, "s3Key": s3_key, "sizeBytes": obj_size,
        }))

        if any(s3_key.endswith(suffix) for suffix in SKIP_KEYS):
            log.info(f"Skipping placeholder key: {s3_key}")
            continue

        try:
            _process_artifact(bucket, s3_key, obj_size)
        except Exception as e:
            log.exception(f"ERROR processing s3://{bucket}/{s3_key}: {e}")


def _quarantine(bucket: str, s3_key: str, pkg_name: str, version: str,
                reason: str, detail: str) -> None:
    """Delete the rogue S3 object and mark the DynamoDB record as CORRUPTED."""
    # Delete the object
    try:
        s3.delete_object(Bucket=bucket, Key=s3_key)
        log.warning(f"Deleted rogue object s3://{bucket}/{s3_key} — reason: {reason}")
    except ClientError as e:
        log.error(f"Failed to delete rogue object: {e}")

    # Mark DynamoDB record CORRUPTED
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
    except ClientError as e:
        log.error(f"Failed to mark record CORRUPTED: {e}")

    _audit(f"PACKAGE_{reason}", "s3-event-processor",
           {"packageName": pkg_name, "version": version},
           "FAILURE", s3Key=s3_key, detail=detail)


def _process_artifact(bucket: str, s3_key: str, obj_size: int) -> None:
    # Parse packageName and version from S3 key.
    #
    # New structure (from 2026-08-24 onwards):
    #   Network_controller_firmware/{deviceType}/{version}/{fileName}
    #   parts[0]="Network_controller_firmware", parts[1]=deviceType, parts[2]=version
    #
    # Old structure (pre-2026-08-24):
    #   {deviceType}/{packageName}/{version}/{fileName}
    #   parts[0]=deviceType, parts[1]=packageName, parts[2]=version
    parts = s3_key.split("/")
    if len(parts) < 4:
        log.error(f"Unexpected S3 key structure: {s3_key}")
        return

    if parts[0] == "Network_controller_firmware" and parts[1] in DEVICE_TYPE_TO_PACKAGE:
        # New key structure
        device_type = parts[1]
        version     = parts[2]
        pkg_name    = DEVICE_TYPE_TO_PACKAGE[device_type]
        log.info(f"New key structure — deviceType={device_type}, packageName={pkg_name}, version={version}")
    else:
        # Old key structure: {deviceType}/{packageName}/{version}/{fileName}
        pkg_name = parts[1]
        version  = parts[2]
        log.info(f"Old key structure — packageName={pkg_name}, version={version}")

    log.info(f"Parsed from S3 key — packageName={pkg_name}, version={version}")

    table = dynamo.Table(PACKAGES_TABLE)
    item  = table.get_item(Key={"packageName": pkg_name, "version": version}).get("Item")
    if not item:
        log.warning(f"No record found for {pkg_name}@{version} — deleting orphan S3 object")
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
        except ClientError:
            pass
        return

    if item.get("status") == "ACTIVE":
        log.info(f"{pkg_name}@{version} already ACTIVE — skipping duplicate S3 event")
        return

    if item.get("status") == "CORRUPTED":
        log.warning(f"{pkg_name}@{version} already CORRUPTED — deleting new upload attempt")
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
        except ClientError:
            pass
        return

    # ── 1. Upload token verification ──────────────────────────────────────────
    # The presigned URL requires x-amz-meta-upload-token as a signed header.
    # S3 stores it as object metadata. We verify it matches the token we issued.
    stored_token = item.get("uploadToken")
    if stored_token:
        try:
            head = s3.head_object(Bucket=bucket, Key=s3_key)
            s3_token = head.get("Metadata", {}).get("upload-token", "")
        except ClientError as e:
            log.error(f"head_object failed: {e}")
            s3_token = ""

        if s3_token != stored_token:
            log.error(f"Upload token mismatch for {pkg_name}@{version} — rogue upload detected")
            _quarantine(bucket, s3_key, pkg_name, version,
                        "UPLOAD_TOKEN_MISMATCH",
                        f"Expected token not present in S3 metadata. Possible unauthorized upload.")
            return

        log.info(f"Upload token verified for {pkg_name}@{version}")

    # ── 2. Download artifact + compute SHA256 ────────────────────────────────
    log.info(f"Downloading artifact s3://{bucket}/{s3_key} ({obj_size} bytes)...")
    t0        = time.monotonic()
    raw_bytes = _download_artifact(bucket, s3_key)
    sha256    = hashlib.sha256(raw_bytes).hexdigest()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(json.dumps({
        "msg": "sha256_computed",
        "packageName": pkg_name, "version": version,
        "sha256": sha256, "sizeBytes": obj_size, "elapsedMs": elapsed_ms,
    }))

    # ── 3. Checksum validation ────────────────────────────────────────────────
    expected_checksum = item.get("expectedChecksum")
    if expected_checksum:
        if sha256.lower() != expected_checksum.lower():
            log.error(
                f"Checksum mismatch for {pkg_name}@{version}: "
                f"computed={sha256} expected={expected_checksum}"
            )
            _quarantine(bucket, s3_key, pkg_name, version,
                        "CHECKSUM_MISMATCH",
                        f"SHA256 mismatch: computed {sha256[:16]}... expected {expected_checksum[:16]}...")
            return

        log.info(f"Checksum verified for {pkg_name}@{version} — SHA256 matches")

    # ── 4. ECDSA sign ─────────────────────────────────────────────────────────
    log.info(f"Signing SHA256 with ECDSA key from Secrets Manager ({SIGNING_SECRET})")
    signature = _sign(sha256)
    log.info(f"ECDSA signature generated, length={len(signature)} chars")

    # ── 5. Validate tar structure + manifest.json ─────────────────────────────
    log.info(f"Validating tar structure for {pkg_name}@{version}")
    manifest = _validate_tar_manifest(raw_bytes, pkg_name, version, bucket, s3_key)
    if manifest is None:
        return  # _quarantine already called inside

    # ── 6. AES-256-GCM encrypt entire tar ────────────────────────────────────
    log.info(f"Encrypting artifact for {pkg_name}@{version}")
    aes_key_bytes, aes_iv_bytes, encrypted_bytes = _encrypt_artifact(raw_bytes)

    # ── 7. Upload encrypted artifact + signature file to S3 ──────────────────
    enc_key = _enc_s3_key(s3_key)
    sig_key = _sig_s3_key(s3_key)
    s3.put_object(Bucket=bucket, Key=enc_key, Body=encrypted_bytes,
                  ContentType="application/octet-stream")
    s3.put_object(Bucket=bucket, Key=sig_key, Body=signature.encode(),
                  ContentType="text/plain")
    log.info(f"Uploaded encrypted artifact → s3://{bucket}/{enc_key}")
    log.info(f"Uploaded signature file     → s3://{bucket}/{sig_key}")

    # ── 8. Double-encrypt AES key with master key (defense in depth) ─────────
    aes_key_enc_b64, master_iv_b64 = _double_encrypt_aes_key(aes_key_bytes)
    aes_iv_b64 = base64.b64encode(aes_iv_bytes).decode()

    # ── 9. Delete raw tar from S3 (only encrypted copy remains) ──────────────
    s3.delete_object(Bucket=bucket, Key=s3_key)
    log.info(f"Deleted raw tar s3://{bucket}/{s3_key}")

    # ── 10. Promote PENDING → ACTIVE ──────────────────────────────────────────
    now_ms = int(time.time() * 1000)
    log.info(f"Promoting {pkg_name}@{version} PENDING → ACTIVE")
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
            ":sz":     obj_size,
            ":ts":     now_ms,
            ":encKey": enc_key,
            ":sigKey": sig_key,
            ":keyEnc": aes_key_enc_b64,
            ":iv":     aes_iv_b64,
            ":miv":    master_iv_b64,
        },
        ConditionExpression="attribute_exists(packageName)",
    )

    _audit("PACKAGE_REGISTERED_ACTIVE", "s3-event-processor",
           {"packageName": pkg_name, "version": version},
           "SUCCESS",
           s3Key=enc_key, sizeBytes=obj_size, sha256=sha256,
           sigLength=len(signature),
           tokenVerified=stored_token is not None,
           checksumVerified=expected_checksum is not None,
           encrypted=True)

    log.info(json.dumps({
        "msg": "package_activated",
        "packageName": pkg_name, "version": version,
        "sizeBytes": obj_size, "sha256": sha256, "encrypted": True,
    }))

    # ── 6. Supersede previous ACTIVE versions for same packageName + releaseType ──
    # CUSTOM release type is isolated — it never supersedes nor gets superseded by other types.
    release_type = item.get("releaseType")
    if release_type:
        _supersede_previous_versions(table, pkg_name, version, release_type, now_ms)


def _semver_tuple(version: str) -> tuple:
    """Convert version string like '2.1.3' to (2, 1, 3) for numeric comparison."""
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _supersede_previous_versions(table, pkg_name: str, current_version: str,
                                  release_type: str, now_ms: int) -> None:
    """Mark ACTIVE versions of the same package+releaseType as SUPERSEDED.

    Only supersedes versions with a LOWER semver than current_version.
    This prevents an accidentally uploaded older version from wiping a newer live release.
    """
    from boto3.dynamodb.conditions import Key, Attr
    result = table.query(
        KeyConditionExpression=Key("packageName").eq(pkg_name),
        FilterExpression=Attr("status").eq("ACTIVE") & Attr("releaseType").eq(release_type),
    )
    current_semver = _semver_tuple(current_version)
    superseded = []
    for old_item in result.get("Items", []):
        if old_item["version"] == current_version:
            continue
        if _semver_tuple(old_item["version"]) >= current_semver:
            log.info(
                f"Skipping supersede of {pkg_name}@{old_item['version']} "
                f"— its semver >= {current_version} (not an older release)"
            )
            continue
        try:
            table.update_item(
                Key={"packageName": pkg_name, "version": old_item["version"]},
                UpdateExpression="SET #st = :sup, supersededAt = :ts, supersededBy = :v",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":sup": "SUPERSEDED",
                    ":ts":  now_ms,
                    ":v":   current_version,
                },
                ConditionExpression=Attr("status").eq("ACTIVE"),
            )
            superseded.append(old_item["version"])
            log.info(f"Superseded {pkg_name}@{old_item['version']} (releaseType={release_type}) → replaced by {current_version}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                log.debug(f"Version {old_item['version']} already not ACTIVE — skipping")
            else:
                log.error(f"Failed to supersede {pkg_name}@{old_item['version']}: {e}")
    if superseded:
        log.info(json.dumps({
            "msg": "versions_superseded",
            "packageName": pkg_name, "releaseType": release_type,
            "newVersion": current_version, "superseded": superseded,
        }))


def _download_artifact(bucket: str, s3_key: str) -> bytes:
    """Download the full S3 object into memory."""
    obj = s3.get_object(Bucket=bucket, Key=s3_key)
    return obj["Body"].read()


def _enc_s3_key(s3_key: str) -> str:
    return (s3_key[:-4] if s3_key.endswith(".tar") else s3_key) + ".enc"


def _sig_s3_key(s3_key: str) -> str:
    return (s3_key[:-4] if s3_key.endswith(".tar") else s3_key) + ".sig"


def _validate_tar_manifest(raw_bytes: bytes, pkg_name: str, version: str,
                            bucket: str, s3_key: str) -> dict | None:
    """Confirm raw_bytes is a valid tar containing manifest.json with required fields."""
    import io
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tf:
            names = tf.getnames()
            if "manifest.json" not in names:
                _quarantine(bucket, s3_key, pkg_name, version,
                            "INVALID_TAR_STRUCTURE",
                            "manifest.json not found inside tar archive")
                return None
            f        = tf.extractfile("manifest.json")
            manifest = json.loads(f.read().decode("utf-8"))
            for field in ("packageName", "version", "files"):
                if field not in manifest:
                    _quarantine(bucket, s3_key, pkg_name, version,
                                "INVALID_MANIFEST",
                                f"Required field '{field}' missing from manifest.json")
                    return None
            log.info(json.dumps({
                "msg":             "manifest_validated",
                "packageName":     pkg_name,
                "version":         version,
                "manifestPackage": manifest.get("packageName"),
                "manifestVersion": manifest.get("version"),
                "fileCount":       len(manifest.get("files", [])),
            }))
            return manifest
    except tarfile.TarError as e:
        _quarantine(bucket, s3_key, pkg_name, version,
                    "INVALID_TAR", f"Not a valid tar archive: {e}")
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _quarantine(bucket, s3_key, pkg_name, version,
                    "INVALID_MANIFEST_JSON", f"manifest.json parse error: {e}")
        return None


def _encrypt_artifact(raw_bytes: bytes) -> tuple:
    """AES-256-GCM encrypt raw_bytes. Returns (key_bytes, iv_bytes, ciphertext)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes_key  = os.urandom(32)   # 256-bit key, unique per artifact
    aes_iv   = os.urandom(12)   # 96-bit GCM nonce
    aesgcm   = AESGCM(aes_key)
    encrypted = aesgcm.encrypt(aes_iv, raw_bytes, None)
    return aes_key, aes_iv, encrypted


def _double_encrypt_aes_key(aes_key: bytes) -> tuple:
    """Wrap aes_key with the master AES key from Secrets Manager.
    Returns (enc_b64, master_iv_b64) — both base64-encoded."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    secret     = sm.get_secret_value(SecretId=MASTER_ENC_SECRET)
    master_key = base64.b64decode(json.loads(secret["SecretString"])["key"])
    master_iv  = os.urandom(12)
    aesgcm     = AESGCM(master_key)
    enc        = aesgcm.encrypt(master_iv, aes_key, None)
    return base64.b64encode(enc).decode(), base64.b64encode(master_iv).decode()


def _compute_sha256(bucket: str, key: str) -> str:
    obj = s3.get_object(Bucket=bucket, Key=key)
    sha256_hash = hashlib.sha256()
    for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 1024):
        sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _sign(sha256_hex: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    secret   = sm.get_secret_value(SecretId=SIGNING_SECRET)
    key_data = json.loads(secret["SecretString"])
    priv_key = serialization.load_pem_private_key(
        key_data["privateKey"].encode(), password=None
    )
    sig_bytes = priv_key.sign(sha256_hex.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig_bytes).decode()
