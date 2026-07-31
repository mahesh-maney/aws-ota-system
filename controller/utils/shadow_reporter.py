"""
Reports installed versions and OTA state to AWS IoT Named Shadow: 'ota-state'.
Used by the backend to track what is installed on each device.
"""
import json
import logging

logger = logging.getLogger(__name__)


class ShadowReporter:
    def __init__(self, mqtt_client, thing_name: str):
        self._client = mqtt_client
        self._thing = thing_name
        self._update_topic = f"$aws/things/{thing_name}/shadow/name/ota-state/update"
        self._get_topic    = f"$aws/things/{thing_name}/shadow/name/ota-state/get"

    def report_versions(self, installed_versions: dict, pending_job_id: str | None = None) -> None:
        """Publishes installed versions to the ota-state named shadow."""
        state = {
            "state": {
                "reported": {
                    "installedVersions": installed_versions,
                    "pendingJobId": pending_job_id,
                }
            }
        }
        self._client.publish(self._update_topic, json.dumps(state), qos=1)
        logger.info(f"Shadow updated with installed versions: {installed_versions}")

    def report_update_progress(self, job_id: str, package_name: str, progress: int, status: str) -> None:
        """Reports in-progress OTA state to shadow."""
        state = {
            "state": {
                "reported": {
                    "pendingJobId": job_id,
                    "pendingUpdate": {
                        "packageName": package_name,
                        "progress": progress,
                        "status": status,
                    }
                }
            }
        }
        self._client.publish(self._update_topic, json.dumps(state), qos=1)
