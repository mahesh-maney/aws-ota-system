"""
digilux_ota_compatibility_check
Returns available updates for a specific controller.
GET /api/v1/controllers/{deviceId}/updates/available
Admin-only.
"""
import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

REGION = os.environ["REGION"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")
INVENTORY_TABLE = os.environ.get("INVENTORY_TABLE", "digilux_device_inventory")
COMPAT_TABLE = os.environ.get("COMPAT_TABLE", "digilux_ota_compatibility")

dynamo = boto3.resource("dynamodb", region_name=REGION)


def lambda_handler(event, context):
    try:
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "admin" not in claims.get("cognito:groups", ""):
            return _response(403, {"error": "Admin access required"})

        device_id = (event.get("pathParameters") or {}).get("deviceId")
        if not device_id:
            return _response(400, {"error": "Missing deviceId in path"})

        # Get device's current installed versions from inventory
        inventory_table = dynamo.Table(INVENTORY_TABLE)
        inv = inventory_table.get_item(Key={"deviceId": device_id})
        if "Item" not in inv:
            return _response(404, {
                "error": "Device not found in OTA inventory. "
                         "The OTA agent may not have started on this device yet."
            })

        device = inv["Item"]
        installed = device.get("installedVersions", {})
        model = device.get("model", "")
        hw_rev = device.get("hwRevision", "")
        thing_name = device.get("thingName", "")

        # Check for active job on this device
        pending_job = device.get("pendingJobId")

        packages_table = dynamo.Table(PACKAGES_TABLE)
        compat_table = dynamo.Table(COMPAT_TABLE)

        # Scan active packages
        result = packages_table.scan(
            FilterExpression="#s = :active",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":active": "ACTIVE"},
        )
        all_packages = result.get("Items", [])

        # Group by packageName, keep latest compatible version
        latest_by_name: dict[str, dict] = {}
        for pkg in all_packages:
            name = pkg["packageName"]
            ver = pkg["version"]
            if name not in latest_by_name:
                latest_by_name[name] = pkg
            else:
                if _version_gt(ver, latest_by_name[name]["version"]):
                    latest_by_name[name] = pkg

        available_updates = []
        for pkg_name, pkg in latest_by_name.items():
            latest_ver = pkg["version"]
            current_ver = installed.get(pkg_name)

            # Skip if already at latest
            if current_ver and not _version_gt(latest_ver, current_ver):
                continue

            # Compatibility check
            compat = compat_table.get_item(
                Key={"packageName": pkg_name, "version": latest_ver}
            ).get("Item", {})

            compatible_models = compat.get("compatibleModels", [])
            min_hw = compat.get("minHwRevision", "")

            if compatible_models and model and model not in compatible_models:
                continue
            if min_hw and hw_rev and not _version_gte(hw_rev, min_hw):
                continue

            available_updates.append({
                "packageName": pkg_name,
                "packageType": pkg["packageType"],
                "currentVersion": current_ver,
                "availableVersion": latest_ver,
                "artifactSize": pkg.get("artifactSize", 0),
                "releaseNotes": pkg.get("releaseNotes", ""),
            })

        return _response(200, {
            "deviceId": device_id,
            "thingName": thing_name,
            "model": model,
            "hwRevision": hw_rev,
            "pendingJobId": pending_job,
            "installedVersions": installed,
            "availableUpdates": available_updates,
            "updateCount": len(available_updates),
        })

    except Exception as e:
        print(f"ERROR: {e}")
        return _response(500, {"error": "Internal server error"})


def _version_gt(v1: str, v2: str) -> bool:
    """Returns True if v1 > v2 using semver-style comparison."""
    try:
        return tuple(int(x) for x in v1.split(".")) > tuple(int(x) for x in v2.split("."))
    except (ValueError, AttributeError):
        return v1 > v2


def _version_gte(v1: str, v2: str) -> bool:
    try:
        return tuple(int(x) for x in v1.split(".")) >= tuple(int(x) for x in v2.split("."))
    except (ValueError, AttributeError):
        return v1 >= v2


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }
