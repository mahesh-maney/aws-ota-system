"""
digilux_ota_package_register
Admin-only. Registers a new package version into the OTA catalog.
Caller uploads artifact to S3 first, then calls this with the S3 key.
POST /api/v1/ota/packages
"""
import json
import os
import time
import hashlib
import base64

import boto3
from botocore.exceptions import ClientError

REGION = os.environ["REGION"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")
COMPAT_TABLE = os.environ.get("COMPAT_TABLE", "digilux_ota_compatibility")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "digilux-ota-signing-key")
COGNITO_POOL_ID = os.environ.get("COGNITO_POOL_ID", "ap-south-1_h1o8s7257")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)

VALID_TYPES = {
    "CONTROLLER_FIRMWARE",
    "CONTROLLER_APP",
    "DRIVER",
    "ZIGBEE_DEVICE",
    "CONFIG",
    "RULES",
}


def lambda_handler(event, context):
    try:
        # Admin group check
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        groups = claims.get("cognito:groups", "")
        if "admin" not in groups:
            return _response(403, {"error": "Admin access required"})

        body = json.loads(event.get("body") or "{}")
        caller = claims.get("email", claims.get("sub", "unknown"))

        # Validate required fields
        required = ["packageName", "version", "packageType", "s3Key"]
        for field in required:
            if not body.get(field):
                return _response(400, {"error": f"Missing required field: {field}"})

        pkg_name = body["packageName"].strip()
        version = body["version"].strip()
        pkg_type = body["packageType"].strip().upper()
        s3_key = body["s3Key"].strip()

        if pkg_type not in VALID_TYPES:
            return _response(400, {"error": f"Invalid packageType. Must be one of: {', '.join(VALID_TYPES)}"})

        # Verify the artifact exists in S3
        try:
            head = s3.head_object(Bucket=ARTIFACT_BUCKET, Key=s3_key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return _response(400, {"error": f"Artifact not found in S3: s3://{ARTIFACT_BUCKET}/{s3_key}"})
            raise

        artifact_size = head["ContentLength"]

        # Compute SHA256 of the artifact
        sha256 = _compute_s3_sha256(s3_key)

        # Sign the SHA256 hash with the OTA signing key
        signature = _sign_sha256(sha256)

        # Check for duplicate
        table = dynamo.Table(PACKAGES_TABLE)
        existing = table.get_item(Key={"packageName": pkg_name, "version": version})
        if "Item" in existing:
            return _response(409, {
                "error": f"Package {pkg_name}@{version} already exists. Use a new version number."
            })

        now_ms = int(time.time() * 1000)

        # Write to packages table
        item = {
            "packageName": pkg_name,
            "version": version,
            "packageType": pkg_type,
            "s3Key": s3_key,
            "s3Bucket": ARTIFACT_BUCKET,
            "sha256": sha256,
            "signature": signature,
            "artifactSize": artifact_size,
            "status": "ACTIVE",
            "releaseNotes": body.get("releaseNotes", ""),
            "createdAt": now_ms,
            "createdBy": caller,
        }
        table.put_item(Item=item)

        # Write compatibility record
        compat = {
            "packageName": pkg_name,
            "version": version,
            "compatibleModels": body.get("compatibleModels", []),
            "minHwRevision": body.get("minHwRevision", ""),
            "dependsOn": body.get("dependsOn", {}),
            "incompatibleWith": body.get("incompatibleWith", {}),
        }
        dynamo.Table(COMPAT_TABLE).put_item(Item=compat)

        return _response(201, {
            "packageName": pkg_name,
            "version": version,
            "packageType": pkg_type,
            "sha256": sha256,
            "artifactSize": artifact_size,
            "s3Uri": f"s3://{ARTIFACT_BUCKET}/{s3_key}",
            "registeredAt": now_ms,
            "registeredBy": caller,
        })

    except Exception as e:
        print(f"ERROR: {e}")
        return _response(500, {"error": "Internal server error"})


def _compute_s3_sha256(s3_key: str) -> str:
    obj = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=s3_key)
    sha256_hash = hashlib.sha256()
    for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 1024):
        sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _sign_sha256(sha256_hex: str) -> str:
    """Signs the SHA256 hex string using ECDSA P-256 stored in Secrets Manager."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    secret = sm.get_secret_value(SecretId=SIGNING_SECRET)
    key_data = json.loads(secret["SecretString"])
    private_key = serialization.load_pem_private_key(
        key_data["privateKey"].encode(), password=None
    )
    signature_bytes = private_key.sign(
        sha256_hex.encode(),
        ec.ECDSA(hashes.SHA256())
    )
    return base64.b64encode(signature_bytes).decode()


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
