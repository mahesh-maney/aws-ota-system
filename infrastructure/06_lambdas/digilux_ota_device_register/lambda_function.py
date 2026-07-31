"""
digilux_ota_device_register
Triggered by IoT Rule when the OTA agent starts on a controller.
Registers the device in digilux_device_inventory (deviceId <-> thingName mapping).
Topic: iot/device/+/ota/register
Message: {
  "deviceId": "uuid",
  "thingName": "digilux-{mac}",
  "model": "DGX-1000",
  "hwRevision": "1.2",
  "installedVersions": {
    "controller-app": "1.0.0",
    "philips-hue-driver": "2.0.0"
  }
}
"""
import datetime
import json
import logging
import os
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
INVENTORY_TABLE = os.environ.get("INVENTORY_TABLE", "digilux_device_inventory")
CANARY_GROUP    = os.environ.get("CANARY_GROUP", "DGX-Canary")
CANARY_MAX      = int(os.environ.get("CANARY_MAX", "5"))

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
        device_id  = event.get("deviceId")
        thing_name = event.get("thingName")
        model      = event.get("model", "unknown")
        hw_rev     = event.get("hwRevision", "unknown")
        installed  = event.get("installedVersions", {})

        log.info(json.dumps({
            "msg": "device_register_received",
            "deviceId": device_id, "thingName": thing_name,
            "model": model, "hwRevision": hw_rev,
            "installedVersions": installed,
        }))

        if not device_id or not thing_name:
            log.warning(f"Register event missing deviceId or thingName — skipping. event={event}")
            return

        now_ms    = int(time.time() * 1000)
        inv_table = dynamo.Table(INVENTORY_TABLE)

        log.debug(f"Checking inventory for deviceId={device_id}")
        existing  = inv_table.get_item(Key={"deviceId": device_id}).get("Item")

        if existing:
            prev_versions = existing.get("installedVersions", {})
            changed = {k: v for k, v in installed.items() if prev_versions.get(k) != v}

            inv_table.update_item(
                Key={"deviceId": device_id},
                UpdateExpression=(
                    "SET thingName = :tn, model = :m, hwRevision = :hw, "
                    "installedVersions = :iv, lastSeen = :ts, lastUpdatedAt = :ts"
                ),
                ExpressionAttributeValues={
                    ":tn": thing_name, ":m": model, ":hw": hw_rev,
                    ":iv": installed,  ":ts": now_ms,
                },
            )
            log.info(json.dumps({
                "msg": "device_reconnected",
                "deviceId": device_id, "thingName": thing_name,
                "model": model, "hwRevision": hw_rev,
                "installedVersions": installed,
                "versionChangesDetected": changed,
            }))

            _audit("DEVICE_RECONNECTED", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   model=model, hwRevision=hw_rev,
                   installedVersions=installed,
                   versionChangesDetected=changed)

        else:
            inv_table.put_item(Item={
                "deviceId":         device_id,
                "thingName":        thing_name,
                "model":            model,
                "hwRevision":       hw_rev,
                "installedVersions": installed,
                "pendingJobId":     None,
                "lastSeen":         now_ms,
                "createdAt":        now_ms,
                "lastUpdatedAt":    now_ms,
            })
            log.info(json.dumps({
                "msg": "new_device_registered",
                "deviceId": device_id, "thingName": thing_name,
                "model": model, "hwRevision": hw_rev,
                "installedVersions": installed,
            }))

            assigned_group = _assign_to_thing_group(thing_name)

            _audit("DEVICE_FIRST_REGISTRATION", f"device:{device_id}",
                   {"deviceId": device_id, "thingName": thing_name},
                   "SUCCESS",
                   model=model, hwRevision=hw_rev,
                   installedVersions=installed,
                   assignedGroup=assigned_group)

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
