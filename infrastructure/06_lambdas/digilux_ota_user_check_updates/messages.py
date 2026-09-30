"""
User-facing copy for digilux_ota_user_check_updates.

Production pattern:
  - Defaults live in messages.json (versioned with the Lambda package).
  - Optional env overrides (NO_UPDATE_MSG, NOT_REGISTERED_MSG, …) for hotfixes
    without a code change — env wins when set.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger()

_DEFAULTS = {
    "no_update": (
        "New Firmware update not available, please try again later."
    ),
    "not_registered": (
        "Your device is not compatible for OTA upgrades."
    ),
    "in_progress": (
        "Your Firmware update ver {version} is in progress, please check after some time "
        "for status. Note: Please ensure the controller is Powered on."
    ),
    "failed": (
        "Your last firmware ver {version} update failed. Please contact support."
    ),
    "timed_out": (
        "Your firmware update ver {version} timed out. Please retry."
    ),
    "job_completed": (
        "Your firmware ver {version} update was successful."
    ),
    "unauthorized": "Unauthorized — invalid token",
    "internal_error": "Internal server error",
}

# Env key → messages.json key (ops override without redeploying copy)
_ENV_OVERRIDES = {
    "NO_UPDATE_MSG": "no_update",
    "NOT_REGISTERED_MSG": "not_registered",
    "OTA_IN_PROGRESS_MSG": "in_progress",
    "OTA_FAILED_MSG": "failed",
    "OTA_TIMED_OUT_MSG": "timed_out",
    "OTA_COMPLETED_MSG": "job_completed",
}


def _load_file() -> dict:
    path = Path(__file__).with_name("messages.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.warning(json.dumps({
                "msg": "messages_json_invalid_shape",
                "path": str(path),
            }))
            return dict(_DEFAULTS)
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if isinstance(v, str) and v})
        return merged
    except FileNotFoundError:
        log.warning(json.dumps({
            "msg": "messages_json_missing",
            "path": str(path),
            "detail": "Falling back to built-in defaults",
        }))
        return dict(_DEFAULTS)
    except Exception as e:
        log.warning(json.dumps({
            "msg": "messages_json_load_failed",
            "path": str(path),
            "error": str(e),
        }))
        return dict(_DEFAULTS)


def _apply_env(messages: dict) -> dict:
    out = dict(messages)
    for env_key, msg_key in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            out[msg_key] = val
    return out


MESSAGES = _apply_env(_load_file())

# Convenience aliases used by lambda_function
NO_UPDATE_MSG = MESSAGES["no_update"]
NOT_REGISTERED_MSG = MESSAGES["not_registered"]
OTA_IN_PROGRESS_MSG = MESSAGES["in_progress"]
OTA_FAILED_MSG = MESSAGES["failed"]
OTA_TIMED_OUT_MSG = MESSAGES["timed_out"]
OTA_COMPLETED_MSG = MESSAGES["job_completed"]
UNAUTHORIZED_MSG = MESSAGES["unauthorized"]
INTERNAL_ERROR_MSG = MESSAGES["internal_error"]
