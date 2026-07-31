"""
OTA Agent configuration.
All paths and settings read from environment variables with safe defaults.
Set these in /etc/digilux/ota-agent.env or the systemd unit file.
"""
import os

# ── AWS / IoT Core ────────────────────────────────────────────────────────────
IOT_ENDPOINT = os.environ["DIGILUX_IOT_ENDPOINT"]          # a2yxnt6tjmcgb1-ats.iot.ap-south-1.amazonaws.com
THING_NAME = os.environ["DIGILUX_THING_NAME"]               # digilux-{mac}
DEVICE_ID = os.environ["DIGILUX_DEVICE_ID"]                 # UUID from provisioning

CERT_PATH = os.environ.get("DIGILUX_CERT_PATH",  "/etc/digilux/certs/device.crt")
KEY_PATH  = os.environ.get("DIGILUX_KEY_PATH",   "/etc/digilux/certs/device.key")
CA_PATH   = os.environ.get("DIGILUX_CA_PATH",    "/etc/digilux/certs/root-ca.pem")

# ── Device metadata ───────────────────────────────────────────────────────────
MODEL      = os.environ.get("DIGILUX_MODEL",       "DGX-1000")
HW_REVISION = os.environ.get("DIGILUX_HW_REVISION", "1.0")

# ── OTA agent paths ───────────────────────────────────────────────────────────
OTA_BASE_DIR     = os.environ.get("OTA_BASE_DIR",     "/opt/digilux/ota")
DOWNLOAD_DIR     = os.environ.get("OTA_DOWNLOAD_DIR", "/tmp/digilux-ota")
BACKUP_DIR       = os.environ.get("OTA_BACKUP_DIR",   "/opt/digilux/backups")
APP_INSTALL_DIR  = os.environ.get("APP_INSTALL_DIR",  "/opt/digilux/app")
DRIVERS_DIR      = os.environ.get("DRIVERS_DIR",      "/opt/digilux/drivers")
CONFIG_DIR       = os.environ.get("CONFIG_DIR",        "/etc/digilux")

# Installed versions file (persisted across restarts)
VERSIONS_FILE = os.environ.get("OTA_VERSIONS_FILE", "/etc/digilux/installed_versions.json")

# OTA signing public key (ECDSA P-256, PEM format)
OTA_PUBLIC_KEY_PATH = os.environ.get("OTA_PUBLIC_KEY_PATH", "/etc/digilux/ota-signing.pub")

# ── MQTT topics ───────────────────────────────────────────────────────────────
TOPIC_OTA_REGISTER = f"iot/device/{DEVICE_ID}/ota/register"
TOPIC_OTA_STATUS   = f"iot/device/{DEVICE_ID}/ota/status"

# AWS IoT Jobs reserved topics
TOPIC_JOBS_NOTIFY  = f"$aws/things/{THING_NAME}/jobs/notify-next"
TOPIC_JOBS_GET     = f"$aws/things/{THING_NAME}/jobs/$next/get"
TOPIC_JOBS_GET_RSP = f"$aws/things/{THING_NAME}/jobs/$next/get/accepted"

def jobs_update_topic(job_id: str) -> str:
    return f"$aws/things/{THING_NAME}/jobs/{job_id}/update"

def jobs_get_topic(job_id: str) -> str:
    return f"$aws/things/{THING_NAME}/jobs/{job_id}/get"

# ── zigbee2mqtt ───────────────────────────────────────────────────────────────
Z2M_MQTT_PREFIX   = os.environ.get("Z2M_MQTT_PREFIX",    "zigbee2mqtt")
Z2M_OTA_DIR       = os.environ.get("Z2M_OTA_DIR",        "/opt/zigbee2mqtt/data/ota")

# ── Health check ──────────────────────────────────────────────────────────────
HEALTH_CHECK_TIMEOUT_SEC = int(os.environ.get("HEALTH_CHECK_TIMEOUT_SEC", "120"))
POST_UPDATE_REBOOT_WAIT  = int(os.environ.get("POST_UPDATE_REBOOT_WAIT",  "90"))

# ── Rollback ──────────────────────────────────────────────────────────────────
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "3"))
