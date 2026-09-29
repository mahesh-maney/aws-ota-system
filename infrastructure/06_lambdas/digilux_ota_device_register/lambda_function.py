"""
digilux_ota_device_register
Triggered by IoT Rule when the OTA agent starts on a controller.
Updates OTA fields on the existing digilux_device_data item for this device.
Topic: iot/device/+/ota/register
Message: {
  "deviceId": "uuid",
  "globalInstalledVersion": "1.0.0",
  "deviceModel": "DGW-100",          # optional — used for thing-group hierarchy placement
  "package": {
    "name": "controller-app",
    "installedVersion": "1.0.0"
  }
}
package.installedVersion carries package-type-specific version info:
  Network controller : .jar
  Z2M                : .tar
  Zigbee             : .bin
  Custom/misc        : .db / .yml / .yaml / .cert / .prop

Thing Group Hierarchy (auto-maintained):
  DIGILUX
  └── PRODUCTION
      ├── GATEWAYS
      │   ├── DGW-100
      │   └── DGW-200
      └── TOUCH-PANELS
          ├── TP-100
          └── TP-200

Every registered device is placed in its model-specific leaf group.
OTA Jobs target PRODUCTION; child-group membership is recursive so all
devices receive PRODUCTION deployments automatically.
"""
import datetime
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION              = os.environ["REGION"]
DEVICE_DATA_TABLE   = os.environ.get("DEVICE_DATA_TABLE",    "digilux_device_data")
CANARY_GROUP        = os.environ.get("CANARY_GROUP",          "DGX-Canary")
CANARY_MAX          = int(os.environ.get("CANARY_MAX", "5"))

# ─── Thing Group Hierarchy ────────────────────────────────────────────────────
# These names must match what is created in CDK / Terraform.
# The Lambda ensures groups exist on first use; idempotent.
ROOT_GROUP          = os.environ.get("ROOT_GROUP",            "DIGILUX")
PRODUCTION_GROUP    = os.environ.get("PRODUCTION_GROUP",      "PRODUCTION")
GATEWAYS_GROUP      = os.environ.get("GATEWAYS_GROUP",        "GATEWAYS")
TOUCH_PANELS_GROUP  = os.environ.get("TOUCH_PANELS_GROUP",    "TOUCH-PANELS")

# Known device models and their category group
GATEWAY_MODELS      = set(os.environ.get("GATEWAY_MODELS",      "DGW-100,DGW-200").split(","))
TOUCH_PANEL_MODELS  = set(os.environ.get("TOUCH_PANEL_MODELS",  "TP-100,TP-200").split(","))

dynamo = boto3.resource("dynamodb", region_name=REGION)
iot    = boto3.client("iot", region_name=REGION)


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
    log.debug(json.dumps({"msg": "register_event_raw", "payload": event}))
    try:
        device_id         = event.get("deviceId")
        installed_version = event.get("globalInstalledVersion", "")
        package           = event.get("package", {})
        # deviceModel is optional — sent by the OTA agent to enable hierarchy placement.
        # If not in the event, we fall back to the stored DB value (set at provisioning time).
        device_model_event = event.get("deviceModel", "").strip() or None

        if not device_id:
            log.warning(f"Register event missing deviceId — skipping. event={event}")
            return

        # thingName == deviceId directly — no separate property needed.
        # Device-sent thingName is ignored to enforce consistency across all devices.
        thing_name = device_id

        log.info(json.dumps({
            "msg":              "device_register_received",
            "deviceId":         device_id,
            "thingName":        thing_name,
            "installedVersion": installed_version,
            "package":          package,
            "deviceModelEvent": device_model_event,
        }))

        now_ms     = int(time.time() * 1000)
        data_table = dynamo.Table(DEVICE_DATA_TABLE)

        # Query by deviceId (hash key) to get the full item including macAddress
        log.debug(f"Looking up device_data for deviceId={device_id}")
        items    = data_table.query(KeyConditionExpression=Key("deviceId").eq(device_id)).get("Items", [])
        existing = items[0] if items else None

        if not existing:
            log.warning(f"Device {device_id} not found in {DEVICE_DATA_TABLE} — OTA register skipped")
            return

        mac_address      = existing["macAddress"]
        prev_version     = existing.get("installedVersion", "")
        version_changed  = prev_version != installed_version
        first_time       = "thingName" not in existing

        # Resolve device model: event takes precedence; fall back to DB value
        device_model = device_model_event or existing.get("deviceModel", "") or ""

        # Build update expression — store deviceModel if we have one
        update_expr = (
            "SET thingName = :tn, globalInstalledVersion = :iv, #pkg = :pkg, "
            "lastSeen = :ts, lastUpdatedAt = :ts, "
            "pendingJobId = if_not_exists(pendingJobId, :null)"
        )
        expr_values = {
            ":tn":   thing_name,
            ":iv":   installed_version,
            ":pkg":  package,
            ":ts":   now_ms,
            ":null": None,
        }
        expr_names = {"#pkg": "package"}
        if device_model:
            update_expr += ", deviceModel = :dm"
            expr_values[":dm"] = device_model

        data_table.update_item(
            Key={"deviceId": device_id, "macAddress": mac_address},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )

        if first_time:
            log.info(json.dumps({
                "msg":              "new_device_ota_registered",
                "deviceId":         device_id,
                "thingName":        thing_name,
                "installedVersion": installed_version,
                "package":          package,
                "deviceModel":      device_model,
            }))
            assigned_groups = _assign_to_thing_group(thing_name, device_model)
            _audit("DEVICE_FIRST_REGISTRATION", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   installedVersion=installed_version,
                   package=package,
                   deviceModel=device_model,
                   assignedGroups=assigned_groups)
        else:
            log.info(json.dumps({
                "msg":              "device_reconnected",
                "deviceId":         device_id,
                "thingName":        thing_name,
                "installedVersion": installed_version,
                "package":          package,
                "deviceModel":      device_model,
                "versionChanged":   version_changed,
            }))
            _audit("DEVICE_RECONNECTED", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   installedVersion=installed_version,
                   package=package,
                   deviceModel=device_model,
                   versionChanged=version_changed)

    except Exception as e:
        log.exception(f"ERROR in device_register handler: {e}")


def _ensure_group_exists(group_name, parent_name=None):  # type: (str, str) -> bool
    """
    Ensure a thing group exists, creating it (with optional parent) if it doesn't.
    Returns True if the group is ready, False on error.
    Idempotent — safe to call every registration.
    """
    try:
        kwargs = {"thingGroupName": group_name}
        if parent_name:
            kwargs["parentGroupName"] = parent_name
        iot.create_thing_group(**kwargs)
        log.info(json.dumps({
            "msg":    "thing_group_created",
            "group":  group_name,
            "parent": parent_name,
        }))
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceAlreadyExistsException":
            return True  # already exists — that's fine
        log.warning(json.dumps({
            "msg":    "thing_group_create_failed",
            "group":  group_name,
            "parent": parent_name,
            "error":  str(e),
        }))
        return False


def _ensure_hierarchy() -> None:
    """
    Ensure the full group hierarchy exists:
      DIGILUX
      └── PRODUCTION
          ├── GATEWAYS
          │   ├── DGW-100
          │   └── DGW-200  (and any other GATEWAY_MODELS)
          └── TOUCH-PANELS
              ├── TP-100
              └── TP-200   (and any other TOUCH_PANEL_MODELS)

    Called once per first-time registration. Errors are non-fatal.
    """
    _ensure_group_exists(ROOT_GROUP)
    _ensure_group_exists(PRODUCTION_GROUP,   ROOT_GROUP)
    _ensure_group_exists(GATEWAYS_GROUP,     PRODUCTION_GROUP)
    _ensure_group_exists(TOUCH_PANELS_GROUP, PRODUCTION_GROUP)
    for model in GATEWAY_MODELS:
        _ensure_group_exists(model, GATEWAYS_GROUP)
    for model in TOUCH_PANEL_MODELS:
        _ensure_group_exists(model, TOUCH_PANELS_GROUP)


def _resolve_leaf_group(device_model):  # type: (str) -> tuple
    """
    Resolve (leaf_group, category_group) for a given device model.

    leaf_group     = the most-specific group (e.g. "DGW-100")
    category_group = its parent category  (e.g. "GATEWAYS")

    Returns (None, None) if the model is unrecognised — device falls back
    to being placed directly in PRODUCTION_GROUP.
    """
    if not device_model:
        return None, None
    model = device_model.strip().upper()
    if model in {m.upper() for m in GATEWAY_MODELS}:
        # Find original case name
        original = next((m for m in GATEWAY_MODELS if m.upper() == model), model)
        return original, GATEWAYS_GROUP
    if model in {m.upper() for m in TOUCH_PANEL_MODELS}:
        original = next((m for m in TOUCH_PANEL_MODELS if m.upper() == model), model)
        return original, TOUCH_PANELS_GROUP
    return None, None


def _add_to_group(thing_name: str, group_name: str) -> bool:
    """Add thing to group. Returns True on success."""
    try:
        iot.add_thing_to_thing_group(
            thingGroupName=group_name,
            thingName=thing_name,
        )
        log.info(json.dumps({
            "msg":       "device_added_to_group",
            "thingName": thing_name,
            "group":     group_name,
        }))
        return True
    except Exception as e:
        log.warning(json.dumps({
            "msg":       "device_group_assignment_failed",
            "thingName": thing_name,
            "group":     group_name,
            "error":     str(e),
        }))
        return False


def _assign_to_thing_group(thing_name: str, device_model: str) -> list:
    """
    Assign device to the correct place in the thing group hierarchy.

    Hierarchy:
      DIGILUX → PRODUCTION → GATEWAYS   → DGW-100 / DGW-200 / …
                           → TOUCH-PANELS → TP-100 / TP-200 / …

    Strategy:
    1. Ensure hierarchy groups exist (idempotent).
    2. Resolve the leaf group from device_model.
       - Known model → add to model-specific leaf group
         (device is implicitly in GATEWAYS/TOUCH-PANELS, PRODUCTION, DIGILUX)
       - Unknown/missing model → add directly to PRODUCTION_GROUP as fallback
    3. Optionally add to DGX-Canary if under the canary limit.

    AWS IoT Jobs target PRODUCTION; child-group membership is recursive so
    every leaf device receives PRODUCTION deployments automatically.
    """
    # Step 1: Ensure hierarchy exists
    try:
        _ensure_hierarchy()
    except Exception as e:
        log.warning(json.dumps({
            "msg":   "ensure_hierarchy_error",
            "error": str(e),
        }))

    assigned_groups = []

    # Step 2: Place device in the correct group
    leaf_group, category_group = _resolve_leaf_group(device_model)

    if leaf_group:
        # Ensure the leaf group exists under its category
        _ensure_group_exists(leaf_group, category_group)
        added = _add_to_group(thing_name, leaf_group)
        if added:
            assigned_groups.extend([leaf_group, category_group, PRODUCTION_GROUP, ROOT_GROUP])
            log.info(json.dumps({
                "msg":          "device_placed_in_hierarchy",
                "thingName":    thing_name,
                "deviceModel":  device_model,
                "leafGroup":    leaf_group,
                "categoryGroup":category_group,
                "productionGroup": PRODUCTION_GROUP,
                "rootGroup":    ROOT_GROUP,
                "note": "Device is implicitly in all ancestor groups via IoT hierarchy",
            }))
        else:
            # Leaf add failed — fall back to PRODUCTION directly
            log.warning(json.dumps({
                "msg":       "leaf_group_fallback",
                "thingName": thing_name,
                "leafGroup": leaf_group,
                "fallback":  PRODUCTION_GROUP,
            }))
            _add_to_group(thing_name, PRODUCTION_GROUP)
            assigned_groups.append(PRODUCTION_GROUP)
    else:
        # Unknown model — add directly to PRODUCTION (safe fallback)
        log.info(json.dumps({
            "msg":         "unrecognised_device_model_fallback",
            "thingName":   thing_name,
            "deviceModel": device_model,
            "fallback":    PRODUCTION_GROUP,
            "detail":      "Device model not in GATEWAY_MODELS or TOUCH_PANEL_MODELS; added to PRODUCTION directly",
        }))
        added = _add_to_group(thing_name, PRODUCTION_GROUP)
        if added:
            assigned_groups.append(PRODUCTION_GROUP)

    # Step 3: Optionally also add to DGX-Canary if under limit
    try:
        canary_members = iot.list_things_in_thing_group(
            thingGroupName=CANARY_GROUP, maxResults=100
        )
        canary_count = len(canary_members.get("things", []))
        log.debug(json.dumps({
            "msg":   "canary_group_count",
            "group": CANARY_GROUP,
            "count": canary_count,
            "max":   CANARY_MAX,
        }))
        if canary_count < CANARY_MAX:
            added_canary = _add_to_group(thing_name, CANARY_GROUP)
            if added_canary:
                assigned_groups.append(CANARY_GROUP)
                log.info(json.dumps({
                    "msg":        "device_added_to_canary_group",
                    "thingName":  thing_name,
                    "group":      CANARY_GROUP,
                    "canaryCount": canary_count,
                }))
    except Exception as e:
        log.warning(json.dumps({
            "msg":       "canary_group_assignment_failed",
            "thingName": thing_name,
            "error":     str(e),
        }))

    log.info(json.dumps({
        "msg":            "group_assignment_complete",
        "thingName":      thing_name,
        "deviceModel":    device_model,
        "assignedGroups": assigned_groups,
    }))
    return assigned_groups
