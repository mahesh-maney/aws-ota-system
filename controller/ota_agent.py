"""
Digilux OTA Agent — main process.
Runs as a systemd service on the Debian Linux controller.

Responsibilities:
  1. Connect to AWS IoT Core
  2. Register device in OTA inventory
  3. Listen for IoT Jobs on the notify-next topic
  4. Route each job to the correct handler
  5. Report progress and final status to AWS IoT Jobs
  6. Maintain installed versions on disk
"""
import json
import logging
import os
import signal
import sys
import time
import traceback

import config
from jobs_client import JobsClient, JOB_STATUS_IN_PROGRESS, JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED
from handlers.firmware_handler import RecoveryNeededError
from utils.downloader import download_and_verify
from utils.shadow_reporter import ShadowReporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ota-agent")

# Map operation types to handler modules
OPERATION_MAP = {
    "CONTROLLER_FIRMWARE": "firmware_handler",
    "CONTROLLER_APP":      "firmware_handler",
    "DRIVER":              "driver_handler",
    "ZIGBEE_DEVICE":       "zigbee_ota_handler",
    "CONFIG":              "config_handler",
    "RULES":               "config_handler",
}


class OTAAgent:
    def __init__(self):
        self._installed_versions = self._load_installed_versions()
        self._jobs_client = JobsClient(on_job=self._handle_job)
        self._shadow_reporter: ShadowReporter | None = None
        self._running = True
        self._current_job_id: str | None = None

        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        os.makedirs(config.BACKUP_DIR, exist_ok=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        logger.info(
            f"OTA Agent starting — thingName={config.THING_NAME} "
            f"deviceId={config.DEVICE_ID} "
            f"installedVersions={json.dumps(self._installed_versions)}"
        )

        self._jobs_client.connect()
        logger.info(f"MQTT connected to IoT Core endpoint")

        self._shadow_reporter = ShadowReporter(
            self._jobs_client.mqtt_client,
            config.THING_NAME,
        )

        time.sleep(2)  # Brief wait for MQTT subscriptions to settle
        self._jobs_client.register_device(self._installed_versions)
        logger.info(
            f"Device registration published — "
            f"versions={json.dumps(self._installed_versions)}"
        )

        self._shadow_reporter.report_versions(self._installed_versions)
        logger.debug("Named shadow 'ota-state' updated with current installed versions")

        time.sleep(1)
        self._jobs_client.request_next_job()
        logger.info("Requested pending jobs from IoT Jobs service")
        logger.info("OTA Agent running — waiting for jobs...")

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        while self._running:
            time.sleep(10)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received")
        self._running = False
        self._jobs_client.disconnect()

    # ── Job routing ───────────────────────────────────────────────────────────

    def _handle_job(self, job_execution: dict) -> None:
        job_id    = job_execution.get("jobId")
        job_doc   = job_execution.get("jobDocument", {})
        operation = job_doc.get("operation", "").upper()

        if not job_id or not operation:
            logger.error(f"Invalid job execution received (missing jobId or operation): {job_execution}")
            return

        pkg_name = job_doc.get("packageName", "unknown")
        version  = job_doc.get("version", "unknown")

        logger.info(
            f"Job received — jobId={job_id} operation={operation} "
            f"packageName={pkg_name} version={version}"
        )
        self._current_job_id = job_id

        if operation not in OPERATION_MAP:
            logger.error(f"Unknown operation '{operation}' in job {job_id} — rejecting")
            self._jobs_client.update_job_status(
                job_id, JOB_STATUS_FAILED,
                error=f"Unknown operation: {operation}",
            )
            return

        artifact      = job_doc.get("artifact", {})
        presigned_url = artifact.get("presignedUrl")
        expected_sha256 = artifact.get("sha256")
        expected_sig  = artifact.get("signature")
        artifact_size = artifact.get("size", 0)

        logger.debug(
            f"Artifact metadata — sha256={expected_sha256} "
            f"size={artifact_size} bytes sigLen={len(expected_sig or '')} chars"
        )

        if not all([presigned_url, expected_sha256, expected_sig]):
            logger.error(f"Job document missing artifact fields for job {job_id}")
            self._jobs_client.update_job_status(
                job_id, JOB_STATUS_FAILED,
                error="Job document missing artifact fields (presignedUrl/sha256/signature)",
            )
            return

        self._jobs_client.update_job_status(
            job_id, JOB_STATUS_IN_PROGRESS, progress=5, detail="Starting download"
        )

        artifact_path = os.path.join(
            config.DOWNLOAD_DIR,
            f"{job_id}_{pkg_name}_{version}"
        )
        job_start_time = time.monotonic()

        try:
            logger.info(f"[{job_id}] Phase 1/3: Downloading and verifying artifact ({artifact_size} bytes)")
            self._progress(job_id, 10, "Downloading artifact")
            download_and_verify(
                presigned_url=presigned_url,
                expected_sha256=expected_sha256,
                expected_signature=expected_sig,
                dest_path=artifact_path,
                public_key_path=config.OTA_PUBLIC_KEY_PATH,
            )
            logger.info(f"[{job_id}] Phase 1/3 complete: artifact downloaded and verified at {artifact_path}")

            logger.info(f"[{job_id}] Phase 2/3: Running {operation} handler for {pkg_name}@{version}")
            self._progress(job_id, 20, f"Running {operation} handler")
            self._run_handler(operation, job_doc, artifact_path, job_id)
            logger.info(f"[{job_id}] Phase 2/3 complete: handler finished successfully")

            logger.info(f"[{job_id}] Phase 3/3: Persisting installed version to disk")
            self._installed_versions[pkg_name] = version
            self._save_installed_versions()
            logger.info(f"[{job_id}] Installed versions saved: {json.dumps(self._installed_versions)}")

            elapsed = int((time.monotonic() - job_start_time))
            self._jobs_client.update_job_status(
                job_id, JOB_STATUS_SUCCEEDED, progress=100,
                detail=f"{pkg_name} updated to {version}",
            )
            self._shadow_reporter.report_versions(self._installed_versions)
            logger.info(
                f"[{job_id}] SUCCEEDED — {pkg_name} updated to {version} "
                f"in {elapsed}s"
            )

        except RecoveryNeededError as e:
            elapsed = int((time.monotonic() - job_start_time))
            logger.critical(
                f"[{job_id}] NEEDS RECOVERY after {elapsed}s — "
                f"install AND rollback both failed: {e}"
            )
            logger.debug(traceback.format_exc())
            self._jobs_client.update_job_status(
                job_id, JOB_STATUS_FAILED,
                progress=0,
                detail="NEEDS_RECOVERY: install failed and rollback failed — manual intervention required",
                error=str(e),
            )

        except Exception as e:
            elapsed = int((time.monotonic() - job_start_time))
            logger.error(
                f"[{job_id}] FAILED after {elapsed}s — "
                f"previous version restored: {e}"
            )
            logger.debug(traceback.format_exc())
            self._jobs_client.update_job_status(
                job_id, JOB_STATUS_FAILED,
                progress=0,
                detail="Install failed — previous version restored",
                error=str(e),
            )

        finally:
            self._current_job_id = None
            if os.path.exists(artifact_path):
                os.unlink(artifact_path)
                logger.debug(f"[{job_id}] Cleaned up artifact file: {artifact_path}")
            time.sleep(2)
            self._jobs_client.request_next_job()
            logger.debug(f"[{job_id}] Requested next pending job")

    def _run_handler(self, operation: str, job_doc: dict, artifact_path: str, job_id: str) -> None:
        module_name = OPERATION_MAP[operation]

        def progress_cb(pct: int, detail: str):
            # Map handler-local percentage (0-100) to overall (20-95)
            mapped = 20 + int(pct * 0.75)
            self._progress(job_id, min(mapped, 95), detail)

        if operation == "ZIGBEE_DEVICE":
            from handlers import zigbee_ota_handler
            zigbee_ota_handler.handle(
                job_doc, artifact_path, progress_cb,
                mqtt_client=self._jobs_client.mqtt_client,
            )
        elif operation in ("CONTROLLER_FIRMWARE", "CONTROLLER_APP"):
            from handlers import firmware_handler
            firmware_handler.handle(job_doc, artifact_path, progress_cb)
        elif operation == "DRIVER":
            from handlers import driver_handler
            driver_handler.handle(job_doc, artifact_path, progress_cb)
        elif operation in ("CONFIG", "RULES"):
            from handlers import config_handler
            config_handler.handle(job_doc, artifact_path, progress_cb)

    def _progress(self, job_id: str, pct: int, detail: str) -> None:
        self._jobs_client.update_job_status(
            job_id, JOB_STATUS_IN_PROGRESS, progress=pct, detail=detail
        )
        if self._shadow_reporter:
            pkg_name = ""  # best-effort
            self._shadow_reporter.report_update_progress(job_id, pkg_name, pct, "IN_PROGRESS")

    # ── Installed versions persistence ────────────────────────────────────────

    def _load_installed_versions(self) -> dict:
        if os.path.exists(config.VERSIONS_FILE):
            try:
                with open(config.VERSIONS_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load versions file: {e}")
        return {}

    def _save_installed_versions(self) -> None:
        os.makedirs(os.path.dirname(config.VERSIONS_FILE), exist_ok=True)
        tmp = config.VERSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._installed_versions, f, indent=2)
        os.replace(tmp, config.VERSIONS_FILE)
        logger.debug(f"Versions saved: {self._installed_versions}")


if __name__ == "__main__":
    agent = OTAAgent()
    agent.start()
