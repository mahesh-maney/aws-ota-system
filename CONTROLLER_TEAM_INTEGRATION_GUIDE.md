# Digilux OTA Agent — Controller Team Integration Guide

## Overview

The Digilux OTA system delivers encrypted, signed firmware updates to the Network Controller.
This guide covers every API call, verification step, and install rule the OTA agent must implement.

The flow is consent-gated: the device owner must explicitly accept an update via the Digilux app
before the controller downloads or installs anything.

```
Admin ──► Digilux Cloud: Upload artifact + create deployment
Digilux Cloud ──► Digilux Cloud: Encrypt (AES-256-GCM) + sign (ECDSA)
Digilux Cloud ──► Digilux Cloud: Status: AWAITING_CONSENT
User App ──► Digilux Cloud: POST /consent (accepted: true)
Digilux Cloud ──► Digilux Cloud: IoT Job created for this device
Controller ──► Digilux Cloud: GET /device/available-updates
Digilux Cloud ──► Controller: availableVersion + releaseNotes
Controller ──► Digilux Cloud: POST /consent (accepted: true)
Digilux Cloud ──► Controller: downloadUrl + aesKey + aesIv
Controller ──► Digilux Cloud: Download encrypted artifact
Controller ──► Controller: Verify GCM tag + SHA256 + ECDSA
Controller ──► Controller: Extract tar, install files by type
Controller ──► Digilux Cloud: Report IoT Job SUCCESS / FAILED
```

---

## Authentication

All OTA endpoints require a valid Cognito access token from the **device user pool**.
The token is obtained via OAuth 2.0 PKCE flow and passed as a `Bearer` token on every request.

| Parameter | Value |
|---|---|
| Cognito User Pool | `ap-south-1_h1o8s7257` |
| App Client ID | `q7189jitfkk4ttesepkgls491` |
| Required scopes | `smarthome_server/read smarthome_server/write` |
| Token type | Access token (not ID token) |
| Header | `Authorization: Bearer <access_token>` |

> **Note:** Plain `USER_PASSWORD_AUTH` tokens lack the required OAuth scopes and will return `401`.
> The token must be obtained via the PKCE authorization code flow through the Cognito Hosted UI.

---

## Step-by-Step OTA Flow

The OTA agent must implement the following steps in order.
Each step is a hard gate — a failure at any step means abort and report.

**Step 1 — Poll for available updates**

Call `GET /api/v1/ota/device/available-updates` periodically. Act on the `otaStatus` of each device entry:

- `REGISTERED` with `availableVersion` newer than `installedVersion` → proceed to Step 2.
- `JOB_ACTIVE` → a firmware job is already running or has failed. Display `activeJob.message` to the user. No new consent or download needed.
- `NOT_REGISTERED` → OTA agent not yet initialised. No action.
- Device absent from the `devices` array, or present with no `availableVersion` and no `activeJob` → up to date.

**Step 2 — Notify the user**

Display the update to the user in the app (version number + `releaseNotes`). The user must
explicitly accept before the agent proceeds. Do not download or install silently.

**Step 3 — Submit consent and receive download URL**

Call `POST /api/v1/ota/my/updates/consent` with `accepted: true`. The response contains the
presigned S3 `downloadUrl`, the `aesKey` (base64), and the `aesIv` (base64) needed to decrypt
the artifact. Store these in memory — do not persist to disk.

**Step 4 — Download the encrypted artifact**

HTTP GET the `downloadUrl`. The response body is a binary AES-256-GCM encrypted blob.
The URL is time-limited — begin the download immediately after Step 3.

**Step 5 — Verify the artifact**

Three checks must all pass (see Verification section for details):

1. AES-256-GCM decrypt using `aesKey` + `aesIv` — GCM tag failure = corrupted download, abort
2. SHA256 of decrypted bytes must match `sha256` from the consent response
3. ECDSA signature must verify against Digilux's public key

**Step 6 — Extract and verify per-file checksums**

Extract the tar. Read `manifest.json`. For each file entry verify `sha256` and `size` match the
extracted file. Any mismatch = abort, do not install partial files.

**Step 7 — Install files by type**

For each file in the manifest, look up `type` in the type table (see Manifest section) to
determine the install path. Copy the file to that path, replacing the existing one.

**Step 8 — Report result**

Update the IoT Job status to `SUCCEEDED` or `FAILED` with a reason.
Update `globalInstalledVersion` on the device record.

---

## API Reference

Base URL: `https://iot.digilux.co.in/api/v1`

All requests require `Authorization: Bearer <access_token>`.

---

### GET /ota/device/available-updates

Returns available updates for all devices owned by the authenticated user.

**Response 200 — update available**

```json
{
  "devices": [
    {
      "deviceId": "edb39bba-baf1-4700-968c-a42228e53aa0",
      "otaStatus": "REGISTERED",
      "package": "HomeAssistantUtility",
      "installedVersion": "4.4.0",
      "availableVersion": "4.5.0",
      "releaseNotes": "Improved Zigbee stability and reduced reconnect time."
    }
  ]
}
```

**Response 200 — job already active**

```json
{
  "devices": [
    {
      "deviceId": "edb39bba-baf1-4700-968c-a42228e53aa0",
      "otaStatus": "JOB_ACTIVE",
      "package": "HomeAssistantUtility",
      "installedVersion": "4.4.0",
      "activeJob": {
        "jobId": "digilux-ota-HomeAssistantUtility-4-5-0-1790146510",
        "status": "IN_PROGRESS",
        "version": "4.5.0",
        "message": "Your Firmware update ver 4.5.0 is in progress, please check after some time for status. Note: Please ensure the controller is Powered on."
      }
    }
  ]
}
```

**`otaStatus` values**

| Value | Meaning | Action |
|---|---|---|
| `REGISTERED` | Update available | Notify user, proceed to consent |
| `JOB_ACTIVE` | Job running or failed | Display `activeJob.message`, wait |
| `NOT_REGISTERED` | OTA agent not initialised | No action |

**`activeJob` fields** (present only when `otaStatus` is `JOB_ACTIVE`)

| Field | Type | Description |
|---|---|---|
| `jobId` | string | IoT Job identifier |
| `status` | string | `AWAITING_CONSENT` \| `QUEUED` \| `IN_PROGRESS` \| `FAILED` |
| `version` | string | Firmware version being applied |
| `message` | string | User-facing string — display as-is |

`AWAITING_CONSENT`, `QUEUED`, and `IN_PROGRESS` all produce the in-progress message.
`FAILED` produces the failure message. When the job reaches `SUCCEEDED`, `pendingJobId` is
cleared and the endpoint returns to normal update-check behaviour.

> `availableVersion` and `activeJob` are mutually exclusive in the same device entry.

---

### POST /ota/my/updates/consent

Submit user consent for an update. Call this only after the user has explicitly accepted.

**Request body**

```json
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "HomeAssistantUtility",
  "version":     "4.5.0",
  "accepted":    true
}
```

Set `accepted: false` if the user declines — the server records the decline and sends a notification.

**Response 200 (accepted: true)**

```json
{
  "jobId":       "deploy-abc123",
  "downloadUrl": "https://digilux-ota-artifacts.s3.amazonaws.com/enc/...",
  "aesKey":      "<base64-encoded AES-256 key>",
  "aesIv":       "<base64-encoded 96-bit GCM nonce>",
  "sha256":      "a3f8c2d1...",
  "signature":   "<base64-encoded ECDSA signature>"
}
```

The `downloadUrl` is time-limited. Begin the download immediately. Store `aesKey`, `aesIv`,
`sha256`, and `signature` in memory for the verification step.

**Response 200 (accepted: false)**

```json
{ "status": "DECLINED" }
```

---

## Manifest File Format

Every artifact tar must contain a `manifest.json` at its root. The manifest is the source of truth
for what files are in the package, where they go, and their expected checksums.

**Schema**

```json
{
  "packageName": "HomeAssistantUtility",
  "version":     "4.5.0",
  "deviceType":  "Network_controller_firmware",
  "files": [
    { "name": "ha-controller.jar",      "type": 1, "sha256": "a3f8c2...", "size": 2097152 },
    { "name": "zigbee-coordinator.bin", "type": 2, "sha256": "b1e4d7...", "size": 131072  },
    { "name": "app.config.yaml",        "type": 5, "sha256": "cc92a1...", "size": 4096    }
  ]
}
```

The `sha256` and `size` fields are computed and injected by the Digilux backend at upload time —
the uploader does not need to supply them. Every file listed in `files` must be present in the tar;
any mismatch causes the artifact to be rejected.

**File type enum**

| `type` | Meaning | Install path |
|---|---|---|
| `1` | Main firmware | `/opt/digilux/app/` |
| `2` | Zigbee coordinator firmware | `/opt/digilux/zigbee/` |
| `3` | Zigbee2MQTT firmware | `/opt/digilux/z2m/` |
| `4` | Zigbee stack firmware | `/opt/digilux/zigbee-stack/` |
| `5` | Config file | `/etc/digilux/` |
| `6` | Certificate | `/etc/digilux/certs/` |
| `7` | Database | `/var/digilux/db/` |
| `8` | Properties file | `/etc/digilux/props/` |

**Install pseudocode**

```python
TYPE_PATH = {
    1: "/opt/digilux/app/",
    2: "/opt/digilux/zigbee/",
    3: "/opt/digilux/z2m/",
    4: "/opt/digilux/zigbee-stack/",
    5: "/etc/digilux/",
    6: "/etc/digilux/certs/",
    7: "/var/digilux/db/",
    8: "/etc/digilux/props/",
}

for f in manifest["files"]:
    assert sha256(f["name"]) == f["sha256"]   # verify first
    assert size(f["name"])   == f["size"]
    copy(f["name"], TYPE_PATH[f["type"]] + f["name"])  # then install
```

> **Unknown type value:** If the agent encounters a `type` integer it does not recognise, skip that
> file and log a warning. Do not abort the entire install for an unrecognised type.

---

## Artifact Verification

Three independent checks must all pass before the agent installs anything. A failure at any check
means the downloaded bytes are discarded.

### Check 1 — AES-256-GCM decryption

The downloaded blob is AES-256-GCM ciphertext. Use the `aesKey` and `aesIv` from the consent
response to decrypt.

```python
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

aes_key   = base64.b64decode(consent_response["aesKey"])
aes_iv    = base64.b64decode(consent_response["aesIv"])
aesgcm    = AESGCM(aes_key)
plaintext = aesgcm.decrypt(aes_iv, ciphertext, None)  # raises if GCM tag fails
```

AES-GCM appends a 16-byte authentication tag to the ciphertext. If even one byte of the download
is missing or altered, `decrypt()` raises an `InvalidTag` exception — this automatically catches
truncated or corrupted downloads. No separate length check is needed.

### Check 2 — SHA256 integrity

After decryption, compute SHA256 of the plaintext bytes and compare against `sha256` from the
consent response.

```python
import hashlib

computed = hashlib.sha256(plaintext).hexdigest()
assert computed == consent_response["sha256"], "SHA256 mismatch — artifact corrupted"
```

### Check 3 — ECDSA signature

Verify the ECDSA signature from the job document against Digilux's public key. The signature
covers four fields joined by `|`: `version|size|packageName|sha256`. All four values are present
in the job document, so no extra API call is needed to reconstruct the signing input.

```python
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

# Digilux public key — pre-installed on the device
with open("/etc/digilux/ota_public_key.pem", "rb") as f:
    pub_key = serialization.load_pem_public_key(f.read())

# Reconstruct the exact signing input used by the Digilux backend
signing_input = (
    f"{job_doc['version']}|"
    f"{job_doc['artifact']['size']}|"
    f"{job_doc['packageName']}|"
    f"{job_doc['artifact']['sha256']}"
).encode()

sig_bytes = base64.b64decode(job_doc["artifact"]["signature"])
pub_key.verify(
    sig_bytes,
    signing_input,
    ec.ECDSA(hashes.SHA256())
)  # raises InvalidSignature if verification fails
```

This confirms the artifact was produced and signed by Digilux and that the version, size, package
name, and content hash are all authentic — no single field can be substituted without breaking
the signature.

### Summary

| Check | What it catches | Library call |
|---|---|---|
| GCM tag | Truncated / corrupted download | `AESGCM.decrypt()` |
| SHA256 | Plaintext integrity after decryption | `hashlib.sha256()` |
| ECDSA | Authenticity + version/size/package binding — signed by Digilux | `pub_key.verify()` |

---

## Error Handling and Abort Conditions

The OTA agent must treat these as hard aborts — discard all downloaded bytes and do not write
anything to the filesystem.

| Condition | When it occurs | Action |
|---|---|---|
| GCM `InvalidTag` on decrypt | Download truncated or corrupted | Discard bytes. Retry download once, then report FAILED. |
| SHA256 mismatch after decrypt | Plaintext does not match expected hash | Discard bytes. Report FAILED with reason `sha256_mismatch`. |
| ECDSA `InvalidSignature` | Artifact was not signed by Digilux | Discard bytes. Report FAILED with reason `signature_invalid`. Do not retry. |
| `manifest.json` missing from tar | Malformed artifact | Discard bytes. Report FAILED with reason `missing_manifest`. |
| Per-file SHA256 or size mismatch | File corrupted inside tar | Discard all extracted files. Report FAILED with reason `file_checksum_mismatch`. |
| Unknown `type` in manifest | New file type not yet in agent's table | Skip that file, log warning, continue install of other files. |
| Download URL expired (HTTP 403) | Download started too late after consent | Re-call `POST /consent` with the same parameters to obtain a fresh URL. |
| IoT Job already completed | Duplicate delivery | No-op. Log and exit. |

**Partial install protection:** The agent must not start overwriting production files until all
verifications pass (Steps 6 and 7 complete). Write to a staging directory first, then atomically
move each file to its final path.

**Reporting failures:** Always update the IoT Job status — even on abort — so the backend knows
the install did not succeed.

```json
{
  "status": "FAILED",
  "statusDetails": {
    "detailsMap": {
      "reason": "sha256_mismatch",
      "version": "4.5.0"
    }
  }
}
```

---

## Quick Reference

**Base URL:** `https://iot.digilux.co.in/api/v1`

| # | Step | Method | Path | Call when |
|---|---|---|---|---|
| 1 | Poll for updates | `GET` | `/ota/device/available-updates` | Periodic (every N minutes) |
| 2 | Submit consent | `POST` | `/ota/my/updates/consent` | User accepts in app |
| 3 | Download artifact | `GET` | `<downloadUrl from step 2>` | Immediately after step 2 |
| 4 | Report result | IoT Job | — | After install succeeds or fails |

**Consent request body**

```json
{ "deviceId": "…", "packageName": "…", "version": "…", "accepted": true }
```

**Verification sequence (must all pass)**

1. AES-256-GCM decrypt → `InvalidTag` = abort
2. SHA256 of plaintext == `sha256` from consent response
3. ECDSA verify signature: signing input is `version|size|packageName|sha256` (all from job doc), public key at `/etc/digilux/ota_public_key.pem`
4. Per-file SHA256 + size from `manifest.json` match extracted files

**File type install paths**

```
1 → /opt/digilux/app/          5 → /etc/digilux/
2 → /opt/digilux/zigbee/       6 → /etc/digilux/certs/
3 → /opt/digilux/z2m/          7 → /var/digilux/db/
4 → /opt/digilux/zigbee-stack/ 8 → /etc/digilux/props/
```
