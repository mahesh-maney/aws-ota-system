"""
digilux_ota_user_check_updates
Returns available OTA updates for all controller devices owned by the calling user.

GET /api/v1/ota/my/updates

Auth: Cognito ID token (any authenticated user — NOT admin only).
The userId is extracted from the JWT `sub` claim.
Only devices registered under that userId are returned.

OTA state (installedVersions, pendingJobId, thingName, model, hwRevision) is stored
directly on digilux_device_data items — no separate inventory table lookup needed.
"""
import datetime
import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION                 = os.environ["REGION"]
DEVICE_DATA_TABLE      = os.environ.get("DEVICE_DATA_TABLE",      "digilux_device_data")
PACKAGES_TABLE         = os.environ.get("PACKAGES_TABLE",         "digilux_ota_packages")
DEVICE_DATA_USER_INDEX = os.environ.get("DEVICE_DATA_USER_INDEX", "userId-index")
CANARY_GROUP           = os.environ.get("CANARY_GROUP",           "DGX-Canary")
ENTITLEMENT_FUNCTION   = os.environ.get("ENTITLEMENT_FUNCTION",   "digilux_entitlement_check")

dynamo         = boto3.resource("dynamodb", region_name=REGION)
iot            = boto3.client("iot",        region_name=REGION)
lambda_client  = boto3.client("lambda",     region_name=REGION)


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
    """Parse '4.2.0' → (4, 2, 0). Non-numeric segments use their numeric prefix (e.g. '28188-rc1' → 28188)."""
    result = []
    for part in str(v).split("."):
        try:
            result.append(int(part.split("-")[0]))
        except (ValueError, AttributeError):
            result.append(0)
    return tuple(result) if result else (0,)


def _is_newer(candidate: str, installed: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(installed)


def _invoke_entitlement_check(controller_id: str, firmware_category: str,
                               tier_override: str | None) -> dict:
    """
    Invoke digilux_entitlement_check (internal Lambda-to-Lambda).
    Returns the entitlement response dict.
    Fails open on any error — existing OTA behaviour is never blocked by
    an entitlement service outage.
    """
    if not controller_id or not firmware_category:
        # Nothing to check — permissive default
        return {"eligible": True, "reason": "missing_input"}

    payload = json.dumps({
        "controllerId":     controller_id,
        "firmwareCategory": firmware_category,
        "tierOverride":     tier_override,
    }).encode()

    try:
        resp = lambda_client.invoke(
            FunctionName   = ENTITLEMENT_FUNCTION,
            InvocationType = "RequestResponse",
            Payload        = payload,
        )
        result = json.loads(resp["Payload"].read())
        # Lambda invocation errors surface as FunctionError key
        if resp.get("FunctionError"):
            log.warning(json.dumps({
                "msg":              "entitlement_function_error",
                "controllerId":     controller_id,
                "firmwareCategory": firmware_category,
                "functionError":    resp["FunctionError"],
            }))
            return {"eligible": True, "reason": "entitlement_function_error"}
        return result
    except Exception as exc:
        log.warning(json.dumps({
            "msg":              "entitlement_invoke_failed",
            "controllerId":     controller_id,
            "firmwareCategory": firmware_category,
            "error":            str(exc),
        }))
        # Fail open — never block legitimate OTA delivery
        return {"eligible": True, "reason": "entitlement_invoke_failed"}


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


def _is_beta_device(thing_name: str) -> bool:
    """Return True if the device's IoT thing is in the canary group."""
    if not thing_name:
        return False
    try:
        resp   = iot.list_thing_groups_for_thing(thingName=thing_name)
        groups = [g.get("groupName", "") for g in resp.get("thingGroups", [])]
        return CANARY_GROUP in groups
    except Exception:
        log.warning(f"Could not check canary group for thing={thing_name} — defaulting to non-beta")
        return False


def _get_latest_available_version(package_name: str, include_beta: bool) -> dict | None:
    """
    Query packages for packageName where:
      - status = ACTIVE  (binary has been processed by artifact_processor)
      - activated = True (admin has explicitly published this version)
      - releaseType = PROD always; also UAT if device is in the canary group
    Returns the highest semver version that matches, or None.
    """
    tbl = dynamo.Table(PACKAGES_TABLE)

    filter_expr = (
        Attr("status").eq("ACTIVE") &
        Attr("activated").eq(True)
    )
    if not include_beta:
        filter_expr = filter_expr & Attr("releaseType").eq("PROD")

    resp  = tbl.query(
        KeyConditionExpression=Key("packageName").eq(package_name),
        FilterExpression=filter_expr,
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

            # ── OTA fields come directly from device_data item ────────────────
            installed_versions = dev.get("installedVersions") or {}
            pending_job_id     = dev.get("pendingJobId")

            if not installed_versions:
                log.info(f"Device {device_id} has no installedVersions (OTA agent not yet started)")
                result_devices.append({
                    "deviceId":          device_id,
                    "otaStatus":         "NOT_REGISTERED",
                    "message":           "OTA agent has not started on this device yet.",
                    "installedVersions": {},
                    "availableUpdates":  [],
                    "pendingJobId":      None,
                })
                continue

            # ── Determine if this device sees UAT packages ───────────────────
            thing_name   = dev.get("thingName")
            include_beta = _is_beta_device(thing_name)
            log.info(json.dumps({
                "msg": "device_beta_status",
                "deviceId": device_id, "thingName": thing_name, "includeBeta": include_beta,
            }))

            available_updates = []

            # ── Compare each installed package with latest available version ──
            for pkg_name, installed_ver in installed_versions.items():
                latest_pkg = _get_latest_available_version(pkg_name, include_beta)
                if not latest_pkg:
                    log.debug(f"No available package found for {pkg_name}")
                    continue

                latest_ver = latest_pkg.get("version", "")
                if not _is_newer(latest_ver, installed_ver):
                    log.debug(f"{pkg_name}: installed={installed_ver}, latest={latest_ver} — up to date")
                    continue

                # ── Entitlement check — gate on subscription tier ─────────────
                firmware_category = latest_pkg.get("firmwareCategory") or None
                tier_override     = latest_pkg.get("tierOverride")     or None
                entitlement       = _invoke_entitlement_check(thing_name, firmware_category, tier_override)

                if not entitlement.get("eligible", True):
                    log.info(json.dumps({
                        "msg":              "update_blocked_by_entitlement",
                        "userId":           user_id,
                        "deviceId":         device_id,
                        "packageName":      pkg_name,
                        "availableVersion": latest_ver,
                        "firmwareCategory": firmware_category,
                        "reason":           entitlement.get("reason"),
                        "subscriptionTier": entitlement.get("subscriptionTier"),
                        "minimumTier":      entitlement.get("minimumTier"),
                    }))
                    continue

                # Newer version available and entitled
                artifact_size = latest_pkg.get("artifactSize", 0)
                if isinstance(artifact_size, Decimal):
                    artifact_size = int(artifact_size)

                available_updates.append({
                    "packageName":      pkg_name,
                    "deviceType":       latest_pkg.get("deviceType", ""),
                    "firmwareCategory": firmware_category,
                    "releaseType":      latest_pkg.get("releaseType", "PROD"),
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
                    "firmwareCategory": firmware_category,
                    "entitlementReason": entitlement.get("reason"),
                }))

            total_updates += len(available_updates)
            result_devices.append({
                "deviceId":          device_id,
                "otaStatus":         "REGISTERED",
                "model":             dev.get("model", ""),
                "hwRevision":        dev.get("hwRevision", ""),
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
