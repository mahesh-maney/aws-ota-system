"""
AWS IoT Jobs MQTT client.
Subscribes to Jobs notify-next topic, retrieves job documents,
and provides callbacks for status reporting.
"""
import json
import logging
import time
import threading
from typing import Callable

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

JOB_STATUS_QUEUED      = "QUEUED"
JOB_STATUS_IN_PROGRESS = "IN_PROGRESS"
JOB_STATUS_SUCCEEDED   = "SUCCEEDED"
JOB_STATUS_FAILED      = "FAILED"
JOB_STATUS_REJECTED    = "REJECTED"


class JobsClient:
    def __init__(self, on_job: Callable[[dict], None]):
        """
        on_job: called with the full job document dict when a new job is available.
        """
        self._on_job = on_job
        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._pending_get: dict[str, threading.Event] = {}   # jobId → event
        self._job_docs: dict[str, dict] = {}

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        client = mqtt.Client(client_id=config.THING_NAME, protocol=mqtt.MQTTv311)
        client.tls_set(
            ca_certs=config.CA_PATH,
            certfile=config.CERT_PATH,
            keyfile=config.KEY_PATH,
        )
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message

        client.connect(config.IOT_ENDPOINT, port=8883, keepalive=60)
        client.loop_start()

        if not self._connected.wait(timeout=30):
            raise RuntimeError("Timed out connecting to AWS IoT Core")

        self._client = client
        logger.info(f"Connected to IoT Jobs: {config.IOT_ENDPOINT}")

    def disconnect(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    @property
    def mqtt_client(self) -> mqtt.Client:
        return self._client

    # ── Jobs API ──────────────────────────────────────────────────────────────

    def request_next_job(self) -> None:
        """Ask AWS for the next pending job."""
        self._client.publish(
            f"$aws/things/{config.THING_NAME}/jobs/$next/get",
            json.dumps({"clientToken": config.THING_NAME}),
            qos=1,
        )

    def update_job_status(
        self,
        job_id: str,
        status: str,
        progress: int = 0,
        detail: str = "",
        error: str = "",
    ) -> None:
        """Update the AWS IoT Job status + publish to the device OTA status topic."""
        payload = {
            "status": status,
            "statusDetails": {
                "progress": str(progress),
                "detail": detail,
            },
            "clientToken": config.THING_NAME,
        }
        self._client.publish(
            config.jobs_update_topic(job_id),
            json.dumps(payload),
            qos=1,
        )

        # Also publish to the existing device OTA status topic (backend IoT rule picks this up)
        status_msg = {
            "jobId": job_id,
            "deviceId": config.DEVICE_ID,
            "thingName": config.THING_NAME,
            "status": status,
            "progress": progress,
            "statusDetail": detail,
            "error": error,
        }
        self._client.publish(
            config.TOPIC_OTA_STATUS,
            json.dumps(status_msg),
            qos=1,
        )
        logger.info(f"Job {job_id} status: {status} ({progress}%) — {detail}")

    def register_device(self, installed_versions: dict) -> None:
        """Publish registration message so backend knows this device is OTA-capable."""
        payload = {
            "deviceId": config.DEVICE_ID,
            "thingName": config.THING_NAME,
            "model": config.MODEL,
            "hwRevision": config.HW_REVISION,
            "installedVersions": installed_versions,
        }
        self._client.publish(
            config.TOPIC_OTA_REGISTER,
            json.dumps(payload),
            qos=1,
        )
        logger.info(f"Device registered: {config.THING_NAME}")

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.error(f"MQTT connection failed: rc={rc}")
            return
        logger.info("MQTT connected")

        # Subscribe to all relevant Jobs topics
        topics = [
            (f"$aws/things/{config.THING_NAME}/jobs/notify-next", 1),
            (f"$aws/things/{config.THING_NAME}/jobs/$next/get/accepted", 1),
            (f"$aws/things/{config.THING_NAME}/jobs/$next/get/rejected", 1),
        ]
        client.subscribe(topics)
        self._connected.set()

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnect: rc={rc} — will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            logger.warning(f"Non-JSON message on {topic}")
            return

        if "jobs/notify-next" in topic or "jobs/$next/get/accepted" in topic:
            job = payload.get("execution")
            if job:
                logger.info(f"New job received: {job.get('jobId')}")
                threading.Thread(target=self._on_job, args=(job,), daemon=True).start()
            else:
                logger.debug("No pending jobs.")

        elif "get/rejected" in topic:
            logger.warning(f"Job get rejected: {payload}")
