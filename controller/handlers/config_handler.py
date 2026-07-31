"""
Configuration and Rules Update Handler.
Handles CONFIG and RULES update types.

Config/rules are JSON files. Atomic write with backup.
No service restart required — the controller hot-reloads config on SIGHUP.
"""
import json
import logging
import os
import subprocess

from utils.backup_manager import backup_file, restore_file
import config

logger = logging.getLogger(__name__)


def handle(job_doc: dict, artifact_path: str, progress_cb) -> None:
    params     = job_doc.get("parameters", {})
    pkg_type   = job_doc["packageType"]   # CONFIG or RULES
    version    = job_doc["version"]
    config_key = params.get("configKey", pkg_type.lower())
    target_file = os.path.join(config.CONFIG_DIR, f"{config_key}.json")

    logger.info(
        f"config_handler started — type={pkg_type} version={version} "
        f"configKey={config_key} targetFile={target_file}"
    )

    progress_cb(10, f"Backing up {config_key} config")
    logger.info(f"Step 1/4: Backing up {target_file}")
    backup_path = backup_file(
        source_file=target_file,
        backup_root=config.BACKUP_DIR,
        label=f"config-{config_key}",
    )
    logger.info(f"Step 1/4 complete: config backed up to {backup_path or '(no existing file)'}")

    try:
        progress_cb(30, "Validating new config")
        logger.info(f"Step 2/4: Validating JSON artifact at {artifact_path}")
        new_config = _load_and_validate(artifact_path)
        logger.info(f"Step 2/4 complete: JSON valid, top-level keys={list(new_config.keys()) if isinstance(new_config, dict) else 'array'}")

        progress_cb(60, "Applying new config")
        logger.info(f"Step 3/4: Atomic write to {target_file}")
        _atomic_write(target_file, new_config)
        logger.info(f"Step 3/4 complete: {target_file} updated")

        progress_cb(80, "Reloading controller config")
        logger.info("Step 4/4: Sending SIGHUP to controller for hot-reload")
        _reload_config()
        logger.info("Step 4/4 complete: SIGHUP sent")

        progress_cb(100, f"{pkg_type} {config_key} updated to {version}")
        logger.info(f"config_handler complete: {config_key} updated to {version}")

    except Exception as e:
        logger.error(f"Config update failed: {e} — rolling back {target_file}")
        logger.debug(f"Failure detail: {e}", exc_info=True)
        if backup_path:
            logger.warning(f"Restoring config from backup: {backup_path}")
            restore_file(backup_path, target_file)
            _reload_config()
            logger.warning(f"Config rolled back successfully from {backup_path}")
        else:
            logger.error("No backup available — config may be in inconsistent state")
        raise


def _load_and_validate(artifact_path: str) -> dict:
    with open(artifact_path, "r") as f:
        data = json.load(f)
    if not isinstance(data, (dict, list)):
        raise ValueError("Config artifact must be a JSON object or array")
    return data


def _atomic_write(target_file: str, data) -> None:
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    tmp = target_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, target_file)
    logger.info(f"Config written: {target_file}")


def _reload_config() -> None:
    """Send SIGHUP to digilux-controller to trigger hot-reload."""
    try:
        result = subprocess.run(
            ["systemctl", "kill", "--signal=SIGHUP", "digilux-controller"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Sent SIGHUP to controller for config reload")
        else:
            logger.warning(f"SIGHUP failed: {result.stderr.decode()}")
    except Exception as e:
        logger.warning(f"Could not reload config: {e}")


def handle_rules(job_doc: dict, artifact_path: str, progress_cb) -> None:
    """Alias for RULES type — same flow, different target path."""
    job_doc = {**job_doc, "parameters": {**job_doc.get("parameters", {}), "configKey": "rules"}}
    handle(job_doc, artifact_path, progress_cb)
