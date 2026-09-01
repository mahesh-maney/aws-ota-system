"""
digilux_ota_device_register
Triggered by IoT Rule when the OTA agent starts on a controller.
Updates OTA fields on the existing digilux_device_data item for this device.
Topic: iot/device/+/ota/register
Message: {
  "deviceId": "uuid",
  "globalInstalledVersion": "1.0.0",
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
"""
import datetime
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION            = os.environ["REGION"]
DEVICE_DATA_TABLE = os.environ.get("DEVICE_DATA_TABLE", "digilux_device_data")
CANARY_GROUP      = os.environ.get("CANARY_GROUP", "DGX-Canary")
CANARY_MAX        = int(os.environ.get("CANARY_MAX", "5"))

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

        data_table.update_item(
            Key={"deviceId": device_id, "macAddress": mac_address},
            UpdateExpression=(
                "SET thingName = :tn, globalInstalledVersion = :iv, #pkg = :pkg, "
                "lastSeen = :ts, lastUpdatedAt = :ts, "
                "pendingJobId = if_not_exists(pendingJobId, :null)"
            ),
            ExpressionAttributeNames={"#pkg": "package"},
            ExpressionAttributeValues={
                ":tn":  thing_name,
                ":iv":  installed_version,  # stored as globalInstalledVersion
                ":pkg": package,
                ":ts":  now_ms,
                ":null": None,
            },
        )

        if first_time:
            log.info(json.dumps({
                "msg":              "new_device_ota_registered",
                "deviceId":         device_id,
                "thingName":        thing_name,
                "installedVersion": installed_version,
                "package":          package,
            }))
            assigned_group = _assign_to_thing_group(thing_name)
            _audit("DEVICE_FIRST_REGISTRATION", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   installedVersion=installed_version,
                   package=package,
                   assignedGroup=assigned_group)
        else:
            log.info(json.dumps({
                "msg":              "device_reconnected",
                "deviceId":         device_id,
                "thingName":        thing_name,
                "installedVersion": installed_version,
                "package":          package,
                "versionChanged":   version_changed,
            }))
            _audit("DEVICE_RECONNECTED", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   installedVersion=installed_version,
                   package=package,
                   versionChanged=version_changed)

    except Exception as e:
        log.exception(f"ERROR in device_register handler: {e}")


def _assign_to_thing_group(thing_name: str) -> str:
    """
    Assign device to DGX-Canary if under the canary limit, else DGX-Production.
    DGX-Beta assignment is manual (admin-controlled).
    Returns the group name assigned.
    """
    try:
        log.debug(f"Counting members of {CANARY_GROUP} to determine group assignment")
        canary_members = iot.list_things_in_thing_group(
            thingGroupName=CANARY_GROUP, maxResults=100
        )
        canary_count = len(canary_members.get("things", []))
        log.info(f"Canary group {CANARY_GROUP} has {canary_count}/{CANARY_MAX} members")

        target_group = CANARY_GROUP if canary_count < CANARY_MAX else "DGX-Production"

        iot.add_thing_to_thing_group(
            thingGroupName=target_group,
            thingName=thing_name,
        )
        log.info(f"Assigned {thing_name} to {target_group} (canary count was {canary_count})")
        return target_group
    except Exception as e:
        log.warning(f"Could not assign {thing_name} to a group: {e}")
        return "unknown"
