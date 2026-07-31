"""
Post-update health check.
Verifies the controller application is running correctly after an update.
Returns True if healthy, False if rollback should be triggered.
"""
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

# Services that must be running for the controller to be considered healthy.
# Adjust to match the actual systemd service names on the controller.
REQUIRED_SERVICES = [
    "digilux-controller",
    "zigbee2mqtt",
]

# MQTT connectivity test topic
HEALTH_MQTT_TIMEOUT = 10


def is_healthy(timeout_sec: int = 120) -> bool:
    """
    Waits up to timeout_sec for all required services to be active.
    Returns True if all services are running within the timeout window.
    """
    logger.info(f"Starting health check (timeout: {timeout_sec}s)...")
    deadline = time.monotonic() + timeout_sec
    check_interval = 5

    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        results   = {svc: _service_is_active(svc) for svc in REQUIRED_SERVICES}
        all_healthy = all(results.values())

        if all_healthy:
            elapsed = int(time.monotonic() - (deadline - timeout_sec))
            logger.info(
                f"Health check PASSED after {elapsed}s ({attempt} poll(s)) — "
                f"all services active: {list(results.keys())}"
            )
            return True

        failed    = [svc for svc, ok in results.items() if not ok]
        running   = [svc for svc, ok in results.items() if ok]
        remaining = int(deadline - time.monotonic())
        logger.info(
            f"Health check poll #{attempt}: waiting for {failed} "
            f"(running: {running}, {remaining}s remaining)"
        )
        time.sleep(check_interval)

    logger.error(
        f"Health check FAILED — services still not running after {timeout_sec}s: "
        f"{[svc for svc in REQUIRED_SERVICES if not _service_is_active(svc)]}"
    )
    return False


def _service_is_active(service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        logger.debug(f"systemctl is-active {service_name}: {state}")
        return state == "active"
    except subprocess.TimeoutExpired:
        logger.warning(f"systemctl timed out checking {service_name}")
        return False
    except FileNotFoundError:
        logger.warning(f"systemctl not found — assuming {service_name} is healthy (non-systemd env)")
        return True
