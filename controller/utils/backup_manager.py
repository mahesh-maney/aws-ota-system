"""
Application-level backup/restore for single-partition Debian controllers.
Maintains up to MAX_BACKUPS rolling backups of each managed directory.
"""
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def backup(source_dir: str, backup_root: str, label: str, max_backups: int = 3) -> str:
    """
    Creates a timestamped backup of source_dir under backup_root/{label}/.
    Prunes old backups beyond max_backups.
    Returns the backup path.
    """
    if not os.path.exists(source_dir):
        logger.warning(f"Nothing to back up — {source_dir} does not exist.")
        return ""

    label_dir = os.path.join(backup_root, label)
    os.makedirs(label_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(label_dir, ts)

    logger.info(f"Backing up {source_dir} → {backup_path}")
    shutil.copytree(source_dir, backup_path)

    _prune_old_backups(label_dir, max_backups)
    return backup_path


def restore(backup_path: str, target_dir: str) -> None:
    """
    Restores a backup to target_dir. Removes current target_dir first.
    """
    if not backup_path or not os.path.exists(backup_path):
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    logger.warning(f"Rolling back: restoring {backup_path} → {target_dir}")

    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    shutil.copytree(backup_path, target_dir)
    logger.info(f"Rollback complete: {target_dir}")


def backup_file(source_file: str, backup_root: str, label: str) -> str:
    """Backs up a single file (e.g., config files)."""
    if not os.path.exists(source_file):
        return ""
    label_dir = os.path.join(backup_root, label)
    os.makedirs(label_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(label_dir, f"{ts}_{os.path.basename(source_file)}")
    shutil.copy2(source_file, dest)
    logger.info(f"File backed up: {source_file} → {dest}")
    return dest


def restore_file(backup_file_path: str, target_file: str) -> None:
    if not os.path.exists(backup_file_path):
        raise FileNotFoundError(f"Backup file not found: {backup_file_path}")
    shutil.copy2(backup_file_path, target_file)
    logger.info(f"File restored: {backup_file_path} → {target_file}")


def latest_backup(backup_root: str, label: str) -> str | None:
    """Returns the path to the most recent backup for label, or None."""
    label_dir = os.path.join(backup_root, label)
    if not os.path.exists(label_dir):
        return None
    entries = sorted(
        [e for e in os.listdir(label_dir) if not e.startswith(".")],
        reverse=True,
    )
    if not entries:
        return None
    return os.path.join(label_dir, entries[0])


def _prune_old_backups(label_dir: str, max_backups: int) -> None:
    entries = sorted(os.listdir(label_dir), reverse=True)
    for old in entries[max_backups:]:
        old_path = os.path.join(label_dir, old)
        logger.debug(f"Pruning old backup: {old_path}")
        if os.path.isdir(old_path):
            shutil.rmtree(old_path)
        else:
            os.unlink(old_path)
