#!/bin/bash
# Digilux OTA Agent — controller install script
# Run as root on the Debian Linux controller.
# Usage: sudo ./install.sh
set -euo pipefail

OTA_DIR="/opt/digilux/ota"
VENV_DIR="$OTA_DIR/venv"
SERVICE_NAME="digilux-ota-agent"

# ── Verify required env vars are set ─────────────────────────────────────────
check_env() {
  local VAR="$1"
  if [ -z "${!VAR:-}" ]; then
    echo "ERROR: $VAR is not set. Set it before running this script."
    exit 1
  fi
}

check_env DIGILUX_IOT_ENDPOINT
check_env DIGILUX_THING_NAME
check_env DIGILUX_DEVICE_ID
check_env DIGILUX_CERT_PATH
check_env DIGILUX_KEY_PATH
check_env DIGILUX_CA_PATH

# ── Create user ───────────────────────────────────────────────────────────────
echo "==> Creating digilux user"
id digilux &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin digilux
usermod -aG systemd-journal digilux 2>/dev/null || true

# ── Directory structure ───────────────────────────────────────────────────────
echo "==> Creating directories"
mkdir -p "$OTA_DIR" \
         /opt/digilux/app \
         /opt/digilux/drivers \
         /opt/digilux/backups \
         /etc/digilux/certs \
         /tmp/digilux-ota

chown -R digilux:digilux /opt/digilux /etc/digilux
chmod 750 /etc/digilux/certs

# ── Python venv and dependencies ──────────────────────────────────────────────
echo "==> Setting up Python virtual environment"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install \
  paho-mqtt==2.1.0 \
  requests==2.31.0 \
  cryptography==42.0.8 \
  -q

# ── Copy agent files ──────────────────────────────────────────────────────────
echo "==> Installing OTA agent files"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "$SCRIPT_DIR"/*.py "$OTA_DIR/"
cp -r "$SCRIPT_DIR/handlers" "$OTA_DIR/"
cp -r "$SCRIPT_DIR/utils" "$OTA_DIR/"
cp -r "$SCRIPT_DIR/config.py" "$OTA_DIR/"

chown -R digilux:digilux "$OTA_DIR"
chmod 640 "$OTA_DIR"/*.py

# ── Write env file ────────────────────────────────────────────────────────────
echo "==> Writing /etc/digilux/ota-agent.env"
cat > /etc/digilux/ota-agent.env << EOF
DIGILUX_IOT_ENDPOINT=${DIGILUX_IOT_ENDPOINT}
DIGILUX_THING_NAME=${DIGILUX_THING_NAME}
DIGILUX_DEVICE_ID=${DIGILUX_DEVICE_ID}
DIGILUX_CERT_PATH=${DIGILUX_CERT_PATH:-/etc/digilux/certs/device.crt}
DIGILUX_KEY_PATH=${DIGILUX_KEY_PATH:-/etc/digilux/certs/device.key}
DIGILUX_CA_PATH=${DIGILUX_CA_PATH:-/etc/digilux/certs/root-ca.pem}
DIGILUX_MODEL=${DIGILUX_MODEL:-DGX-1000}
DIGILUX_HW_REVISION=${DIGILUX_HW_REVISION:-1.0}
OTA_BASE_DIR=/opt/digilux/ota
OTA_DOWNLOAD_DIR=/tmp/digilux-ota
OTA_BACKUP_DIR=/opt/digilux/backups
APP_INSTALL_DIR=/opt/digilux/app
DRIVERS_DIR=/opt/digilux/drivers
CONFIG_DIR=/etc/digilux
OTA_VERSIONS_FILE=/etc/digilux/installed_versions.json
OTA_PUBLIC_KEY_PATH=/etc/digilux/ota-signing.pub
Z2M_MQTT_PREFIX=zigbee2mqtt
Z2M_OTA_DIR=/opt/zigbee2mqtt/data/ota
HEALTH_CHECK_TIMEOUT_SEC=120
MAX_BACKUPS=3
EOF
chmod 640 /etc/digilux/ota-agent.env
chown root:digilux /etc/digilux/ota-agent.env

# ── Sudoers rule for service management ───────────────────────────────────────
echo "==> Adding sudoers rule for service management"
cat > /etc/sudoers.d/digilux-ota << 'EOF'
digilux ALL=(root) NOPASSWD: /bin/systemctl start digilux-controller
digilux ALL=(root) NOPASSWD: /bin/systemctl stop digilux-controller
digilux ALL=(root) NOPASSWD: /bin/systemctl kill digilux-controller
digilux ALL=(root) NOPASSWD: /bin/systemctl kill --signal=SIGUSR1 digilux-controller
digilux ALL=(root) NOPASSWD: /bin/systemctl kill --signal=SIGHUP digilux-controller
EOF
chmod 440 /etc/sudoers.d/digilux-ota

# ── Install and enable systemd service ────────────────────────────────────────
echo "==> Installing systemd service"
cp "$SCRIPT_DIR/digilux-ota-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo ""
echo "OTA Agent installed and running."
echo ""
echo "Check status: systemctl status $SERVICE_NAME"
echo "View logs:    journalctl -u $SERVICE_NAME -f"
echo ""
echo "NEXT STEP: Copy the OTA signing public key to /etc/digilux/ota-signing.pub"
echo "           (Get it from: aws secretsmanager get-secret-value --secret-id digilux-ota-signing-key)"
