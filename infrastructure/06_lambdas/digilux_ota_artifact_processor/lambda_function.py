"""
digilux_ota_artifact_processor
Step 2 of the upload flow — triggered automatically by S3 on object creation.
Computes SHA256, signs with ECDSA, promotes package status PENDING → ACTIVE.
No admin action required after the binary is uploaded.

Triggered by: S3 Event Notification (s3:ObjectCreated:Put)
              on bucket: digilux-ota-artifacts
              prefixes: firmware/, application/, drivers/, zigbee-devices/, config/, rules/
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
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")
ARTIFACT_BUCKET= os.environ.get("ARTIFACT_BUCKET","digilux-ota-artifacts")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "digilux-ota-signing-key")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)
sm     = boto3.client("secretsmanager", region_name=REGION)

# Ignore S3 key suffixes that are not real artifacts (folder placeholders)
SKIP_KEYS = {".keep"}


def _audit(event: str, actor: str, resource: dict, result: str, **extra) -> None:
    print(json.dumps({
        "audit": True,
        "event": event,
        "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor": actor,
        "resource": resource,
        "result": result,
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
            # Do not raise — allow other records to process


def _process_artifact(bucket: str, s3_key: str, obj_size: int) -> None:
    # Extract packageName and version from key path:
    # {prefix}/{packageName}/{version}/{filename}
    parts = s3_key.split("/")
    if len(parts) < 4:
        log.error(f"Unexpected S3 key structure (expected prefix/name/version/file): {s3_key}")
        return

    pkg_name = parts[1]
    version  = parts[2]
    log.info(f"Parsed from S3 key — packageName={pkg_name}, version={version}")

    table = dynamo.Table(PACKAGES_TABLE)

    log.debug(f"Looking up PENDING record for {pkg_name}@{version}")
    item = table.get_item(Key={"packageName": pkg_name, "version": version}).get("Item")
    if not item:
        log.warning(f"No PENDING record found for {pkg_name}@{version} — skipping (possible stale S3 event)")
        return

    if item.get("status") == "ACTIVE":
        log.info(f"{pkg_name}@{version} is already ACTIVE — skipping duplicate S3 event")
        return

    log.info(f"Computing SHA256 for s3://{bucket}/{s3_key} ({obj_size} bytes)...")
    t0 = time.monotonic()
    sha256 = _compute_sha256(bucket, s3_key)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps({
        "msg": "sha256_computed",
        "packageName": pkg_name, "version": version,
        "sha256": sha256, "sizeBytes": obj_size, "elapsedMs": elapsed_ms,
    }))

    log.info(f"Signing SHA256 with ECDSA key from Secrets Manager ({SIGNING_SECRET})")
    signature = _sign(sha256)
    log.info(f"ECDSA signature generated, length={len(signature)} chars")

    now_ms = int(time.time() * 1000)
    log.info(f"Promoting {pkg_name}@{version} PENDING → ACTIVE in DynamoDB")
    table.update_item(
        Key={"packageName": pkg_name, "version": version},
        UpdateExpression=(
            "SET #st = :active, sha256 = :h, signature = :sig, "
            "artifactSize = :sz, processedAt = :ts"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":active": "ACTIVE",
            ":h":      sha256,
            ":sig":    signature,
            ":sz":     obj_size,
            ":ts":     now_ms,
        },
        ConditionExpression="attribute_exists(packageName)",
    )

    _audit("PACKAGE_REGISTERED_ACTIVE", "s3-event-processor",
           {"packageName": pkg_name, "version": version},
           "SUCCESS",
           s3Key=s3_key, sizeBytes=obj_size, sha256=sha256,
           sigLength=len(signature))

    print(json.dumps({
        "msg": "package_activated",
        "packageName": pkg_name, "version": version,
        "sizeBytes": obj_size, "sha256": sha256,
    }))


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
