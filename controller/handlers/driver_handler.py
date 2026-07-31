"""
Device Driver Update Handler.
Handles DRIVER update type.

Drivers live in /opt/digilux/drivers/{driver_name}/.
Each driver is a self-contained Python package with a manifest.json.
"""
import json
import logging
import os
import subprocess
import tarfile
import zipfile

from utils.backup_manager import backup, restore
from utils.health_check import is_healthy
import config

logger = logging.getLogger(__name__)


def handle(job_doc: dict, artifact_path: str, progress_cb) -> None:
    params      = job_doc.get("parameters", {})
    driver_name = params.get("driverName")
    if not driver_name:
        raise ValueError("DRIVER update missing parameters.driverName")

    pkg_name   = job_doc["packageName"]
    version    = job_doc["version"]
    driver_dir = os.path.join(config.DRIVERS_DIR, driver_name)

    logger.info(
        f"driver_handler started — driver={driver_name} version={version} "
        f"artifact={artifact_path} driverDir={driver_dir}"
    )

    progress_cb(10, f"Backing up driver: {driver_name}")
    logger.info(f"Step 1/3: Backing up {driver_dir}")
    backup_path = backup(
        source_dir=driver_dir,
        backup_root=config.BACKUP_DIR,
        label=f"driver-{driver_name}",
        max_backups=config.MAX_BACKUPS,
    )
    logger.info(f"Step 1/3 complete: backup at {backup_path or '(no existing driver)'}")

    try:
        progress_cb(30, "Installing driver package")
        logger.info(f"Step 2/3: Installing driver {driver_name} from {artifact_path}")
        os.makedirs(driver_dir, exist_ok=True)
        _install_driver(artifact_path, driver_dir, driver_name, version)
        logger.info(f"Step 2/3 complete: driver files installed to {driver_dir}")

        progress_cb(70, "Reloading driver in controller")
        logger.info(f"Step 3/3: Reloading driver {driver_name} via SIGUSR1")
        _reload_driver(driver_name)
        logger.info(f"Step 3/3 complete: SIGUSR1 sent to digilux-controller")

        progress_cb(100, f"Driver {driver_name} updated to {version}")
        logger.info(f"driver_handler complete: {driver_name} updated to {version}")

    except Exception as e:
        logger.error(f"Driver update failed: {e} — rolling back {driver_name}")
        logger.debug(f"Failure detail: {e}", exc_info=True)
        _rollback(backup_path, driver_dir, driver_name)
        raise


def _install_driver(artifact_path: str, driver_dir: str, driver_name: str, version: str) -> None:
    import shutil
    # Clear existing driver files (keep directory)
    for item in os.listdir(driver_dir):
        item_path = os.path.join(driver_dir, item)
        if os.path.isdir(item_path):
            shutil.rmtree(item_path)
        else:
            os.unlink(item_path)

    if artifact_path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(artifact_path, "r:gz") as tar:
            tar.extractall(path=driver_dir)
    elif artifact_path.endswith(".zip"):
        with zipfile.ZipFile(artifact_path, "r") as zf:
            zf.extractall(driver_dir)
    else:
        import shutil
        shutil.copy2(artifact_path, os.path.join(driver_dir, f"{driver_name}.py"))

    # Write version manifest
    manifest = {"driverName": driver_name, "version": version}
    with open(os.path.join(driver_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)


def _reload_driver(driver_name: str) -> None:
    """
    Signal the main controller service to reload drivers.
    Uses SIGUSR1 convention — the controller should handle this to hot-reload drivers
    without a full restart.
    """
    try:
        result = subprocess.run(
            ["systemctl", "kill", "--signal=SIGUSR1", "digilux-controller"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info(f"Sent SIGUSR1 to digilux-controller for driver reload")
        else:
            logger.warning("SIGUSR1 failed — driver will load on next restart")
    except Exception as e:
        logger.warning(f"Could not signal controller: {e}")


def _rollback(backup_path: str, driver_dir: str, driver_name: str) -> None:
    if not backup_path:
        logger.error(f"No backup for driver {driver_name} — cannot rollback")
        return
    try:
        restore(backup_path, driver_dir)
        _reload_driver(driver_name)
        logger.warning(f"Driver {driver_name} rolled back")
    except Exception as e:
        logger.critical(f"Driver rollback failed: {e}")
