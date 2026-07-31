"""
Zigbee Device OTA Handler (via zigbee2mqtt).
Handles ZIGBEE_DEVICE update type.

Flow:
  1. Download Zigbee firmware (.ota / .zigbee) from pre-signed S3 URL
  2. Place firmware file in zigbee2mqtt's OTA directory
  3. Trigger OTA via zigbee2mqtt MQTT bridge API
  4. Monitor progress via zigbee2mqtt MQTT topics
  5. Report completion to AWS IoT Jobs

zigbee2mqtt OTA bridge API:
  Request:  zigbee2mqtt/bridge/request/device/ota_update/update
            Payload: {"id": "0x00158d0001234567"}
  Response: zigbee2mqtt/bridge/response/device/ota_update/update
            Payload: {"data": {"id": "...", "from": {...}, "to": {...}}, "status": "ok"|"error"}
  Progress: zigbee2mqtt/{friendlyName}
            Payload includes "update": {"state": "updating", "progress": 0-100}
"""
import json
import logging
import os
import shutil
import threading
import time

import config

logger = logging.getLogger(__name__)

OTA_TIMEOUT_SEC = 600   # 10 minutes for Zigbee OTA (can be slow over the air)


def handle(job_doc: dict, artifact_path: str, progress_cb, mqtt_client) -> None:
    """
    mqtt_client: the shared MQTT client from jobs_client (already connected).
    """
    params = job_doc.get("parameters", {})
    device_ieee = params.get("deviceIeee")
    friendly_name = params.get("friendlyName", device_ieee)

    if not device_ieee:
        raise ValueError("ZIGBEE_DEVICE update missing parameters.deviceIeee")

    version = job_doc["version"]

    logger.info(
        f"zigbee_ota_handler started — deviceIeee={device_ieee} "
        f"friendlyName={friendly_name} version={version} artifact={artifact_path}"
    )
    progress_cb(10, f"Preparing Zigbee OTA for {device_ieee}")

    ota_filename = os.path.basename(artifact_path)
    z2m_ota_path = os.path.join(config.Z2M_OTA_DIR, ota_filename)
    os.makedirs(config.Z2M_OTA_DIR, exist_ok=True)
    shutil.copy2(artifact_path, z2m_ota_path)
    logger.info(f"Step 1/3: Zigbee firmware staged at {z2m_ota_path}")

    progress_cb(20, "Triggering zigbee2mqtt OTA update")

    # Subscribe to response and progress topics
    result = {"done": False, "success": False, "error": None, "progress": 0}
    result_event = threading.Event()

    response_topic = f"{config.Z2M_MQTT_PREFIX}/bridge/response/device/ota_update/update"
    progress_topic = f"{config.Z2M_MQTT_PREFIX}/{friendly_name}"

    def on_z2m_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            if msg.topic == response_topic:
                if payload.get("data", {}).get("id") == device_ieee or \
                   payload.get("data", {}).get("id") == friendly_name:
                    if payload.get("status") == "ok":
                        result["done"] = True
                        result["success"] = True
                    elif payload.get("status") == "error":
                        result["done"] = True
                        result["success"] = False
                        result["error"] = payload.get("error", "Unknown zigbee2mqtt error")
                    result_event.set()

            elif msg.topic == progress_topic:
                update_info = payload.get("update", {})
                if update_info.get("state") == "updating":
                    pct = update_info.get("progress", 0)
                    result["progress"] = pct
                    mapped = 25 + int(pct * 0.65)
                    logger.info(f"Zigbee OTA progress for {device_ieee}: {pct}% (overall {mapped}%)")
                    progress_cb(mapped, f"Zigbee OTA progress: {pct}%")
                elif update_info.get("state") == "available":
                    logger.debug(f"Zigbee device {device_ieee} reports OTA 'available' state")
        except Exception as e:
            logger.warning(f"Error parsing z2m message: {e}")

    # Temporarily add our callback to the MQTT client
    original_on_message = mqtt_client.on_message
    mqtt_client.on_message = on_z2m_message
    mqtt_client.subscribe([(response_topic, 1), (progress_topic, 0)])

    try:
        update_request = {"id": device_ieee, "updateDevice": True}
        request_topic  = f"{config.Z2M_MQTT_PREFIX}/bridge/request/device/ota_update/update"
        logger.info(
            f"Step 2/3: Publishing OTA trigger to {request_topic} "
            f"for device {device_ieee} (timeout={OTA_TIMEOUT_SEC}s)"
        )
        mqtt_client.publish(request_topic, json.dumps(update_request), qos=1)

        completed = result_event.wait(timeout=OTA_TIMEOUT_SEC)

        if not completed:
            logger.error(
                f"Zigbee OTA timed out after {OTA_TIMEOUT_SEC}s "
                f"for device {device_ieee} — last progress: {result['progress']}%"
            )
            raise TimeoutError(
                f"Zigbee OTA timed out after {OTA_TIMEOUT_SEC}s for {device_ieee}"
            )

        if not result["success"]:
            logger.error(
                f"Zigbee OTA FAILED for {device_ieee}: {result['error']}"
            )
            raise RuntimeError(
                f"Zigbee OTA failed for {device_ieee}: {result['error']}"
            )

        logger.info(f"Step 3/3: Zigbee OTA complete for {device_ieee} → version {version}")
        progress_cb(100, f"Zigbee device {device_ieee} updated to {version}")
        logger.info(f"zigbee_ota_handler complete: device {device_ieee} updated to {version}")

    finally:
        # Restore original MQTT message handler
        mqtt_client.unsubscribe([response_topic, progress_topic])
        mqtt_client.on_message = original_on_message
        # Clean up the temporary firmware file (zigbee2mqtt caches internally)
        if os.path.exists(z2m_ota_path):
            os.unlink(z2m_ota_path)
