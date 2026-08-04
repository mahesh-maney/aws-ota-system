"""
digilux_ota_user_check_updates
Returns available OTA updates for all controller devices owned by the calling user.

GET /api/v1/ota/my/updates

Auth: Cognito ID token (any authenticated user — NOT admin only).
The userId is extracted from the JWT `sub` claim.
Only devices registered under that userId are returned.
"""
import datetime
import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION          = os.environ["REGION"]
DEVICE_DATA_TABLE   = os.environ.get("DEVICE_DATA_TABLE",  "digilux_device_data")
INVENTORY_TABLE     = os.environ.get("INVENTORY_TABLE",     "digilux_device_inventory")
PACKAGES_TABLE      = os.environ.get("PACKAGES_TABLE",      "digilux_ota_packages")
DEVICE_DATA_USER_INDEX = os.environ.get("DEVICE_DATA_USER_INDEX", "userId-index")

dynamo = boto3.resource("dynamodb", region_name=REGION)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

class _Dec(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_Dec),
    }


def _version_tuple(v: str):
    """Parse '4.2.0' → (4, 2, 0). Falls back to string comparison on parse error."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (str(v),)


def _is_newer(candidate: str, installed: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(installed)


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


# ──────────────────────────────────────────────────────────────────────────────
# Core logic
# ──────────────────────────────────────────────────────────────────────────────

def _get_user_devices(user_id: str) -> list[dict]:
    """Query digilux_device_data by userId-index GSI. Returns list of device items."""
    tbl = dynamo.Table(DEVICE_DATA_TABLE)
    resp = tbl.query(
        IndexName=DEVICE_DATA_USER_INDEX,
        KeyConditionExpression=Key("userId").eq(user_id),
    )
    return resp.get("Items", [])


def _get_inventory(device_id: str) -> dict | None:
    """Fetch device inventory record (installedVersions, pendingJobId, etc.)."""
    tbl = dynamo.Table(INVENTORY_TABLE)
    return tbl.get_item(Key={"deviceId": device_id}).get("Item")


def _get_latest_active_version(package_name: str) -> dict | None:
    """
    Query digilux_ota_packages for all versions of packageName with status=ACTIVE,
    then return the item with the highest semver version, or None if none found.
    """
    tbl = dynamo.Table(PACKAGES_TABLE)
    resp = tbl.query(
        KeyConditionExpression=Key("packageName").eq(package_name),
        FilterExpression="#s = :active",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":active": "ACTIVE"},
    )
    items = resp.get("Items", [])
    if not items:
        return None
    return max(items, key=lambda i: _version_tuple(i.get("version", "0.0.0")))


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    try:
        # ── Auth: extract userId from Cognito JWT claims ─────────────────────
        claims  = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            log.warning("Missing sub claim in token")
            return _resp(401, {"error": "Unauthorized — invalid token"})

        email = claims.get("email", user_id)
        log.info(json.dumps({"msg": "check_updates_request", "userId": user_id, "email": email}))

        # ── Fetch devices owned by this user ─────────────────────────────────
        device_items = _get_user_devices(user_id)
        log.info(f"Found {len(device_items)} device(s) for userId={user_id}")

        if not device_items:
            _audit("USER_CHECK_UPDATES", user_id, {"userId": user_id}, "SUCCESS",
                   devicesFound=0, totalUpdates=0)
            return _resp(200, {"devices": [], "totalUpdates": 0})

        result_devices = []
        total_updates  = 0

        for dev in device_items:
            device_id = dev.get("deviceId")
            if not device_id:
                log.warning(f"Device record missing deviceId — skipping: {dev}")
                continue

            # ── Fetch OTA inventory for this device ──────────────────────────
            inv = _get_inventory(device_id)
            if not inv:
                log.info(f"Device {device_id} not yet in OTA inventory (agent not yet started)")
                result_devices.append({
                    "deviceId":          device_id,
                    "otaStatus":         "NOT_REGISTERED",
                    "message":           "OTA agent has not started on this device yet.",
                    "installedVersions": {},
                    "availableUpdates":  [],
                    "pendingJobId":      None,
                })
                continue

            installed_versions = inv.get("installedVersions") or {}
            pending_job_id     = inv.get("pendingJobId")
            available_updates  = []

            # ── Compare each installed package with latest ACTIVE version ─────
            for pkg_name, installed_ver in installed_versions.items():
                latest_pkg = _get_latest_active_version(pkg_name)
                if not latest_pkg:
                    log.debug(f"No ACTIVE package found for {pkg_name}")
                    continue

                latest_ver = latest_pkg.get("version", "")
                if not _is_newer(latest_ver, installed_ver):
                    log.debug(f"{pkg_name}: installed={installed_ver}, latest={latest_ver} — up to date")
                    continue

                # Newer version available
                artifact_size = latest_pkg.get("artifactSize", 0)
                if isinstance(artifact_size, Decimal):
                    artifact_size = int(artifact_size)

                available_updates.append({
                    "packageName":      pkg_name,
                    "packageType":      latest_pkg.get("packageType", ""),
                    "currentVersion":   installed_ver,
                    "availableVersion": latest_ver,
                    "releaseNotes":     latest_pkg.get("releaseNotes", ""),
                    "artifactSize":     artifact_size,
                })
                log.info(json.dumps({
                    "msg": "update_available",
                    "userId": user_id, "deviceId": device_id,
                    "packageName": pkg_name,
                    "currentVersion": installed_ver, "availableVersion": latest_ver,
                }))

            total_updates += len(available_updates)
            result_devices.append({
                "deviceId":          device_id,
                "otaStatus":         "REGISTERED",
                "model":             inv.get("model", ""),
                "hwRevision":        inv.get("hwRevision", ""),
                "installedVersions": dict(installed_versions),
                "pendingJobId":      pending_job_id,
                "availableUpdates":  available_updates,
                "updateCount":       len(available_updates),
            })

        _audit("USER_CHECK_UPDATES", user_id, {"userId": user_id}, "SUCCESS",
               devicesFound=len(result_devices), totalUpdates=total_updates)

        log.info(json.dumps({
            "msg": "check_updates_complete",
            "userId": user_id, "devices": len(result_devices), "totalUpdates": total_updates,
        }))

        return _resp(200, {"devices": result_devices, "totalUpdates": total_updates})

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        log.error(json.dumps({"msg": "aws_client_error", "code": code, "error": msg}))
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        log.exception(f"Unhandled error in user_check_updates: {e}")
        return _resp(500, {"error": "Internal server error"})
