# Digilux OTA (Over-The-Air) Update System — Integration Guide

**Version:** 1.3
**Date:** 2026-08-04
**Audience:** Integration / QA Team
**Base URL:** `https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome`

---

## 1. Overview

The Digilux OTA system supports two update modes:

**Admin-initiated:** Ops team pushes mandatory updates to any device or device group. The device cannot decline once an admin deployment is created.

**User-initiated:** The homeowner opens the Flutter app, sees an available update, gives explicit consent, and the device downloads and installs the update over HTTPS. The admin must have published the package first — users only control *when* to apply it, not *what* is available.

### Update Types Supported

| Type | Description |
|---|---|
| `CONTROLLER_FIRMWARE` | Core OS-level firmware for the controller |
| `CONTROLLER_APP` | Main controller application (Python service) |
| `DRIVER` | Hardware/peripheral drivers |
| `ZIGBEE_DEVICE` | Firmware for Zigbee-connected end devices (via zigbee2mqtt) |
| `CONFIG` | Configuration file updates |
| `RULES` | Automation rules updates |

### Rollout Stages

| Stage | Rate | Use Case |
|---|---|---|
| `CANARY` | Max 2 devices/min | First 5 devices — smoke test |
| `BETA` | Exponential from 2/min | ~10% of fleet |
| `PRODUCTION` | Exponential from 5/min | Full fleet |

---

## 2. Architecture

```
Admin (API)                  AWS Cloud                        Controller Device
    │                            │                                    │
    │  POST /packages/upload-url │                                    │
    │──────────────────────────>│                                    │
    │  ← { uploadUrl, s3Key }   │                                    │
    │                            │                                    │
    │  PUT <uploadUrl> (binary)  │                                    │
    │──────────────────────────>│ S3                                 │
    │                            │──> S3 Event                        │
    │                            │──> artifact_processor Lambda       │
    │                            │    (SHA256 + ECDSA sign)           │
    │                            │    Package: PENDING → ACTIVE       │
    │                            │                                    │
    │  POST /deployments         │                                    │
    │──────────────────────────>│                                    │
    │  ← { jobId, QUEUED }      │                                    │
    │                            │──> IoT Job created                 │
    │                            │──────────────────────────────────>│
    │                            │    Job document delivered          │
    │                            │                                    │ Download artifact
    │                            │                                    │ Verify SHA256 + ECDSA
    │                            │                                    │ Backup current version
    │                            │                                    │ Install new version
    │                            │                                    │ Health check
    │                            │                                    │ Rollback if failed
    │                            │<──────────────────────────────────│
    │                            │    SUCCEEDED / FAILED + detail     │
    │                            │──> status_handler Lambda           │
    │                            │    Update job status in DynamoDB   │
    │                            │    Update device inventory         │
    │  GET /deployments/{jobId}  │                                    │
    │──────────────────────────>│                                    │
    │  ← { SUCCEEDED, deviceStatuses } │                             │
```

---

## 3. Authentication

### Admin endpoints (Sections 4.1–4.7)

Require a **Cognito ID Token** from the `admin` group.

```http
Authorization: <Cognito ID Token>
```

Non-admin tokens receive `HTTP 403 — Admin access required`.

### End-user endpoints (Section 4.8–4.10)

Require any valid **Cognito ID Token** — no admin group needed. The `userId` is extracted from the JWT `sub` claim to determine device ownership.

```http
Authorization: <Cognito ID Token>
```

Users can only see and update devices registered under their own `userId`.

---

## 4. API Reference

### 4.1 List Packages

```
GET /api/v1/ota/packages
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `status` | `ACTIVE` | Filter by status: `ACTIVE`, `PENDING` |
| `packageName` | — | List all versions of a specific package |

**Response `200`:**
```json
{
  "packages": [
    {
      "packageName": "controller-app",
      "version": "4.0.0",
      "packageType": "CONTROLLER_APP",
      "status": "ACTIVE",
      "artifactSize": 2097810,
      "releaseNotes": "Zigbee 3.0 support",
      "createdBy": "admin@example.com",
      "createdAt": 1785475706560
    }
  ],
  "count": 1
}
```

---

### 4.2 Get Upload URL

```
POST /api/v1/ota/packages/upload-url
```

**Request body:**

```json
{
  "packageName": "controller-app",
  "version": "4.0.0",
  "packageType": "CONTROLLER_APP",
  "fileName": "controller-app-4.0.0.tar.gz",
  "releaseNotes": "Zigbee 3.0 support, improved stability",
  "compatibleModels": ["DGX-1000"],
  "minHwRevision": "1.0",
  "dependsOn": {},
  "incompatibleWith": {}
}
```

| Field | Required | Description |
|---|---|---|
| `packageName` | Yes | Identifier for the software component |
| `version` | Yes | Semantic version (e.g. `4.0.0`) |
| `packageType` | Yes | One of the 6 supported types |
| `fileName` | No | Defaults to `artifact.bin` |
| `releaseNotes` | No | Human-readable changelog |
| `compatibleModels` | No | e.g. `["DGX-1000"]`; empty = all models |
| `minHwRevision` | No | e.g. `"1.0"` — devices below this are skipped |
| `dependsOn` | No | `{"other-pkg": "2.0.0"}` |
| `incompatibleWith` | No | `{"legacy-pkg": "1.0.0"}` |

**Response `200`:**
```json
{
  "uploadUrl": "https://s3.amazonaws.com/...",
  "s3Key": "application/controller-app/4.0.0/controller-app-4.0.0.tar.gz",
  "expiresIn": 3600,
  "packageName": "controller-app",
  "version": "4.0.0",
  "packageType": "CONTROLLER_APP",
  "status": "PENDING",
  "instructions": "PUT your binary to uploadUrl with Content-Type: application/octet-stream. The package will be registered automatically within seconds of upload."
}
```

**Response `409`** — Version already exists and is ACTIVE:
```json
{ "error": "Package controller-app@4.0.0 already exists and is ACTIVE. Use a new version number." }
```

**After receiving this response**, the integration must:

1. `PUT <uploadUrl>` with the binary file and header `Content-Type: application/octet-stream`
2. Poll `GET /api/v1/ota/packages?packageName=controller-app` until `status` changes from `PENDING` → `ACTIVE` (typically within 3–5 seconds)

---

### 4.3 Check Available Updates for a Device

```
GET /api/v1/controllers/{deviceId}/updates/available
```

`deviceId` is the UUID from the device inventory (not the MAC-based thingName).

**Response `200`:**
```json
{
  "deviceId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "thingName": "digilux-94ba062a250c",
  "model": "DGX-1000",
  "hwRevision": "1.0",
  "pendingJobId": null,
  "installedVersions": {
    "controller-app": "3.0.0"
  },
  "availableUpdates": [
    {
      "packageName": "controller-app",
      "packageType": "CONTROLLER_APP",
      "currentVersion": "3.0.0",
      "availableVersion": "4.0.0",
      "artifactSize": 2097810,
      "releaseNotes": "Zigbee 3.0 support"
    }
  ],
  "updateCount": 1
}
```

**Response `404`** — Device not in inventory (OTA agent never ran on it):
```json
{ "error": "Device not found in OTA inventory. The OTA agent may not have started on this device yet." }
```

> **Notes:**
> - `availableUpdates` is empty if the device is already on the latest compatible version.
> - The `pendingJobId` field shows any in-progress deployment.
> - **Version comparison** uses semver-style integer comparison (e.g. `4.2.0 > 4.10.0` is evaluated as tuples `(4,2,0) < (4,10,0)` — i.e. `4.10.0` wins). Non-numeric version strings fall back to lexicographic comparison.
> - **`compatibleModels: []`** (empty list) means the package is compatible with all device models. Filtering only applies when the list is non-empty.

---

### 4.4 Create Deployment

```
POST /api/v1/ota/deployments
```

**Request body:**
```json
{
  "packageName": "controller-app",
  "version": "4.0.0",
  "targetType": "THING",
  "targetId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "rolloutStage": "CANARY"
}
```

| Field | Required | Description |
|---|---|---|
| `packageName` | Yes | Must match an ACTIVE package |
| `version` | Yes | Must match an ACTIVE version |
| `targetType` | Yes | `THING` (single device by UUID) or `THING_GROUP` (e.g. `DGX-Canary`) |
| `targetId` | Yes | Device UUID (for THING) or group name (for THING_GROUP) |
| `rolloutStage` | No | `CANARY` / `BETA` / `PRODUCTION`. Defaults to `PRODUCTION` |
| `rolloutConfig` | No | Override rollout rate settings |

**Response `201`:**
```json
{
  "jobId": "digilux-ota-controller-app-4-0-0-1785475706",
  "iotJobArn": "arn:aws:iot:ap-south-1:<YOUR_ACCOUNT_ID>:job/...",
  "packageName": "controller-app",
  "version": "4.0.0",
  "targetType": "THING",
  "targetId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "rolloutStage": "CANARY",
  "status": "QUEUED"
}
```

**Validation errors:**

| HTTP | Condition |
|---|---|
| `400` | Device already has this version installed |
| `400` | `targetType` not `THING` or `THING_GROUP` |
| `400` | Package is not in `ACTIVE` status |
| `404` | Package/version not found |
| `404` | Device not found in OTA inventory |

---

### 4.5 List Deployments

```
GET /api/v1/ota/deployments?limit=20
```

Returns jobs newest-first. `limit` max is 100.

**Response `200`:**
```json
{
  "jobs": [
    {
      "jobId": "digilux-ota-controller-app-4-0-0-1785475706",
      "packageName": "controller-app",
      "version": "4.0.0",
      "packageType": "CONTROLLER_APP",
      "targetType": "THING",
      "targetId": "edb39bba-baf1-4700-968c-a42228e53aa0",
      "rolloutStage": "CANARY",
      "status": "SUCCEEDED",
      "createdBy": "admin@example.com",
      "createdAt": 1785475706560,
      "completedAt": 1785475888991
    }
  ],
  "count": 1
}
```

---

### 4.6 Get Deployment Status

```
GET /api/v1/ota/deployments/{jobId}
```

**Response `200`:**
```json
{
  "jobId": "digilux-ota-controller-app-4-0-0-1785475706",
  "packageName": "controller-app",
  "version": "4.0.0",
  "targetType": "THING",
  "targetId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "rolloutStage": "CANARY",
  "status": "SUCCEEDED",
  "createdBy": "admin@example.com",
  "createdAt": 1785475706560,
  "completedAt": 1785475888991,
  "deviceStatuses": {
    "edb39bba-baf1-4700-968c-a42228e53aa0": {
      "status": "SUCCEEDED",
      "progress": 100,
      "updatedAt": 1785475888991
    }
  },
  "iotStatus": {
    "numberOfSucceededThings": 1,
    "numberOfFailedThings": 0,
    "numberOfInProgressThings": 0,
    "numberOfQueuedThings": 0
  },
  "iotJobStatus": "COMPLETED"
}
```

**Job `status` values:**

| Status | Meaning |
|---|---|
| `QUEUED` | Job created, device not yet picked it up |
| `IN_PROGRESS` | Device is downloading/installing |
| `SUCCEEDED` | Update installed and health check passed |
| `FAILED` | Update failed; see `deviceStatuses[id].statusDetail` |
| `CANCELLED` | Aborted by admin |
| `REJECTED` | Device rejected the job (e.g. precondition not met on device side) |

---

### 4.7 Abort Deployment

```
POST /api/v1/ota/deployments/{jobId}/abort
```

Cancels the IoT Job for any devices not yet in terminal state. Devices already installing are not interrupted.

**Response `200`:**
```json
{ "jobId": "digilux-ota-controller-app-4-0-0-...", "status": "CANCELLED" }
```

---

### 4.8 Check Available Updates (End User)

```
GET /api/v1/ota/my/updates
```

Returns all controller devices owned by the calling user and any available updates. Ownership is resolved from the JWT `sub` claim via `digilux_device_data`.

**Response `200`:**
```json
{
  "devices": [
    {
      "deviceId": "edb39bba-baf1-4700-968c-a42228e53aa0",
      "otaStatus": "REGISTERED",
      "model": "DGX-1000",
      "hwRevision": "1.0",
      "installedVersions": { "controller-app": "3.0.0" },
      "pendingJobId": null,
      "availableUpdates": [
        {
          "packageName": "controller-app",
          "packageType": "CONTROLLER_APP",
          "currentVersion": "3.0.0",
          "availableVersion": "4.0.0",
          "releaseNotes": "Zigbee 3.0 support",
          "artifactSize": 2097810
        }
      ],
      "updateCount": 1
    }
  ],
  "totalUpdates": 1
}
```

> `otaStatus: NOT_REGISTERED` — OTA agent has never started on this device. The device will not appear until it has connected at least once.

---

### 4.9 Give Update Consent (End User)

```
POST /api/v1/ota/my/updates/consent
```

Records user consent and triggers the OTA update. The device receives an IoT Job notification and downloads the binary over HTTPS from S3.

**Request body:**
```json
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0"
}
```

**Response `202`:**
```json
{
  "consentId":   "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "jobId":       "digilux-ota-controller-app-4-0-0-1785475706",
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0",
  "status":      "QUEUED",
  "message":     "Update accepted. Your device will download and install the update shortly."
}
```

**Validation and security checks (all server-side):**

| HTTP | Condition |
|---|---|
| `400` | Missing or malformed request fields |
| `404` | Device not found or not owned by this user |
| `404` | Package/version not found |
| `409` | This version is already installed |
| `409` | Requested version is not newer than installed |
| `409` | Another update is already in progress (`pendingJobId` non-null) |
| `409` | Device OTA agent has never started |
| `429` | Rate limit exceeded (1 consent per device per 5 min) |

> **Note on `404` vs `403`:** The ownership check returns `404` on failure, not `403`. This avoids revealing whether a given `deviceId` exists to an unauthorized caller.

---

### 4.10 Get Update Status (End User)

```
GET /api/v1/ota/my/updates/{jobId}/status
```

Returns the status of a user-initiated update. The `jobId` must belong to a consent created by the calling user — returns `404` if not found or not owned.

**Response `200`:**
```json
{
  "jobId":         "digilux-ota-controller-app-4-0-0-1785475706",
  "consentId":     "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "packageName":   "controller-app",
  "version":       "4.0.0",
  "status":        "SUCCEEDED",
  "statusMessage": "The update was installed successfully.",
  "consentedAt":   1785475706560,
  "createdAt":     1785475706560,
  "completedAt":   1785475888991,
  "progress":      100,
  "statusDetail":  null
}
```

**Status values:**

| Status | `statusMessage` |
|---|---|
| `QUEUED` | Your device is queued for the update. |
| `IN_PROGRESS` | Your device is downloading and installing the update. |
| `SUCCEEDED` | The update was installed successfully. |
| `FAILED` | The update failed. Your device may have rolled back to the previous version. |
| `CANCELLED` | The update was cancelled. |
| `REJECTED` | Your device rejected the update. |

---

## 5. Complete Integration Flow

### 5.1 Admin-initiated deployment

```
1. POST /api/v1/ota/packages/upload-url        (admin token)
   → receive { uploadUrl, s3Key, status: "PENDING" }

2. PUT <uploadUrl>  (binary, Content-Type: application/octet-stream)
   → HTTP 200 from S3

3. Poll GET /api/v1/ota/packages?packageName=<name>  (admin token)
   → wait for status: "ACTIVE"  (typically < 5 seconds)

4. GET /api/v1/controllers/{deviceId}/updates/available  (admin token)
   → confirm availableUpdates contains the new version

5. POST /api/v1/ota/deployments  (admin token)
   → receive { jobId, status: "QUEUED" }

6. Poll GET /api/v1/ota/deployments/{jobId}  (admin token)
   → wait for status: "SUCCEEDED" or "FAILED"
```

### 5.2 User-initiated update (Flutter app)

> **Prerequisite:** Admin must have completed steps 1–3 above so the package is `ACTIVE`.

```
1. GET /api/v1/ota/my/updates  (user token)
   → receive list of devices with availableUpdates
   → Flutter shows "Update available: controller-app 4.0.0"

2. User taps "Update Now" in Flutter app

3. POST /api/v1/ota/my/updates/consent  (user token)
   → body: { deviceId, packageName, version }
   → receive { consentId, jobId, status: "QUEUED" }
   → device receives IoT notification over MQTT
   → device downloads binary via HTTPS from S3
   → device verifies SHA256 + ECDSA signature
   → device installs and reports status

4. Poll GET /api/v1/ota/my/updates/{jobId}/status  (user token)
   → wait for status: "SUCCEEDED" or "FAILED"
   → Flutter shows result to user
```

---

## 6. Edge Cases & Validations

### 6.1 Partial Download (Internet Lost Mid-Download)

The device retries the download up to **3 times** before declaring failure:

```
Attempt 1: 40% downloaded → connection drops → partial file deleted → wait 30 s
Attempt 2: 80% downloaded → connection drops → partial file deleted → wait 60 s
Attempt 3: connection drops → partial file deleted → FAILED reported to cloud
```

**Key points:**
- Only transient network errors are retried (`ConnectionError`, `ChunkedEncodingError`, `ReadTimeout`)
- A partial `.tmp` file is always deleted before each retry — a truncated file is never installed
- Non-retryable errors (HTTP 403, expired pre-signed URL, 404) fail immediately without retry
- After 3 failures: job marked `FAILED`, admin must create a new deployment (which generates a fresh pre-signed URL)

**Pre-signed URL expiry = 1 hour**, which matches the **IoT Jobs in-progress timeout = 60 min** — if the URL expires, the job also times out, and a new deployment is required.

---

### 6.2 Installation Failure (Corruption / Disk Full / Health Check Failure)

The device always takes a backup **before** stopping the service. If anything fails during install:

**Scenario A — Install fails, rollback succeeds (normal failure path):**
```
Backup taken → Service stopped → Install fails (corrupted tar, disk full, etc.)
→ Backup restored → Service restarted with previous version
→ Job marked FAILED
→ detail: "Install failed — previous version restored"
→ Device is healthy on old version
```

**Scenario B — Install fails AND rollback also fails (critical):**
```
Backup taken → Service stopped → Install fails
→ Restore backup fails (e.g. disk corruption)
→ Job marked FAILED
→ detail: "NEEDS_RECOVERY: install failed and rollback failed — manual intervention required"
→ Device may be in broken state — requires physical/SSH access
```

**Integration team action on `NEEDS_RECOVERY`:**
- `NEEDS_RECOVERY` is handled gracefully by the cloud — it does **not** trigger a Lambda error alarm
- Detect via CloudWatch Logs Insights query on `/aws/lambda/digilux_ota_status_handler`:
  ```
  filter needsRecovery = 1
  | fields @timestamp, resource.deviceId, resource.jobId
  | sort @timestamp desc
  ```
- Or check `deviceStatuses[deviceId].statusDetail` in `GET /deployments/{jobId}` for the string `"NEEDS_RECOVERY"`
- Escalate to field team for manual recovery (physical/SSH access required)

---

### 6.3 Re-deploying an Already-Installed Version

The API rejects this at the deployment creation step:

```
POST /api/v1/ota/deployments
→ 400: "Device already has controller-app@4.0.0 installed. No deployment needed."
```

> This check applies to `targetType: THING` only. Group deployments (`THING_GROUP`) are not checked at this level since individual devices in the group may be on different versions.

---

### 6.4 Version Skipping (e.g. 1.0 → 1.2, skipping 1.1)

**Fully supported by design.** The system always targets the latest compatible version directly. The full artifact for 1.2 is delivered — no intermediate installation of 1.1 is required. This matches standard OS/app update behaviour (Android, macOS, etc.).

---

### 6.5 Device Offline When Deployment Is Created

The IoT Job is stored in AWS IoT Jobs. When the device comes back online:
- The OTA agent subscribes to the jobs notification topic on startup
- IoT Jobs re-delivers the pending job automatically
- Device picks up the job and proceeds normally

---

### 6.6 Device Not in OTA Inventory

If the OTA agent has never started on a device, it does not appear in inventory. Creating a deployment targeting it returns `404`. The device must run the OTA agent at least once to self-register.

---

### 6.7 Auto-Abort on Fleet-Wide Failure

For group deployments, IoT Jobs automatically cancels the rollout if:
- More than **10% of devices FAIL** (minimum 3 devices must have executed)
- More than **20% of devices TIME OUT** (minimum 3 devices must have executed)

This prevents a bad update from propagating to the full fleet.

---

### 6.8 Internet Lost After Successful Install (Before Status Report)

If the device installed successfully but lost connection before reporting back:

1. **Within 60 min:** paho-mqtt auto-reconnects → SUCCEEDED is sent belatedly → job status updated → device inventory updated
2. **After 60 min:** IoT Jobs marks execution `TIMED_OUT` → on next OTA agent startup, `register_device` fires with the new installed version from disk → inventory is corrected automatically

---

## 7. Device Inventory & Status

The `digilux_device_inventory` DynamoDB table is the source of truth for what is installed on each device.

| Field | Description |
|---|---|
| `deviceId` | UUID — primary key used in all API calls |
| `thingName` | IoT Thing name (MAC-based, e.g. `digilux-94ba062a250c`) |
| `model` | Hardware model (e.g. `DGX-1000`) |
| `hwRevision` | Hardware revision (for compatibility filtering) |
| `installedVersions` | `{ "controller-app": "4.0.0", ... }` |
| `pendingJobId` | Active job ID, `null` if idle |
| `lastSeen` | Timestamp of last agent registration |

The inventory is updated by:
- **Device startup:** OTA agent publishes installed versions → `digilux_ota_device_register` Lambda
- **Job completion:** Status SUCCEEDED → `digilux_ota_status_handler` Lambda

---

## 8. Package Lifecycle

```
POST /upload-url     S3 Upload         S3 Event → Lambda           Deploy
   PENDING    ──────────────>   PENDING   ──────────────>   ACTIVE   ──────>  In IoT Job
                                           (SHA256 + ECDSA
                                            computed & stored)
```

A package in `PENDING` state **cannot be deployed** — the API returns `400`. Always wait for `ACTIVE` before creating a deployment.

---

## 9. S3 Key Structure

Artifacts are stored at:

```
s3://digilux-ota-artifacts/{prefix}/{packageName}/{version}/{fileName}
```

| Package Type | Prefix |
|---|---|
| `CONTROLLER_FIRMWARE` | `firmware/` |
| `CONTROLLER_APP` | `application/` |
| `DRIVER` | `drivers/` |
| `ZIGBEE_DEVICE` | `zigbee-devices/` |
| `CONFIG` | `config/` |
| `RULES` | `rules/` |

See **Section 12** for S3 bucket security settings and artifact lifecycle policy.

---

## 10. Security

| Control | Detail |
|---|---|
| **Auth** | All API endpoints require Cognito admin group membership |
| **Artifact integrity** | SHA256 hash verified on device before install |
| **Artifact authenticity** | ECDSA P-256 signature verified on device using public key stored at `/etc/digilux/ota-agent.pub` |
| **Transport** | Pre-signed S3 URLs (HTTPS only), expire in 1 hour |
| **Mandatory updates** | Device cannot decline — `"mandatory": true` in job document |
| **Signing key** | Private key stored in AWS Secrets Manager (`digilux-ota-signing-key`), never on device |

---

## 11. Observability & Alerting

### 11.1 SNS Alert Topic

All CloudWatch alarms are wired to the `digilux-ota-alerts` SNS topic:

```
arn:aws:sns:ap-south-1:<YOUR_ACCOUNT_ID>:digilux-ota-alerts
```

Subscribed email: `<YOUR_ALERT_EMAIL>`

---

### 11.2 CloudWatch Alarms

| Alarm | Threshold | Triggers On |
|---|---|---|
| `digilux-ota-errors-digilux_ota_upload_url` | ≥3 errors / 5 min | Upload URL Lambda failing |
| `digilux-ota-errors-digilux_ota_artifact_processor` | ≥3 errors / 5 min | Artifact processing failures |
| `digilux-ota-errors-digilux_ota_job_create` | ≥3 errors / 5 min | Deployment creation failures |
| `digilux-ota-errors-digilux_ota_status_handler` | ≥3 errors / 5 min | Device status update failures |
| `digilux-ota-errors-digilux_ota_compatibility_check` | ≥3 errors / 5 min | Compatibility check failures |
| `digilux-ota-errors-digilux_ota_device_register` | ≥3 errors / 5 min | Device registration failures |
| `digilux-ota-dlq-digilux_ota_artifact_processor` | ≥1 message | Async invocation failed after 2 retries |
| `digilux-ota-dlq-digilux_ota_status_handler` | ≥1 message | Async invocation failed after 2 retries |
| `digilux-ota-rule-errors` | ≥1 error | IoT Rule failed to deliver to Lambda |

All alarms notify `digilux-ota-alerts` SNS on both ALARM and OK state transitions.

---

### 11.3 Dead Letter Queues

`artifact_processor` and `status_handler` are async-invoked (S3 events and IoT rules respectively). Both have SQS DLQs configured with **2 retries** before a failed invocation is captured:

| Lambda | DLQ |
|---|---|
| `digilux_ota_artifact_processor` | `digilux_ota_artifact_processor-dlq` |
| `digilux_ota_status_handler` | `digilux_ota_status_handler-dlq` |

If a DLQ alarm fires, retrieve the failed message from SQS to inspect the event payload that caused the failure.

---

### 11.4 Log Groups

All 6 OTA Lambda log groups have **30-day retention**:

```
/aws/lambda/digilux_ota_upload_url
/aws/lambda/digilux_ota_artifact_processor
/aws/lambda/digilux_ota_job_create
/aws/lambda/digilux_ota_status_handler
/aws/lambda/digilux_ota_compatibility_check
/aws/lambda/digilux_ota_device_register
/digilux/ota/rule-errors   ← IoT Rule delivery failures
```

---

### 11.5 CloudWatch Dashboard

Dashboard: **`digilux-ota-fleet`** (region: `ap-south-1`)

Widgets:
- Lambda errors across all 6 functions (5-min intervals)
- Lambda invocation counts
- DLQ depth for artifact_processor and status_handler
- Recent OTA status events (live log query)
- Recent errors and failures across all Lambda log groups

---

## 12. S3 Artifact Storage

Artifacts are stored in `digilux-ota-artifacts` with the following protections and lifecycle:

| Setting | Value |
|---|---|
| Versioning | Enabled |
| Public access | Fully blocked |
| Encryption | AES-256 (SSE-S3) |
| Incomplete multipart uploads | Aborted after 7 days |
| Non-current versions | Moved to STANDARD_IA after 30 days |
| Non-current versions | Deleted after 90 days |

The 90-day retention on non-current versions allows rollback reference while controlling storage costs. Active (current) artifact versions are never expired.

---

## 13. Known Constraints

| Constraint | Detail |
|---|---|
| Single-partition device | No A/B partition — rollback uses file-system backup (3 generations kept) |
| Version skipping | Fully supported; no sequential upgrade enforcement |
| Group deployments | No per-device version check — target all devices in group regardless of current version |
| Pre-signed URL TTL | 1 hour — download must complete within 1 hour of deployment creation |
| Agent prerequisite | Device must have run OTA agent at least once to appear in inventory |
| Zigbee OTA | Routed via zigbee2mqtt bridge API — 10-minute per-device timeout |

---

## 14. Version History

| Version | Date | Author | Changes |
|---|---|---|---|
| `1.0` | 2026-07-31 | Digilux Engineering | Initial release — full OTA system: package upload, artifact processing, deployment lifecycle, device status handling, abort flow, audit logging, compatibility check, IoT integration |
| `1.1` | 2026-07-31 | Digilux Engineering | Production hardening: SNS alert topic (`digilux-ota-alerts`), SQS DLQs for async Lambdas, 30-day log retention on all Lambda log groups, S3 lifecycle policy (non-current versions → STANDARD_IA @30d, deleted @90d), individual CloudWatch alarms for all 6 Lambdas + DLQ depth alarms, updated dashboard with DLQ widget; fixed e2e test T08 duplicate deployment API call bug |
| `1.2` | 2026-07-31 | Digilux Engineering | Accuracy fixes: added `REJECTED` job status, corrected `NEEDS_RECOVERY` alert path (CloudWatch Logs Insights, not Lambda error alarm), added `instructions` field to upload-url response, documented semver version comparison logic and empty `compatibleModels` behaviour |
| `1.3` | 2026-08-04 | Digilux Engineering | User-initiated OTA flow: Sections 4.8–4.10 (check updates, consent, status); updated auth section to cover user vs admin tokens; added Section 5.2 (user flow sequence); new DynamoDB table `digilux_ota_user_consents`; device ownership verification via `digilux_device_data`; rate limiting; consent audit trail |
