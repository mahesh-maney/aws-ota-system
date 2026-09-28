# OTA Artifact Download and Decryption — Controller Integration Guide

> Last updated: 2026-09-25

The controller receives an IoT Job, downloads an encrypted firmware artifact from S3, decrypts it using a key embedded in the job document, verifies the signature, and reports the result back.

---

## Overview

The controller has three responsibilities when an OTA job arrives:

1. **Download** — fetch the encrypted firmware file from S3 using a time-limited presigned URL in the job document
2. **Decrypt** — decrypt the file using the AES-256-GCM key and IV provided in the job document
3. **Verify** — check the ECDSA signature and SHA256 checksum before installing anything

All three inputs (URL, key, signature) arrive together in the IoT Job document. The controller does not need to call any external service — everything it needs is already inside the job.

---

## How the system works — three phases

```
╔══════════════════════════════════════════════════════════════════════╗
║  PHASE 1 — UPLOAD  (done by Digilux admin, once per firmware build)  ║
╚══════════════════════════════════════════════════════════════════════╝

Admin UI
  │  PUT raw firmware file
  ▼
S3 (temporary slot)
  │  S3 event triggers
  ▼
artifact_processor Lambda  (runs inside AWS)
  ├── generates random AES-256 key
  ├── calls Key Server POST /wrap  ──►  Key Server (Digilux Lambda)
  │                                         encrypts AES key with master key
  │                                     ◄── returns wrappedDataKey
  ├── encrypts firmware with AES key
  ├── signs encrypted bytes with ECDSA private key
  └── stores in S3:
          enc/<uuid>.enc   (encrypted firmware)
          sig/<uuid>.sig   (ECDSA signature)
      stores in DynamoDB:
          wrappedDataKey, aesIv, sha256, signature, encS3Key


╔══════════════════════════════════════════════════════════════════════╗
║  PHASE 2 — CONSENT  (triggered when device owner taps YES in app)    ║
╚══════════════════════════════════════════════════════════════════════╝

user_consent Lambda
  ├── calls Key Server POST /unwrap  ──►  Key Server
  │                                           decrypts wrappedDataKey
  │                                       ◄── returns plaintext AES key
  ├── generates presigned S3 URL for enc/<uuid>.enc
  └── creates IoT Job with document:
          presignedUrl  (time-limited S3 URL)
          dataKey       (plaintext AES key — 32 bytes, base64)
          iv            (GCM nonce — 12 bytes, base64)
          sha256        (checksum of the DECRYPTED plaintext)
          signature     (ECDSA over the ENCRYPTED bytes)
          size          (byte count of the encrypted file)

IoT Core delivers the job document to the device over TLS/MQTT


╔══════════════════════════════════════════════════════════════════════╗
║  PHASE 3 — DEVICE INSTALL  (controller does this)                    ║
╚══════════════════════════════════════════════════════════════════════╝

Controller
  ├── 1. Receive job document (MQTT, already delivered by IoT Core)
  ├── 2. Download encrypted file from presignedUrl (plain HTTPS GET)
  ├── 3. Verify ECDSA signature over encrypted bytes
  ├── 4. Decrypt with AES-256-GCM using dataKey + iv
  ├── 5. Verify SHA256 of decrypted plaintext
  ├── 6. Unpack tar, verify per-file checksums from manifest.json
  ├── 7. Install files to paths in manifest
  └── 8. Report SUCCEEDED or FAILED to IoT Core
```

The controller only participates in Phase 3. It never calls the Key Server — the AES key arrives pre-unwrapped inside the IoT Job document.

---

## Step-by-step: what the controller does

### Step 1 — Receive the IoT Job

AWS IoT Core delivers the job document over the device's existing MQTT connection (TLS-encrypted). The OTA agent subscribes to the standard IoT Jobs topic and receives the document as a JSON payload. No polling required — IoT pushes it.

Subscribe to: `$aws/things/<thingName>/jobs/notify`

### Step 2 — Download the encrypted artifact

Use `artifact.presignedUrl` from the job document to download the file over HTTPS. This is a standard S3 GET — no AWS credentials needed, the URL is self-authenticating. The URL expires (typically 24 hours), so download promptly.

- Stream to disk or buffer in memory depending on file size
- Do **not** decompress or process bytes during download
- Verify that bytes received == `artifact.size` after download completes

### Step 3 — Verify the ECDSA signature

Verify the signature **before decrypting**. This confirms the artifact was produced by Digilux and has not been tampered with in transit.

```
signature     = base64_decode(artifact.signature)
public_key    = load_digilux_ecdsa_p256_public_key()  // bundled with controller firmware
encrypted_bytes = the raw bytes downloaded in Step 2

verify_ecdsa_p256_sha256(public_key, encrypted_bytes, signature)
// FAIL → report REJECTED, errorCode 10003 (SIGNATURE_VERIFICATION_FAILED)
```

The Digilux ECDSA-P256 public key is provided separately and must be compiled into the controller firmware or stored in secure flash. Contact Digilux to obtain it.

### Step 4 — Decrypt the artifact

The artifact is encrypted with AES-256-GCM. All inputs come from the job document:

```
key       = base64_decode(artifact.dataKey)   // 32 bytes — AES-256
iv        = base64_decode(artifact.iv)         // 12 bytes — GCM nonce

plaintext = aes_256_gcm_decrypt(
                ciphertext = encrypted_bytes,
                key        = key,
                iv         = iv
            )
// AES-GCM appends a 16-byte authentication tag to the ciphertext.
// A correct library checks this tag automatically during decryption.
// Tag failure → report REJECTED, errorCode 10004 (CHECKSUM_MISMATCH)
```

### Step 5 — Verify the SHA256 checksum

After decryption, compute SHA256 of the plaintext and compare with `artifact.sha256`:

```
computed = sha256(plaintext)
expected = artifact.sha256   // hex string, e.g. "a3f1c9e2b7d4..."

if computed != expected:
    report REJECTED, errorCode 10004 (CHECKSUM_MISMATCH)
```

### Step 6 — Unpack and install

The decrypted plaintext is a `.tar` archive. Extract it. It contains:

- `manifest.json` — lists every file with its destination path, type enum, and SHA256
- The firmware payload files

Verify each file's SHA256 against `manifest.json` **before writing to disk**. Install files to the paths specified by the type enum (see operationType table below). If any file fails its checksum, abort the entire install and report REJECTED with errorCode 10105 (PACKAGE_CORRUPT).

### Step 7 — Report the result

After install completes (or fails), publish to the IoT status topic:

```
Topic:   iot/device/<deviceId>/ota/status

Success:
{
  "jobId":       "<jobId>",
  "deviceId":    "<deviceId>",
  "thingName":   "<thingName>",
  "status":      "SUCCEEDED",
  "progress":    100,
  "packageName": "<packageName>",
  "version":     "<version>"
}

Failure:
{
  "jobId":       "<jobId>",
  "deviceId":    "<deviceId>",
  "thingName":   "<thingName>",
  "status":      "REJECTED",
  "progress":    <last known %>,
  "packageName": "<packageName>",
  "version":     "<version>",
  "statusDetails": {
    "errorCode": 10003
  }
}
```

---

## Job document reference

Full example of what IoT Core delivers to the controller:

```json
{
  "operationType": 1,
  "packageName":   "HomeAssistantUtility",
  "version":       "4.6.0",
  "mandatory":     true,
  "rollback":      false,
  "artifact": {
    "presignedUrl": "https://s3.ap-south-1.amazonaws.com/digilux-ota-artifacts/enc/f3a9c1e2-...?X-Amz-...",
    "sha256":       "a3f1c9e2b7d4a8c1f2e3d4b5a6c7d8e9...",
    "signature":    "MEUCIQDxyz...",
    "size":         2097152,
    "dataKey":      "base64encodedAES256key32bytes====",
    "iv":           "base64encodedGCMnonce12b"
  }
}
```

### Field reference

| Field | Type | What the controller does with it |
|---|---|---|
| `operationType` | integer | Determines install destination — see table below |
| `packageName` | string | For logging/audit only |
| `version` | string | For logging/audit only |
| `mandatory` | boolean | Always `true` — update cannot be skipped |
| `rollback` | boolean | `true` = rollback deployment; install the same way as normal |
| `artifact.presignedUrl` | string | HTTPS URL to download the encrypted file — no credentials needed |
| `artifact.sha256` | string | SHA256 hex of the **decrypted** plaintext — verify **after** decryption |
| `artifact.signature` | string | Base64 ECDSA-P256 signature over the **encrypted** bytes — verify **before** decryption |
| `artifact.size` | integer | Byte count of the encrypted download — verify completeness |
| `artifact.dataKey` | string | Base64 AES-256 key (32 bytes) — input to decrypt |
| `artifact.iv` | string | Base64 GCM nonce (12 bytes) — input to decrypt |

### operationType values

| Value | Package type | Install path |
|---|---|---|
| 1 | Main firmware | `/opt/digilux/app/` |
| 2 | Zigbee firmware | `/opt/digilux/zigbee/` |
| 3 | Z2M firmware | `/opt/digilux/z2m/` |
| 4 | Zigbee stack firmware | `/opt/digilux/zigbee-stack/` |
| 5 | Config files | `/etc/digilux/` |
| 6 | Certificates | system cert store |
| 7 | Database | app data directory |
| 8 | Properties | `/etc/digilux/` |

---

## Error handling

| Step | Failure | Error code to report | Action |
|---|---|---|---|
| Download | HTTP error or timeout | — | Retry up to 3 times with exponential backoff. If still failing, report `FAILED` with `error: "DOWNLOAD_FAILED"` |
| Download | Received bytes != `artifact.size` | 10104 (`DOWNLOAD_FAILED`) | Discard bytes, retry once, then report REJECTED |
| Signature verify | Signature does not match | 10003 (`SIGNATURE_VERIFICATION_FAILED`) | Discard bytes immediately. Do **not** retry — this may indicate tampering. Report REJECTED |
| Decryption | GCM tag authentication failure | 10004 (`CHECKSUM_MISMATCH`) | Discard bytes. Report REJECTED |
| SHA256 check | Hash mismatch after decrypt | 10004 (`CHECKSUM_MISMATCH`) | Discard plaintext. Report REJECTED |
| manifest.json missing | Not found in tar | 10203 (`INVALID_JOB_DOCUMENT`) | Report REJECTED |
| Per-file SHA256 mismatch | File corrupt inside tar | 10105 (`PACKAGE_CORRUPT`) | Discard all extracted files. Report REJECTED |
| Insufficient storage | Not enough disk space | 10103 (`INSUFFICIENT_STORAGE`) | Report REJECTED before attempting download |

**Rule: always report a final status (SUCCEEDED or REJECTED/FAILED), even if you abort mid-install.** The backend clears the device's pending job when it receives a terminal status. If you do not report, the device stays blocked and will not receive future updates until the job times out.

---

## Reporting back — status update

Publish to MQTT topic `iot/device/<deviceId>/ota/status` at the following points:

| When | `status` value | `progress` |
|---|---|---|
| Download starts | `IN_PROGRESS` | 0 |
| Download complete | `IN_PROGRESS` | 33 |
| Decryption + verify complete | `IN_PROGRESS` | 66 |
| Install complete | `IN_PROGRESS` | 99 |
| All done, reboot scheduled | `SUCCEEDED` | 100 |
| Any unrecoverable failure | `REJECTED` | last known % |

Send at least one `IN_PROGRESS` report so the admin UI shows the deployment is active. Send the final `SUCCEEDED` or `REJECTED` so the backend closes the job and unblocks the device.

For `REJECTED`, include the error code in `statusDetails.errorCode` using the values in the Error handling table above. This populates the rejection reason in the admin UI and audit logs.
