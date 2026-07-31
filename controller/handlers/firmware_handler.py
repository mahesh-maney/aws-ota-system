"""
Controller Application Firmware Handler.
Handles CONTROLLER_FIRMWARE and CONTROLLER_APP update types.

Strategy (single-partition Debian):
  1. Download & verify artifact
  2. Backup current installation
  3. Install new version (pip package or tarball)
  4. Restart the controller service
  5. Health check — if fail, restore backup and restart

Two distinct failure outcomes are reported to the cloud:
  - RolledBack  : install failed but previous version restored successfully — device is healthy
  - NeedsRecovery: install failed AND rollback failed — device needs manual intervention
"""
import logging
import os
import shutil
import subprocess
import tarfile
import zipfile

from utils.backup_manager import backup, restore, latest_backup
from utils.health_check import is_healthy
import config

logger = logging.getLogger(__name__)


class InstallError(RuntimeError):
    """Install failed; previous version was restored successfully."""


class RecoveryNeededError(RuntimeError):
    """Install failed AND rollback failed — device may be in a broken state."""

CONTROLLER_SERVICE = "digilux-controller"


def handle(job_doc: dict, artifact_path: str, progress_cb) -> None:
    """
    job_doc: full IoT job document
    artifact_path: local path to downloaded+verified artifact
    progress_cb(pct, detail): callable to report progress back to OTA agent
    Raises on unrecoverable failure.
    """
    pkg_name = job_doc["packageName"]
    version = job_doc["version"]

    logger.info(
        f"firmware_handler started — pkg={pkg_name} version={version} "
        f"artifact={artifact_path} installDir={config.APP_INSTALL_DIR}"
    )
    progress_cb(10, "Backing up current installation")

    logger.info(f"Step 1/5: Backing up {config.APP_INSTALL_DIR} before upgrade")
    backup_path = backup(
        source_dir=config.APP_INSTALL_DIR,
        backup_root=config.BACKUP_DIR,
        label=pkg_name,
        max_backups=config.MAX_BACKUPS,
    )
    logger.info(f"Step 1/5 complete: backup created at {backup_path}")

    try:
        progress_cb(20, "Stopping controller service")
        logger.info(f"Step 2/5: Stopping {CONTROLLER_SERVICE}")
        _stop_service(CONTROLLER_SERVICE)
        logger.info(f"Step 2/5 complete: {CONTROLLER_SERVICE} stopped")

        progress_cb(30, "Installing new version")
        logger.info(f"Step 3/5: Installing artifact {artifact_path} → {config.APP_INSTALL_DIR}")
        _install_artifact(artifact_path, config.APP_INSTALL_DIR)
        logger.info(f"Step 3/5 complete: artifact installed")

        progress_cb(60, "Starting controller service")
        logger.info(f"Step 4/5: Starting {CONTROLLER_SERVICE}")
        _start_service(CONTROLLER_SERVICE)
        logger.info(f"Step 4/5 complete: {CONTROLLER_SERVICE} started")

        progress_cb(70, "Running health check")
        logger.info(f"Step 5/5: Running health check (timeout={config.HEALTH_CHECK_TIMEOUT_SEC}s)")
        healthy = is_healthy(timeout_sec=config.HEALTH_CHECK_TIMEOUT_SEC)

        if not healthy:
            logger.error(f"Step 5/5 FAILED: health check timed out — triggering rollback")
            raise RuntimeError("Health check failed after update")

        logger.info(f"Step 5/5 complete: health check passed")
        progress_cb(100, f"Update to {version} succeeded")
        logger.info(f"firmware_handler complete: {pkg_name} successfully updated to {version}")

    except Exception as e:
        logger.error(f"Install failed at some step: {e} — initiating rollback to {backup_path}")
        logger.debug(f"Failure detail: {e}", exc_info=True)
        _rollback(backup_path, pkg_name, original_error=e)
        # _rollback raises RecoveryNeededError if it fails; if we reach here rollback succeeded
        logger.warning(f"Rollback succeeded — {pkg_name} restored to previous version")
        raise InstallError(
            f"Install failed (previous version restored): {e}"
        ) from e


def _install_artifact(artifact_path: str, install_dir: str) -> None:
    """Supports .tar.gz, .zip, and Python wheel (.whl) artifacts."""
    os.makedirs(install_dir, exist_ok=True)
    artifact_size = os.path.getsize(artifact_path)
    logger.debug(f"Installing artifact {artifact_path} ({artifact_size} bytes) into {install_dir}")

    if artifact_path.endswith(".whl") or artifact_path.endswith(".whl.verified"):
        logger.info(f"Installing Python wheel via pip — target={install_dir}")
        result = subprocess.run(
            ["pip", "install", "--upgrade", "--target", install_dir, artifact_path],
            check=True,
            capture_output=True,
        )
        logger.debug(f"pip stdout: {result.stdout.decode()[:500]}")

    elif artifact_path.endswith((".tar.gz", ".tgz")):
        logger.info(f"Extracting tarball into {install_dir}")
        with tarfile.open(artifact_path, "r:gz") as tar:
            members = tar.getnames()
            logger.debug(f"Tarball contains {len(members)} entries, first few: {members[:5]}")
            tar.extractall(path=install_dir)

    elif artifact_path.endswith(".zip"):
        logger.info(f"Extracting zip archive into {install_dir}")
        with zipfile.ZipFile(artifact_path, "r") as zf:
            logger.debug(f"Zip contains {len(zf.namelist())} entries")
            zf.extractall(install_dir)

    else:
        dest = os.path.join(install_dir, os.path.basename(artifact_path))
        logger.info(f"Copying binary artifact to {dest} with mode 0o755")
        shutil.copy2(artifact_path, dest)
        os.chmod(dest, 0o755)

    logger.info(f"Artifact installed successfully to {install_dir}")


def _rollback(backup_path: str, pkg_name: str, original_error: Exception) -> None:
    """Restore the previous installation and restart the service.
    Raises RecoveryNeededError if rollback itself fails — caller must propagate
    this as a distinct error so the cloud marks the device as needing intervention."""
    if not backup_path:
        msg = f"No backup available for {pkg_name} — cannot roll back after: {original_error}"
        logger.critical(msg)
        raise RecoveryNeededError(msg)
    try:
        restore(backup_path, config.APP_INSTALL_DIR)
        _start_service(CONTROLLER_SERVICE)
        logger.warning(f"Rollback of {pkg_name} complete — previous version restored")
    except Exception as rb_err:
        msg = (
            f"Rollback of {pkg_name} FAILED ({rb_err}) after install error: {original_error}. "
            "Device may need manual recovery."
        )
        logger.critical(msg)
        raise RecoveryNeededError(msg) from rb_err


def _stop_service(name: str) -> None:
    result = subprocess.run(["systemctl", "stop", name], capture_output=True, timeout=30)
    if result.returncode != 0:
        logger.warning(f"Could not stop {name}: {result.stderr.decode()}")


def _start_service(name: str) -> None:
    result = subprocess.run(["systemctl", "start", name], capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start {name}: {result.stderr.decode()}")
