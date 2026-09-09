# Digilux OTA — Controller Integration Guide

**Audience:** Nitin / Embedded Linux team
**Version:** 1.0
**Last updated:** 2026-09-09

This guide covers everything the controller side needs to receive, verify, decrypt, apply, and report OTA updates delivered by the Digilux OTA system.

---

## Overview

The OTA system delivers artifacts via a secure pipeline:

```
Admin uploads .tar (contains manifest.json + all update files)
    ↓
Server validates tar + manifest.json
Server encrypts entire tar (AES-256-GCM, unique key per artifact)
Server signs the encrypted payload (ECDSA P-256)
Server stores payload.enc + signature.sig in S3
    ↓
User app triggers update → controller receives MQTT payload
MQTT payload contains: downloadUrl, signatureUrl, sha256, signature,
                        aesKey (unique per artifact), aesIv, encrypted: true
    ↓
Controller: verify signature → decrypt → SHA256 check → untar → apply
Controller: restart service → verify new version running
Controller: report SUCCESS or FAILED + rollback via MQTT
```

**Security properties:**
- Artifact is AES-256-GCM encrypted — unreadable without the key
- Key is delivered per-request over TLS — never stored on device at rest
- ECDSA signature covers the SHA256 of the original tar — tamper detection
- GCM authentication tag confirms no byte was changed in transit
- On verification failure at any step — abort, never apply

---

## Prerequisites

### 1. Get the Public Key From Mahesh (One-Time)

Mahesh runs this command and sends you `ota_public_key.pem`:

```bash
aws secretsmanager get-secret-value \
  --secret-id digilux-ota-signing-key \
  --region ap-south-1 \
  --query 'SecretString' --output text | \
  python3 -c "
import sys, json
from cryptography.hazmat.primitives import serialization
data = json.loads(sys.stdin.read())
priv = serialization.load_pem_private_key(data['privateKey'].encode(), password=None)
pub  = priv.public_key()
print(pub.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo
).decode())
" > ota_public_key.pem
```

**Bake this file into the controller at build time:**

```
/opt/digilux/ota_public_key.pem
```

This is the only thing Mahesh gives you. The AES decryption key is unique per artifact and delivered at download time — it is never stored anywhere on the device.

### 2. Install Python3 Dependency

```bash
pip3 install cryptography
```

Everything else used is Python3 standard library.

### 3. Directory Structure on the Controller

```
/opt/digilux/
├── ota_public_key.pem          ← baked in at build time
├── ota_apply.py                ← OTA client script (this guide)
├── post_install.sh             ← post-install verification + reporting
├── app/                        ← install directory (tar extracted here)
│   ├── manifest.json
│   ├── running_version.txt     ← YOUR APP must write this on every startup
│   └── ... (your update files)
└── app_backup/                 ← auto-created during update, deleted on success
/etc/digilux/
└── device_id                   ← UUID of this device (set at provisioning time)
/var/log/
└── digilux_ota.log             ← OTA operation log
```

---

## The MQTT Payload

When a user triggers an OTA update from the app, your controller receives this on:

```
Topic: iot/device/{deviceId}/ota
```

```json
{
  "operationType": 1,
  "packageName":   "controller-app",
  "version":       "2.5.0",
  "downloadUrl":   "https://s3.ap-south-1.amazonaws.com/.../payload.enc?...",
  "signatureUrl":  "https://s3.ap-south-1.amazonaws.com/.../signature.sig?...",
  "sha256":        "a1b2c3d4e5f6...",
  "signature":     "<base64 ECDSA-P256 signature>",
  "encrypted":     true,
  "aesKey":        "<base64 AES-256 key — unique to this artifact>",
  "aesIv":         "<base64 96-bit GCM nonce>",
  "size":          4096000,
  "expiresAt":     "2026-09-09T11:00:00Z",
  "initiatedBy":   "USER_APP",
  "rollback":      true
}
```

**Important:** `aesKey` is only valid for the duration of the pre-signed URL (1–48 hours depending on file size). Apply the update immediately after receiving this payload.

---

## OTA Client Script (`/opt/digilux/ota_apply.py`)

```python
#!/usr/bin/env python3
"""
Digilux OTA client — embedded Linux controller.
Receives MQTT payload as JSON argument, applies the update.

Usage:
    python3 /opt/digilux/ota_apply.py '<mqtt_payload_json>'

Exit codes:
    0 — update applied and verified successfully
    1 — update failed (check stdout for JSON error)
"""
import base64
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import urllib.request

from cryptography.exceptions import InvalidTag, InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/digilux_ota.log"),
    ],
)
log = logging.getLogger("digilux.ota")

PUBLIC_KEY_PATH     = "/opt/digilux/ota_public_key.pem"
INSTALL_DIR         = "/opt/digilux/app"
POST_INSTALL_SCRIPT = "/opt/digilux/post_install.sh"


def apply_update(payload: dict) -> dict:
    """
    Full OTA pipeline. Returns manifest dict on success.
    Raises RuntimeError with a descriptive message on any failure.
    The caller (post_install.sh or MQTT listener) handles reporting.
    """
    if not payload.get("encrypted"):
        raise RuntimeError("Artifact is not encrypted — refusing to apply unencrypted payload.")

    download_url  = payload["downloadUrl"]
    signature_b64 = payload["signature"]
    sha256_hex    = payload["sha256"]
    aes_key_b64   = payload["aesKey"]
    aes_iv_b64    = payload["aesIv"]
    version       = payload["version"]

    # ── Step 1: Download encrypted artifact ──────────────────────────────────
    log.info(f"[1/6] Downloading encrypted artifact for v{version} ...")
    req = urllib.request.Request(download_url, headers={"User-Agent": "DigiluxOTA/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        enc_bytes = resp.read()
    log.info(f"[1/6] Downloaded {len(enc_bytes):,} bytes")

    # ── Step 2: Verify ECDSA signature ───────────────────────────────────────
    # The signature was created by signing the SHA256 hex string of the raw tar.
    # Verify BEFORE decrypting — reject tampered artifacts immediately.
    log.info("[2/6] Verifying ECDSA signature ...")
    with open(PUBLIC_KEY_PATH, "rb") as f:
        pub_key = serialization.load_pem_public_key(f.read())

    sig_bytes = base64.b64decode(signature_b64)
    try:
        pub_key.verify(sig_bytes, sha256_hex.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise RuntimeError(
            "ECDSA signature verification FAILED — "
            "artifact origin cannot be confirmed. Aborting."
        )
    log.info("[2/6] Signature verified OK")

    # ── Step 3: AES-256-GCM decrypt ──────────────────────────────────────────
    # AESGCM.decrypt() verifies the GCM authentication tag automatically.
    # If any byte was changed in transit, InvalidTag is raised before we
    # see any plaintext — the corrupted content is never exposed.
    log.info("[3/6] Decrypting artifact (AES-256-GCM) ...")
    aes_key = base64.b64decode(aes_key_b64)
    aes_iv  = base64.b64decode(aes_iv_b64)
    aesgcm  = AESGCM(aes_key)
    try:
        raw_tar_bytes = aesgcm.decrypt(aes_iv, enc_bytes, None)
    except InvalidTag:
        raise RuntimeError(
            "AES-GCM tag verification FAILED — "
            "artifact is corrupted or tampered in transit. Aborting."
        )
    finally:
        # Zero out key material from memory immediately
        aes_key = b"\x00" * len(aes_key)
        del aes_key, aes_iv, enc_bytes
    log.info("[3/6] Decryption OK — GCM integrity tag passed")

    # ── Step 4: SHA256 verify decrypted tar ──────────────────────────────────
    log.info("[4/6] Verifying SHA256 of decrypted tar ...")
    actual_sha256 = hashlib.sha256(raw_tar_bytes).hexdigest()
    if actual_sha256.lower() != sha256_hex.lower():
        raise RuntimeError(
            f"SHA256 mismatch — expected {sha256_hex[:16]}..., "
            f"got {actual_sha256[:16]}... Aborting."
        )
    log.info("[4/6] SHA256 OK")

    # ── Step 5: Validate manifest.json ───────────────────────────────────────
    log.info("[5/6] Validating manifest.json ...")
    with tarfile.open(fileobj=io.BytesIO(raw_tar_bytes), mode="r:*") as tf:
        names = tf.getnames()
        if "manifest.json" not in names:
            raise RuntimeError("manifest.json missing from tar archive. Aborting.")

        manifest = json.loads(tf.extractfile("manifest.json").read().decode())
        for field in ("packageName", "version", "files"):
            if field not in manifest:
                raise RuntimeError(f"manifest.json missing required field: '{field}'. Aborting.")

        log.info(json.dumps({
            "msg":         "manifest_validated",
            "packageName": manifest["packageName"],
            "version":     manifest["version"],
            "files":       [f["name"] for f in manifest.get("files", [])],
        }))

        # ── Step 6: Extract to install directory ──────────────────────────────
        log.info(f"[6/6] Extracting to {INSTALL_DIR} ...")
        os.makedirs(INSTALL_DIR, exist_ok=True)
        tf.extractall(path=INSTALL_DIR)

    log.info(f"Update {manifest['packageName']} v{manifest['version']} extracted successfully")
    return manifest


if __name__ == "__main__":
    try:
        payload  = json.loads(sys.argv[1])
        manifest = apply_update(payload)
        print(json.dumps({"status": "extracted", "version": manifest["version"]}))
        sys.exit(0)
    except (IndexError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "failed", "error": f"Invalid payload argument: {e}"}))
        sys.exit(1)
    except Exception as e:
        log.error(f"OTA apply FAILED: {e}")
        print(json.dumps({"status": "failed", "error": str(e)}))
        sys.exit(1)
```

---

## Post-Install Script (`/opt/digilux/post_install.sh`)

This script runs after extraction. It restarts the service, verifies the new version is actually running, and reports the result back to the Digilux server via MQTT.

```bash
#!/bin/bash
# Digilux OTA post-install verification and reporting
# Usage: post_install.sh <expected_version>
set -euo pipefail

EXPECTED_VERSION="$1"
SERVICE_NAME="digilux-controller"           # systemd service name
INSTALL_DIR="/opt/digilux/app"
BACKUP_DIR="/opt/digilux/app_backup"
VERSION_FILE="$INSTALL_DIR/running_version.txt"
DEVICE_ID_FILE="/etc/digilux/device_id"
MQTT_BROKER="localhost"
HEALTH_TIMEOUT=60                            # seconds to wait for service to start
PACKAGE_NAME="controller-app"

DEVICE_ID=$(cat "$DEVICE_ID_FILE")
REGISTER_TOPIC="iot/device/${DEVICE_ID}/ota/register"

log() {
    echo "[$(date -u +%FT%TZ)] $*" | tee -a /var/log/digilux_ota.log
}

mqtt_report() {
    local status="$1"
    local installed_version="$2"
    local error_msg="${3:-}"

    mosquitto_pub -h "$MQTT_BROKER" -t "$REGISTER_TOPIC" -q 1 -m "$(cat <<EOF
{
  "deviceId":               "$DEVICE_ID",
  "globalInstalledVersion": "$installed_version",
  "package": {
    "name":             "$PACKAGE_NAME",
    "installedVersion": "$installed_version"
  },
  "otaStatus":  "$status",
  "otaVersion": "$EXPECTED_VERSION",
  "otaError":   "$error_msg"
}
EOF
)"
    log "Reported $status to MQTT (installedVersion=$installed_version)"
}

# ── Step 1: Backup current install ───────────────────────────────────────────
log "Backing up current install to $BACKUP_DIR ..."
rm -rf "$BACKUP_DIR"
cp -a "$INSTALL_DIR" "$BACKUP_DIR"

# ── Step 2: Clear version file so we can detect when the new version writes it
rm -f "$VERSION_FILE"

# ── Step 3: Restart service ───────────────────────────────────────────────────
log "Restarting $SERVICE_NAME ..."
systemctl restart "$SERVICE_NAME"

# ── Step 4: Wait for new version to confirm it is running ────────────────────
log "Waiting up to ${HEALTH_TIMEOUT}s for version $EXPECTED_VERSION to start ..."
ELAPSED=0
while [ $ELAPSED -lt $HEALTH_TIMEOUT ]; do
    if [ -f "$VERSION_FILE" ]; then
        RUNNING_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
        if [ "$RUNNING_VERSION" = "$EXPECTED_VERSION" ]; then
            log "Service confirmed running version $RUNNING_VERSION"
            break
        fi
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

# ── Step 5: Verify or rollback ────────────────────────────────────────────────
RUNNING_VERSION=$(cat "$VERSION_FILE" 2>/dev/null | tr -d '[:space:]' || echo "")

if [ "$RUNNING_VERSION" != "$EXPECTED_VERSION" ]; then
    log "ERROR: Expected v$EXPECTED_VERSION but got '${RUNNING_VERSION:-none}'. Rolling back ..."

    systemctl stop "$SERVICE_NAME" || true
    rm -rf "$INSTALL_DIR"
    cp -a "$BACKUP_DIR" "$INSTALL_DIR"
    systemctl restart "$SERVICE_NAME"

    sleep 5
    ROLLBACK_VER=$(cat "$VERSION_FILE" 2>/dev/null | tr -d '[:space:]' || echo "unknown")
    log "Rollback complete. Running: $ROLLBACK_VER"

    mqtt_report "FAILED" "$ROLLBACK_VER" \
        "v$EXPECTED_VERSION failed to start within ${HEALTH_TIMEOUT}s — rolled back to $ROLLBACK_VER"
    exit 1
fi

# ── Step 6: Report success ────────────────────────────────────────────────────
mqtt_report "SUCCESS" "$EXPECTED_VERSION"
rm -rf "$BACKUP_DIR"
log "OTA update to v$EXPECTED_VERSION complete."
exit 0
```

Make it executable:

```bash
chmod +x /opt/digilux/post_install.sh
```

---

## MQTT Listener Integration

In your existing MQTT listener, add a handler for the OTA topic:

```python
import json
import subprocess
import logging

log = logging.getLogger("digilux.mqtt")
OTA_APPLY_SCRIPT = "/opt/digilux/ota_apply.py"
POST_INSTALL_SCRIPT = "/opt/digilux/post_install.sh"


def on_ota_message(client, userdata, msg):
    """Called when a message arrives on iot/device/{deviceId}/ota"""
    try:
        payload = json.loads(msg.payload.decode())
    except json.JSONDecodeError as e:
        log.error(f"OTA: invalid JSON payload — {e}")
        return

    if not payload.get("encrypted"):
        log.warning("OTA: received unencrypted payload — ignoring")
        return

    version = payload.get("version", "unknown")
    log.info(f"OTA: starting update to v{version}")

    # Step 1: Extract the artifact
    result = subprocess.run(
        ["python3", OTA_APPLY_SCRIPT, json.dumps(payload)],
        capture_output=True, text=True, timeout=600,
    )

    try:
        outcome = json.loads(result.stdout)
    except Exception:
        outcome = {"status": "failed", "error": result.stderr or "no output"}

    if result.returncode != 0:
        log.error(f"OTA: extraction failed — {outcome.get('error')}")
        # post_install.sh was not reached — report directly via MQTT
        # (device_register lambda will clear pendingJobId)
        client.publish(
            f"iot/device/{payload.get('deviceId')}/ota/register",
            json.dumps({
                "deviceId":               payload.get("deviceId"),
                "globalInstalledVersion": _read_current_version(),
                "package": {
                    "name":             "controller-app",
                    "installedVersion": _read_current_version(),
                },
                "otaStatus":  "FAILED",
                "otaVersion": version,
                "otaError":   outcome.get("error", "extraction failed"),
            }),
            qos=1,
        )
        return

    # Step 2: Run post-install (restart service, verify, report)
    log.info(f"OTA: extraction OK — running post-install for v{version}")
    subprocess.run(
        [POST_INSTALL_SCRIPT, version],
        timeout=120,
    )
    # post_install.sh handles all MQTT reporting from here


def _read_current_version() -> str:
    try:
        return open("/opt/digilux/app/running_version.txt").read().strip()
    except FileNotFoundError:
        return "unknown"
```

---

## One Mandatory Requirement: Version File

Your controller app **must** write its version to a file on every startup:

```java
// Java (controller.jar)
import java.nio.file.*;
Files.writeString(Path.of("/opt/digilux/app/running_version.txt"), APP_VERSION);
```

```python
# Python
with open("/opt/digilux/app/running_version.txt", "w") as f:
    f.write(APP_VERSION)
```

```bash
# Shell (start script)
echo "2.5.0" > /opt/digilux/app/running_version.txt
exec java -jar controller.jar
```

This file is what `post_install.sh` reads to confirm the new version started. Without it, success/failure cannot be determined and the update is always treated as failed.

---

## MQTT Report Payloads

### Success
```json
{
  "deviceId":               "edb39bba-baf1-4700-968c-a42228e53aa0",
  "globalInstalledVersion": "2.5.0",
  "package": {
    "name":             "controller-app",
    "installedVersion": "2.5.0"
  },
  "otaStatus":  "SUCCESS",
  "otaVersion": "2.5.0",
  "otaError":   ""
}
```

### Failure (with automatic rollback)
```json
{
  "deviceId":               "edb39bba-baf1-4700-968c-a42228e53aa0",
  "globalInstalledVersion": "2.4.0",
  "package": {
    "name":             "controller-app",
    "installedVersion": "2.4.0"
  },
  "otaStatus":  "FAILED",
  "otaVersion": "2.5.0",
  "otaError":   "v2.5.0 failed to start within 60s — rolled back to 2.4.0"
}
```

On both outcomes `pendingJobId` is cleared on the server, so the device is never blocked from future updates.

---

## Security Verification Order (Do Not Change)

The script enforces this order — skipping or reordering any step is a security risk:

```
1. VERIFY ECDSA signature    ← confirms artifact came from Digilux server
2. DECRYPT AES-256-GCM       ← GCM tag confirms no byte changed in transit
3. SHA256 check              ← confirms decrypted content matches what server stored
4. Validate manifest.json    ← confirms structure before touching filesystem
5. Extract to INSTALL_DIR    ← only if all 4 above pass
6. Restart + verify version  ← confirms new code is actually running
7. Report via MQTT           ← clears pendingJobId on server
```

---

## Quick Reference

| Item | Value |
|------|-------|
| MQTT OTA topic | `iot/device/{deviceId}/ota` |
| MQTT register topic | `iot/device/{deviceId}/ota/register` |
| Public key path | `/opt/digilux/ota_public_key.pem` |
| Install directory | `/opt/digilux/app/` |
| Version file | `/opt/digilux/app/running_version.txt` |
| OTA log | `/var/log/digilux_ota.log` |
| Python dependency | `pip3 install cryptography` |
| Signing algorithm | ECDSA P-256 with SHA-256 |
| Encryption | AES-256-GCM, unique key per artifact |
| Key delivery | Via MQTT payload over TLS — never stored on device |

---

## Contact

For questions on the OTA server side, API endpoints, or key rotation contact Mahesh.
