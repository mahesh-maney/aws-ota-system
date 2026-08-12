# Digilux OTA (Over-The-Air) Update System — Integration Guide

**Version:** 1.5
**Date:** 2026-08-12
**Audience:** Integration / QA Team
**Base URL:** `https://iot.digilux.co.in/smarthome` (custom domain)
**Alternate URL:** `https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome`
**Region:** `ap-south-1` | **Cognito Pool:** `ap-south-1_h1o8s7257` | **Client ID:** `q7189jitfkk4ttesepkgls491`

---

## 1. Overview

The Digilux OTA system supports two update modes:

**Admin-initiated:** Ops team pushes mandatory updates to any device or device group. The device cannot decline once an admin deployment is created.

**User-initiated:** The homeowner opens the Flutter app, sees an available update, gives explicit consent, and the device downloads and installs the update over HTTPS. The admin must have published the package first — users only control *when* to apply it, not *what* is available.

### Device Types

Each upload is tied to a `deviceType` which determines the package name and file extension automatically:

| `deviceType` | Derived `packageName` | File extension |
|---|---|---|
| `Network_controller_firmware` | `HomeAssistantUtility` | `.jar` |
| `Network_controller_zigbee_firmware` | `ZigbeeFirmware` | `.tar` |
| `Network_controller_Z2M_Firmware` | `Z2MFirmware` | `.bin` |
| `Network_controller_Miscellaneous` | `NetControllerMisc` | `.py` |

### Release Types

| `releaseType` | Visibility |
|---|---|
| `PROD` | All registered devices |
| `BETA` | Devices in the `DGX-Canary` IoT Thing Group only |

### Rollout Stages

| Stage | Rate | Use Case |
|---|---|---|
| `CANARY` | Max 2 devices/min | First 5 devices — smoke test |
| `BETA` | Exponential from 2/min | ~10% of fleet |
| `PRODUCTION` | Exponential from 5/min | Full fleet |

---

## Quick Reference — All Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| **Admin** | | `admin` group token | |
| GET | `/api/v1/ota/packages` | Admin | List packages (filter: `?deviceType=`, `?status=`) |
| POST | `/api/v1/ota/packages/upload-url` | Admin | Get pre-signed S3 upload URL |
| PATCH | `/api/v1/ota/packages/{packageName}/{version}/activate` | Admin | Publish or withdraw a package |
| GET | `/api/v1/controllers/{deviceId}/updates/available` | Admin | Compatibility check for a device |
| POST | `/api/v1/ota/deployments` | Admin | Create deployment (push update) |
| GET | `/api/v1/ota/deployments` | Admin | List all deployments |
| GET | `/api/v1/ota/deployments/{jobId}` | Admin | Get deployment status |
| POST | `/api/v1/ota/deployments/{jobId}/abort` | Admin | Abort a deployment |
| **End User** | | Any valid token | |
| GET | `/api/v1/ota/device/available-updates` | User | Check available updates for user's devices |
| POST | `/api/v1/ota/my/updates/consent` | User | Consent + trigger update (IoT Job flow) |
| POST | `/api/v1/ota/my/updates/download-link` | User | Get download URL + MQTT push (app-mediated flow) |
| GET | `/api/v1/ota/my/updates/{jobId}/status` | User | Track consent-flow update status |

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
    │  PATCH /packages/.../activate │                                 │
    │──────────────────────────>│  (publish package to users)        │
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

### Admin endpoints (Sections 4.1–4.8)

Require a **Cognito ID Token** from the `admin` group.

```http
Authorization: <Cognito ID Token>
```

Non-admin tokens receive `HTTP 403 — Admin access required`.

### End-user endpoints (Sections 4.9–4.11)

Require any valid **Cognito ID Token** — no admin group needed. The `userId` is extracted from the JWT `sub` claim to determine device ownership.

```http
Authorization: <Cognito ID Token>
```

Users can only see and update devices registered under their own `userId`.

### How to obtain a token

```bash
# Admin token (for sections 4.1–4.8)
aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id q7189jitfkk4ttesepkgls491 \
  --auth-parameters USERNAME=<admin-email>,PASSWORD=<password> \
  --region ap-south-1 \
  --query 'AuthenticationResult.IdToken' \
  --output text

# End-user token (for sections 4.9–4.12)
aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id q7189jitfkk4ttesepkgls491 \
  --auth-parameters USERNAME=<user-email>,PASSWORD=<password> \
  --region ap-south-1 \
  --query 'AuthenticationResult.IdToken' \
  --output text
```

Tokens are valid for **24 hours**. Pass the token as the `Authorization` header value (no `Bearer` prefix).

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
| `deviceType` | — | Filter by device type (e.g. `Network_controller_firmware`) |

**Response `200`:**
```json
{
  "packages": [
    {
      "packageName": "HomeAssistantUtility",
      "version": "1.2.3",
      "deviceType": "Network_controller_firmware",
      "releaseType": "PROD",
      "status": "ACTIVE",
      "activated": true,
      "artifactSize": 2097810,
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
  "deviceType":  "Network_controller_firmware",
  "version":     "1.2.3",
  "releaseType": "PROD"
}
```

| Field | Required | Description |
|---|---|---|
| `deviceType` | Yes | One of the 4 supported device types (see table above) |
| `version` | Yes | Semantic version string (e.g. `1.2.3`) |
| `releaseType` | Yes | `PROD` (all devices) or `BETA` (canary group only) |

`packageName` and `fileName` are derived automatically from `deviceType` — you do not supply them.

**Response `200`:**
```json
{
  "uploadUrl":   "https://digilux-ota-artifacts.s3.ap-south-1.amazonaws.com/...",
  "s3Key":       "Network_controller_firmware/HomeAssistantUtility/1.2.3/HomeAssistantUtility-1.2.3.jar",
  "expiresIn":   3600,
  "packageName": "HomeAssistantUtility",
  "version":     "1.2.3",
  "deviceType":  "Network_controller_firmware",
  "releaseType": "PROD",
  "activated":   false,
  "status":      "PENDING"
}
```

**Response `409`** — Version already exists and is ACTIVE:
```json
{ "error": "Package HomeAssistantUtility@1.2.3 already exists and is ACTIVE. Use a new version number." }
```

**After receiving this response**, the integration must:

1. `PUT <uploadUrl>` with the binary file and header `Content-Type: application/octet-stream`
2. Poll `GET /api/v1/ota/packages?packageName=HomeAssistantUtility` until `status` changes from `PENDING` → `ACTIVE` (typically within 2–5 seconds)
3. Call `PATCH /api/v1/ota/packages/HomeAssistantUtility/1.2.3/activate` with `{"activated": true}` to publish the package to users

---

### 4.3 Activate / Withdraw Package

```
PATCH /api/v1/ota/packages/{packageName}/{version}/activate
```

After a package reaches `status=ACTIVE` it is still hidden from end users until an admin explicitly publishes it. This two-step design lets you upload and validate a firmware binary before it becomes visible.

**Path parameters:**

| Parameter | Description |
|---|---|
| `packageName` | e.g. `HomeAssistantUtility` (returned by Get Upload URL) |
| `version` | e.g. `1.2.3` |

**Request body:**
```json
{ "activated": true }
```

Set `"activated": false` to withdraw a previously published package (removes it from user update checks without deleting the binary).

**Prerequisite:** Package must have `status=ACTIVE` (set automatically by the artifact processor after upload).

**Response `200`:**
```json
{
  "packageName": "HomeAssistantUtility",
  "version":     "1.2.3",
  "activated":   true,
  "activatedBy": "admin@example.com",
  "activatedAt": 1785475706560
}
```

**Validation errors:**

| HTTP | Condition |
|---|---|
| `400` | Package is not yet `ACTIVE` (still `PENDING`) |
| `403` | Non-admin token |
| `404` | Package/version not found |

---

### 4.5 Check Available Updates for a Device

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
      "deviceType": "Network_controller_firmware",
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

### 4.5 Create Deployment

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

### 4.6 List Deployments

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
      "deviceType": "Network_controller_firmware",
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

### 4.7 Get Deployment Status

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

### 4.8 Abort Deployment

```
POST /api/v1/ota/deployments/{jobId}/abort
```

Cancels the IoT Job for any devices not yet in terminal state. Devices already installing are not interrupted.

**Response `200`:**
```json
{ "jobId": "digilux-ota-controller-app-4-0-0-...", "status": "CANCELLED" }
```

---

### 4.9 Check Available Updates (End User)

```
GET /api/v1/ota/device/available-updates
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
          "deviceType": "Network_controller_firmware",
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

> `otaStatus: NOT_REGISTERED` — OTA agent has never started on this device.

> Only packages with `status=ACTIVE` **and** `activated=true` appear in user update checks. BETA packages only appear for devices in the `DGX-Canary` IoT Thing Group. The device will not appear until it has connected at least once.

---

### 4.10 Give Update Consent (End User)

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

### 4.11 Get Update Status (End User)

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

### 4.12 Get Download Link — App-Mediated Update (End User)

```
POST /api/v1/ota/my/updates/download-link
```

An alternative to the consent flow. The Lambda returns a **CloudFront signed download URL** directly to the Flutter app and simultaneously publishes the same payload to the device's OTA MQTT topic (`iot/device/{deviceId}/ota`). The device receives the URL over MQTT and downloads the firmware over HTTPS.

**When to use this instead of `/consent`:**
- You want the Flutter app to hold the URL (e.g., to display progress, pass to a `NetworkController`, or handle offline-device scenarios)
- You want lower overhead — no IoT Job is created, just an MQTT publish

**No IoT Job is created** — the device must report completion via its standard status topic.

**Request body:**
```json
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0"
}
```

**Response `200`:**
```json
{
  "downloadUrl":   "https://d2lr14tk4wqz8z.cloudfront.net/application/controller-app/4.0.0/controller-app-4.0.0.tar.gz?Policy=...&Signature=...&Key-Pair-Id=K3CL07APICBEMS",
  "sha256":        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signature":     "MEUCIQDx...",
  "packageName":   "controller-app",
  "version":       "4.0.0",
  "size":          12456789,
  "deviceType": "Network_controller_firmware",
  "expiresAt":     "2026-08-04T15:00:00Z",
  "mqttDelivered": true,
  "message":       "Download URL sent to device via MQTT. Device will begin download shortly."
}
```

**`mqttDelivered`** — `true` if the device was notified via MQTT; `false` if the MQTT publish failed (e.g., device offline). In the `false` case the URL is still returned to the app — Flutter can retry or display a "device offline" message.

**MQTT payload published to `iot/device/{deviceId}/ota`:**
```json
{
  "operation":   "DOWNLOAD_AND_INSTALL",
  "packageName": "controller-app",
  "version":     "4.0.0",
  "packageType": "deb",
  "downloadUrl": "<presigned-url>",
  "sha256":      "<hex>",
  "signature":   "<base64>",
  "size":        12456789,
  "expiresAt":   "2026-08-04T15:00:00Z",
  "initiatedBy": "USER_APP",
  "userId":      "<sub>",
  "mandatory":   false,
  "rollback":    true
}
```

**Security checks** (identical to `/consent` except no rate-limit check and no IoT Job created):
1. Valid Cognito JWT with `sub` claim
2. `deviceId` UUID format
3. Device ownership (userId-index GSI on `digilux_device_data`)
4. Package `ACTIVE`
5. Device registered in OTA inventory
6. Version strictly newer than installed
7. No pending job already in-progress

**Error responses:** same codes as `/consent` — `400`, `401`, `404`, `409`, `500`.

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
1. GET /api/v1/ota/device/available-updates  (user token)
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

### 5.3 App-mediated download-link flow (Flutter app / curl)

> **Prerequisite:** Admin must have completed steps 1–3 of section 5.1 so the package is `ACTIVE`.

```
1. GET /api/v1/ota/device/available-updates  (user token)
   → receive list of devices with availableUpdates
   → Flutter shows "Update available: controller-app 4.0.0"

2. User taps "Update Now" in Flutter app

3. POST /api/v1/ota/my/updates/download-link  (user token)
   → body: { deviceId, packageName, version }
   → Lambda generates pre-signed S3 URL
   → Lambda publishes { downloadUrl, sha256, signature, ... } to iot/device/{deviceId}/ota
   → Response: { downloadUrl, sha256, signature, size, expiresAt, mqttDelivered }

4a. If mqttDelivered = true:
    → Device already received download command over MQTT
    → Device downloads binary from S3 over HTTPS
    → Device verifies SHA256 + ECDSA signature
    → Device installs and reports status via MQTT status topic
    → Flutter can display "Update in progress" using downloadUrl as a reference

4b. If mqttDelivered = false (device offline):
    → Flutter can show "Device offline — update will apply when device reconnects"
    → Flutter may pass downloadUrl to a NetworkController or local BT channel if available
    → URL is valid for 1 hour; call again if URL expires before device comes online
```

**curl example — download-link:**

```bash
BASE_URL="https://iot.digilux.co.in/smarthome"
USER_TOKEN="<user-cognito-id-token>"

# Step 1 — check available updates for user's devices
curl -s -X GET "$BASE_URL/api/v1/ota/device/available-updates" \
  -H "Authorization: $USER_TOKEN" | jq '.devices[].availableUpdates'

# Step 2 — get download link (Lambda also pushes URL to device via MQTT)
RESP=$(curl -s -X POST "$BASE_URL/api/v1/ota/my/updates/download-link" \
  -H "Authorization: $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"deviceId":"<device-uuid>","packageName":"controller-app","version":"4.0.0"}')

echo $RESP | jq '{downloadUrl: .downloadUrl, mqttDelivered: .mqttDelivered, expiresAt: .expiresAt}'
```

**Key differences from consent flow:**
| | Consent (`/consent`) | Download-link (`/download-link`) |
|---|---|---|
| IoT Job created | Yes (full audit, retry logic) | No |
| App receives URL | No | Yes |
| Device notified | Via IoT Job | Via direct MQTT publish |
| Status tracking | Via `/updates/{jobId}/status` | Via device MQTT status topic |
| Rate limiting | Yes (1 per device per 5 min) | No |
| Best for | Automated installs, audit trail | App-controlled installs |

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

**Artifact URL expiry** is tiered by file size: ≤50MB → 1hr, ≤200MB → 6hr, ≤500MB → 24hr, >500MB → 48hr (configurable via `ota.config`). URLs are served via **CloudFront signed URLs** (not S3 presigned) for CDN-accelerated delivery. The **IoT Job in-progress timeout is 24 hours** (`IOT_JOB_TIMEOUT_MINUTES=1440`) — if a device is offline for more than 24hr, the job times out and a new deployment is required.

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

1. **Within 24hr:** paho-mqtt auto-reconnects → SUCCEEDED is sent belatedly → job status updated → device data updated
2. **After 24hr:** IoT Jobs marks execution `TIMED_OUT` → on next OTA agent startup, `device_register` fires with the new installed version from disk → `digilux_device_data` is corrected automatically

---

## 7. Device Inventory & Status

OTA fields (`installedVersions`, `pendingJobId`, `thingName`) are stored as attributes on the `digilux_device_data` table — the same table used for device ownership. This eliminates a separate inventory table and reduces DynamoDB reads.

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
| Artifact URL TTL | Tiered by file size: ≤50MB→1hr / ≤200MB→6hr / ≤500MB→24hr / >500MB→48hr (CloudFront signed URL) |
| Agent prerequisite | Device must have run OTA agent at least once to appear in inventory |
| Zigbee OTA | Routed via zigbee2mqtt bridge API — 10-minute per-device timeout |

---

## 14. QA Test Guide

### 14.1 Test Environment

| Item | Value |
|---|---|
| Base URL | `https://iot.digilux.co.in/smarthome` |
| Region | `ap-south-1` |
| Cognito Pool | `ap-south-1_h1o8s7257` |
| Cognito Client ID | `q7189jitfkk4ttesepkgls491` |
| Test device UUID | `edb39bba-baf1-4700-968c-a42228e53aa0` |
| Test device thing name | `digilux-94ba062a250c` |
| Test device model | `DGX-1000` |
| Postman collection | `postman/Digilux_OTA.postman_collection.json` |
| Postman environment | `postman/Digilux_OTA.postman_environment.json` |

### 14.2 Test Accounts

| Account | Role | Use for |
|---|---|---|
| `mahesh.maney@gmail.com` | Admin (admin group) | All admin-only endpoints (4.1–4.7) |
| `demotesthw5@yopmail.com` | Regular user | End-user endpoints (4.8–4.11); owns test device |
| `stores@digilux.co.in` | Regular user | Alternative non-admin token; owns a different device |

> Passwords are held by the Digilux engineering team. Contact the team to obtain test credentials. Tokens expire after 24 hours.

### 14.3 Pre-Test Reset

Before each test run, reset the test device to a clean state so version checks pass:

```bash
DEVICE_ID="edb39bba-baf1-4700-968c-a42228e53aa0"
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"irjof5RLuQcVv2tvEVdSilZbm1Wj7J4AGWw69ZJ1e0r1AN7fM4W1NQ==\"}}" \
  --update-expression "SET pendingJobId = :null, installedVersions.#pkg = :v, lastUpdatedAt = :ts" \
  --expression-attribute-names '{"#pkg":"controller-app"}' \
  --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"2.0.0\"},\":ts\":{\"N\":\"$(date +%s)000\"}}" \
  --region ap-south-1
```

This resets the device to `controller-app@2.0.0` with no pending job — allowing all version-check and consent tests to pass.

### 14.4 Test Cases — Admin Flow

#### TC-A01 Authentication
| # | Action | Expected |
|---|---|---|
| 1 | Call `GET /api/v1/ota/packages` with **no token** | `401 Unauthorized` |
| 2 | Call `GET /api/v1/ota/packages` with a **non-admin token** | `403 Forbidden — Admin access required` |
| 3 | Call `GET /api/v1/ota/packages` with a valid **admin token** | `200 OK` |

#### TC-A02 Package Upload Flow
| # | Action | Expected |
|---|---|---|
| 1 | `POST /packages/upload-url` with valid body | `200` with `uploadUrl`, `s3Key`, `status: PENDING` |
| 2 | `PUT <uploadUrl>` with binary (no auth header) | `200` from S3 |
| 3 | Poll `GET /packages?packageName=<name>` for up to 15 s | `status` changes from `PENDING` → `ACTIVE` |
| 4 | Re-upload same `packageName@version` | `409 — already ACTIVE` |
| 5 | `POST /packages/upload-url` missing `packageName` | `400` |
| 6 | `POST /packages/upload-url` with invalid `packageType` | `400` |

#### TC-A03 Compatibility Check
| # | Action | Expected |
|---|---|---|
| 1 | `GET /controllers/<test-device-id>/updates/available` | `200` with `installedVersions`, `availableUpdates` |
| 2 | `GET /controllers/<unknown-uuid>/updates/available` | `404 — Device not found` |

#### TC-A04 Deployment Lifecycle
| # | Action | Expected |
|---|---|---|
| 1 | `POST /deployments` with ACTIVE package + test device | `201` with `jobId`, `status: QUEUED` |
| 2 | `GET /deployments/{jobId}` | `200` with `iotStatus`, `deviceStatuses` |
| 3 | `GET /deployments` | `200` with array of jobs |
| 4 | `POST /deployments` targeting same device + same version already installed | `400` |
| 5 | `POST /deployments` with PENDING package | `400` |
| 6 | `POST /deployments` with non-existent package | `404` |

#### TC-A05 Abort
| # | Action | Expected |
|---|---|---|
| 1 | `POST /deployments/{jobId}/abort` on an active job | `200` with `status: CANCELLED` |
| 2 | `POST /deployments/{jobId}/abort` on already-cancelled job | `400` |

### 14.5 Test Cases — End User Flow

> **Pre-condition for all user tests:** run the device reset from Section 14.3 first.

#### TC-U01 Check Available Updates
| # | Action | Expected |
|---|---|---|
| 1 | `GET /ota/device/available-updates` with a valid non-admin token | `200` with `devices` array |
| 2 | Device at `2.0.0`; ACTIVE package at `4.0.0` | `availableUpdates` contains `controller-app 4.0.0` |
| 3 | No packages published yet | `availableUpdates: []` for that device |

#### TC-U02 Give Update Consent
| # | Action | Expected |
|---|---|---|
| 1 | `POST /my/updates/consent` with valid body (user token, owned device, ACTIVE version, newer) | `202` with `consentId`, `jobId`, `status: QUEUED` |
| 2 | Same request again within 5 minutes | `429 — rate limit` |
| 3 | Request for device not owned by this user | `404 — Device not found` |
| 4 | Request for version already installed | `409 — already installed` |
| 5 | Request for version older than installed | `409 — not newer` |
| 6 | Request while `pendingJobId` is set | `409 — update already in progress` |
| 7 | Admin token used instead of user token | `202` (admin tokens are valid Cognito tokens and pass auth) |
| 8 | Missing `deviceId` field | `400` |
| 9 | Malformed UUID for `deviceId` | `400` |

#### TC-U03 Get Update Status
| # | Action | Expected |
|---|---|---|
| 1 | `GET /my/updates/{jobId}/status` with a `jobId` from TC-U02 step 1 | `200` with `status`, `statusMessage`, `consentId` |
| 2 | `GET /my/updates/{jobId}/status` using another user's `jobId` | `404` |
| 3 | `GET /my/updates/nonexistent-job-id/status` | `404` |

#### TC-U04 Get Download Link
| # | Action | Expected |
|---|---|---|
| 1 | `POST /my/updates/download-link` with valid body (owned device, ACTIVE version, newer) | `200` with `downloadUrl`, `sha256`, `signature`, `mqttDelivered`, `expiresAt` |
| 2 | Verify `downloadUrl` is accessible via HTTP GET (no auth) | `200` from S3 (file download starts) |
| 3 | Verify `mqttDelivered: true` when device is online | `mqttDelivered: true` |
| 4 | Request for device not owned by this user | `404 — Device not found` |
| 5 | Request for version already installed (reset device first) | `409 — already installed` |
| 6 | Request while `pendingJobId` is set | `409 — update already in progress` |
| 7 | Repeated calls within short interval (no rate limit) | Each call succeeds with `200` |

### 14.6 Postman Quick Start

1. Open Postman
2. **Import** → `postman/Digilux_OTA.postman_collection.json`
3. **Import** → `postman/Digilux_OTA.postman_environment.json`
4. Select environment **"Digilux OTA — AWS (ap-south-1)"**
5. Set `auth_token` — paste admin Cognito ID token
6. Set `user_auth_token` — paste non-admin Cognito ID token
7. `device_id`, `package_name`, `package_version` are pre-filled in the environment
8. Use **Full Deployment Flow** folder for admin E2E, **Full User Update Flow** for consent flow, **Full App-Mediated Update Flow** for download-link flow

### 14.7 Automated E2E Test Suite

The repository includes a bash-based E2E test script covering 69 assertions across 16 test groups.

```bash
# Prerequisites: AWS CLI configured, tokens in /tmp
echo "<admin-id-token>" > /tmp/ota_admin_token.txt
echo "<nonadmin-id-token>" > /tmp/ota_nonadmin_token.txt
echo "https://iot.digilux.co.in/smarthome" > /tmp/ota_base_url.txt

# Reset test device (see Section 14.3)

# Run
bash infrastructure/e2e_test.sh
```

Results are printed to stdout and saved to `infrastructure/e2e_test_results.txt`. Last verified: **69/69 PASS** (2026-08-04).

> The E2E suite covers the admin flow (T01–T16). User-flow endpoint tests (consent, download-link) are covered by the Postman collection.

---

## 15. Version History

| Version | Date | Author | Changes |
|---|---|---|---|
| `1.0` | 2026-07-31 | Digilux Engineering | Initial release — full OTA system: package upload, artifact processing, deployment lifecycle, device status handling, abort flow, audit logging, compatibility check, IoT integration |
| `1.1` | 2026-07-31 | Digilux Engineering | Production hardening: SNS alert topic (`digilux-ota-alerts`), SQS DLQs for async Lambdas, 30-day log retention on all Lambda log groups, S3 lifecycle policy (non-current versions → STANDARD_IA @30d, deleted @90d), individual CloudWatch alarms for all 6 Lambdas + DLQ depth alarms, updated dashboard with DLQ widget; fixed e2e test T08 duplicate deployment API call bug |
| `1.2` | 2026-07-31 | Digilux Engineering | Accuracy fixes: added `REJECTED` job status, corrected `NEEDS_RECOVERY` alert path (CloudWatch Logs Insights, not Lambda error alarm), added `instructions` field to upload-url response, documented semver version comparison logic and empty `compatibleModels` behaviour |
| `1.3` | 2026-08-04 | Digilux Engineering | User-initiated OTA flow: Sections 4.9–4.10 (check updates, consent, status); updated auth section to cover user vs admin tokens; added Section 5.2 (user flow sequence); new DynamoDB table `digilux_ota_user_consents`; device ownership verification via `digilux_device_data`; rate limiting; consent audit trail |
| `1.5` | 2026-08-12 | Digilux Engineering | Consolidated `digilux_device_inventory` into `digilux_device_data`; CloudFront signed URLs for artifact delivery; tiered presign expiry (1hr–48hr by file size, `ota.config`); IoT Job timeout 24hr; `dynamodb:UpdateItem` added to user Lambda IAM role; user e2e test suite expanded to 53 tests (TU01–TU11) |
| `1.4` | 2026-08-04 | Digilux Engineering | App-mediated download-link flow: Section 4.11 (`POST /api/v1/ota/my/updates/download-link`); Lambda returns pre-signed URL to Flutter app and simultaneously publishes download payload to device MQTT OTA topic; Section 5.3 (flow comparison table: consent vs download-link); new Lambda `digilux_ota_user_get_download_link`; IAM `iot:Publish` permission on `iot/device/*/ota` |
