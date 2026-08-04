# Digilux OTA Update System

Production-grade Over-The-Air (OTA) update system for Digilux controller devices. Supports two update modes:

- **Admin-initiated** — ops team pushes mandatory updates to any device or group via the admin API
- **User-initiated** — homeowners check for available updates, give consent via the Flutter app, and the device downloads and installs over HTTPS

**Region:** `ap-south-1` | **Runtime:** Python 3.11 | **Transport:** MQTT (AWS IoT) + HTTPS (S3 + API Gateway)

---

## Table of Contents

1. [Architecture](#architecture)
2. [Quick Start](#quick-start)
3. [API Reference — Admin](#api-reference--admin)
4. [API Reference — End User](#api-reference--end-user)
5. [Update Types & Rollout Stages](#update-types--rollout-stages)
6. [Deployment Lifecycle](#deployment-lifecycle)
7. [Failure Handling](#failure-handling)
8. [Device Inventory](#device-inventory)
9. [Security](#security)
10. [Observability](#observability)
11. [Infrastructure](#infrastructure)
12. [Testing](#testing)

---

## Architecture

### Admin-Initiated Flow

```
Admin / Integration App
        │
        │  REST (HTTPS)
        ▼
  API Gateway
        │
        ├── POST /packages/upload-url ──► digilux_ota_upload_url
        │                                        │ Pre-signed S3 URL
        │   PUT <uploadUrl> (binary) ────────────► S3: digilux-ota-artifacts
        │                                        │
        │                                        │ S3 Event
        │                                        ▼
        │                              digilux_ota_artifact_processor
        │                              (SHA256 + ECDSA sign → ACTIVE)
        │
        ├── POST /deployments ──────────► digilux_ota_job_create
        │                                        │ Creates IoT Job
        │                                        ▼
        │                                  AWS IoT Core
        │                                        │ MQTT notify (port 8883, mTLS)
        │                                        ▼
        │                               Controller Device
        │                               └─ HTTPS download from S3
        │                               └─ verify SHA256 + ECDSA
        │                               └─ install + health check
        │                                        │ MQTT status report
        │                                        ▼
        │                     IoT Rule: digilux_ota_status_ingest
        │                                        ▼
        │                              digilux_ota_status_handler
        │                              (DynamoDB: jobs + inventory)
        │
        ├── GET /deployments/{jobId} ──► digilux_ota_job_create
        ├── GET /packages ─────────────► digilux_ota_upload_url
        └── GET /controllers/{id}/updates/available ──► digilux_ota_compatibility_check
```

### User-Initiated Flow

```
Flutter App (homeowner)
        │
        │  REST (HTTPS)   Cognito ID token (regular user)
        ▼
  API Gateway
        │
        ├── GET  /ota/my/updates ────────► digilux_ota_user_check_updates
        │        ↑ checks device ownership via digilux_device_data
        │        ↑ compares installedVersions vs latest ACTIVE package
        │
        ├── POST /ota/my/updates/consent ► digilux_ota_user_consent
        │        ↑ verifies ownership, package status, version, rate limit
        │        │ Creates IoT Job + records consent in digilux_ota_user_consents
        │        ▼
        │   AWS IoT Core
        │        │ MQTT notify → Controller Device
        │        └─ HTTPS download from S3 (pre-signed URL in job document)
        │        └─ verify → install → report status via MQTT
        │
        └── GET  /ota/my/updates/{jobId}/status ► digilux_ota_user_update_status
                 ↑ ownership check via digilux_ota_user_consents before returning data
```

---

## Quick Start

### 1. Get an admin token

```bash
TOKEN=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id q7189jitfkk4ttesepkgls491 \
  --auth-parameters USERNAME=<admin-email>,PASSWORD=<password> \
  --region ap-south-1 \
  --query 'AuthenticationResult.IdToken' \
  --output text)
```

### 2. Upload a package

```bash
# Step 1 — get pre-signed URL
RESP=$(curl -s -X POST \
  "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/packages/upload-url" \
  -H "Authorization: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "packageName": "controller-app",
    "version": "4.0.0",
    "packageType": "CONTROLLER_APP",
    "fileName": "controller-app-4.0.0.tar.gz",
    "releaseNotes": "Zigbee 3.0 support"
  }')

UPLOAD_URL=$(echo $RESP | jq -r '.uploadUrl')

# Step 2 — upload the binary
curl -X PUT "$UPLOAD_URL" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @controller-app-4.0.0.tar.gz

# Step 3 — wait for ACTIVE (typically < 5 seconds)
curl -s "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/packages?packageName=controller-app" \
  -H "Authorization: $TOKEN" | jq '.packages[].status'
```

### 3. Deploy to a device

```bash
# Create deployment
JOB=$(curl -s -X POST \
  "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/deployments" \
  -H "Authorization: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "packageName": "controller-app",
    "version": "4.0.0",
    "targetType": "THING",
    "targetId": "<device-uuid>",
    "rolloutStage": "CANARY"
  }')

JOB_ID=$(echo $JOB | jq -r '.jobId')

# Poll status
curl -s "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/deployments/$JOB_ID" \
  -H "Authorization: $TOKEN" | jq '{status, deviceStatuses}'
```

### 4. User-initiated update (Flutter app / end user)

```bash
USER_TOKEN=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id q7189jitfkk4ttesepkgls491 \
  --auth-parameters USERNAME=<user-email>,PASSWORD=<password> \
  --region ap-south-1 \
  --query 'AuthenticationResult.IdToken' \
  --output text)

# Check available updates for the user's devices
curl -s "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/my/updates" \
  -H "Authorization: $USER_TOKEN" | jq '.devices[].availableUpdates'

# Give consent (triggers download + install on the device)
JOB=$(curl -s -X POST \
  "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/my/updates/consent" \
  -H "Authorization: $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"deviceId":"<device-uuid>","packageName":"controller-app","version":"4.0.0"}')

JOB_ID=$(echo $JOB | jq -r '.jobId')

# Poll status
curl -s "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/my/updates/$JOB_ID/status" \
  -H "Authorization: $USER_TOKEN" | jq '{status, statusMessage, progress}'
```

### Postman

Import `postman/Digilux_OTA.postman_collection.json` and `postman/Digilux_OTA.postman_environment.json`.
- Set `auth_token` for admin endpoints, `user_auth_token` for user endpoints
- **Full Deployment Flow** folder — admin end-to-end sequence
- **End User Updates > Full User Update Flow** — user-initiated sequence

---

## API Reference — Admin

**Base URL:** `https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome`

**Auth:** All admin endpoints require `Authorization: <Cognito ID Token>` from the `admin` Cognito group. Non-admin tokens return `403`.

---

### GET /api/v1/ota/packages

List packages filtered by status or name.

| Query Param | Default | Description |
|---|---|---|
| `status` | `ACTIVE` | `ACTIVE` or `PENDING` |
| `packageName` | — | List all versions of a specific package |

**Response 200:**
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

### POST /api/v1/ota/packages/upload-url

Request a pre-signed S3 URL to upload a new package artifact.

**Request body:**
```json
{
  "packageName": "controller-app",
  "version": "4.0.0",
  "packageType": "CONTROLLER_APP",
  "fileName": "controller-app-4.0.0.tar.gz",
  "releaseNotes": "Zigbee 3.0 support",
  "compatibleModels": ["DGX-1000"],
  "minHwRevision": "1.0",
  "dependsOn": {},
  "incompatibleWith": {}
}
```

| Field | Required | Description |
|---|---|---|
| `packageName` | Yes | Software component identifier |
| `version` | Yes | Semantic version (e.g. `4.0.0`) |
| `packageType` | Yes | See [Update Types](#update-types--rollout-stages) |
| `fileName` | No | Defaults to `artifact.bin` |
| `releaseNotes` | No | Human-readable changelog |
| `compatibleModels` | No | `["DGX-1000"]`; empty `[]` = all models |
| `minHwRevision` | No | Devices below this revision are skipped |
| `dependsOn` | No | `{"other-pkg": "2.0.0"}` |
| `incompatibleWith` | No | `{"legacy-pkg": "1.0.0"}` |

**Response 200:**
```json
{
  "uploadUrl": "https://s3.amazonaws.com/...",
  "s3Key": "application/controller-app/4.0.0/controller-app-4.0.0.tar.gz",
  "expiresIn": 3600,
  "packageName": "controller-app",
  "version": "4.0.0",
  "status": "PENDING",
  "instructions": "PUT your binary to uploadUrl with Content-Type: application/octet-stream..."
}
```

**Response 409:** Version already ACTIVE.

After receiving `uploadUrl`, PUT the binary to it (no auth header, `Content-Type: application/octet-stream`). Then poll `GET /packages?packageName=<name>` until `status` = `ACTIVE` (typically < 5 seconds).

---

### GET /api/v1/controllers/{deviceId}/updates/available

Returns installed versions and available updates for a device.

`deviceId` is the UUID from the device inventory, not the MAC-based IoT thing name.

**Response 200:**
```json
{
  "deviceId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "thingName": "digilux-94ba062a250c",
  "model": "DGX-1000",
  "hwRevision": "1.0",
  "pendingJobId": null,
  "installedVersions": { "controller-app": "3.0.0" },
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

**Response 404:** Device has never run the OTA agent.

---

### POST /api/v1/ota/deployments

Create a deployment (push a package to a device or group).

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
| `targetType` | Yes | `THING` (single device UUID) or `THING_GROUP` (group name) |
| `targetId` | Yes | Device UUID or group name (e.g. `DGX-Canary`) |
| `rolloutStage` | No | `CANARY` / `BETA` / `PRODUCTION` (default: `PRODUCTION`) |

**Response 201:**
```json
{
  "jobId": "digilux-ota-controller-app-4-0-0-1785475706",
  "iotJobArn": "arn:aws:iot:ap-south-1:...",
  "packageName": "controller-app",
  "version": "4.0.0",
  "targetType": "THING",
  "targetId": "edb39bba-baf1-4700-968c-a42228e53aa0",
  "rolloutStage": "CANARY",
  "status": "QUEUED"
}
```

| HTTP | Condition |
|---|---|
| `400` | Device already has this version installed |
| `400` | Package not in ACTIVE status |
| `400` | Invalid `targetType` |
| `404` | Package/version not found |
| `404` | Device not in OTA inventory |

---

### GET /api/v1/ota/deployments

List all deployments, newest-first.

| Query Param | Default | Max |
|---|---|---|
| `limit` | `20` | `100` |

---

### GET /api/v1/ota/deployments/{jobId}

Get full deployment status including per-device progress.

**Response 200:**
```json
{
  "jobId": "digilux-ota-controller-app-4-0-0-1785475706",
  "packageName": "controller-app",
  "version": "4.0.0",
  "status": "SUCCEEDED",
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

**Job status values:**

| Status | Meaning |
|---|---|
| `QUEUED` | Created, device not yet acknowledged |
| `IN_PROGRESS` | Device is downloading or installing |
| `SUCCEEDED` | Installed and health check passed |
| `FAILED` | Failed; check `deviceStatuses[id].statusDetail` |
| `CANCELLED` | Aborted by admin |
| `REJECTED` | Device rejected (precondition not met) |

---

### POST /api/v1/ota/deployments/{jobId}/abort

Cancels the deployment for devices not yet in a terminal state. Devices already installing are not interrupted.

**Response 200:**
```json
{ "jobId": "digilux-ota-controller-app-4-0-0-...", "status": "CANCELLED" }
```

---

## API Reference — End User

**Auth:** Any valid Cognito ID token — no admin group required. Users can only access their own devices.

> **Division of responsibility:** Admins upload and publish packages. End users decide *when* to apply them to their device.

---

### GET /api/v1/ota/my/updates

Returns all controller devices owned by the calling user and any available updates.

- Ownership is resolved via `digilux_device_data` (Cognito `sub` → `userId`)
- Version comparison uses semver integer tuples — `4.10.0 > 4.2.0`
- `otaStatus: NOT_REGISTERED` means the OTA agent has never started on that device

**Response 200:**
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

---

### POST /api/v1/ota/my/updates/consent

Records the user's consent and triggers the OTA update on their device. The device downloads the binary over HTTPS from S3 and installs it.

**Request body:**
```json
{
  "deviceId":    "edb39bba-baf1-4700-968c-a42228e53aa0",
  "packageName": "controller-app",
  "version":     "4.0.0"
}
```

**Security checks (all server-side, in order):**
1. Valid JWT with `sub` claim present
2. `deviceId` UUID format validation
3. **Device ownership** — `deviceId` must be registered under this `userId` in `digilux_device_data` (returns `404` on failure, not `403` — avoids leaking device existence)
4. Package `packageName@version` must exist and be `ACTIVE`
5. Requested version must be strictly newer than installed version
6. No update already in progress (`pendingJobId` must be null)
7. Rate limit — max 1 consent per device per 5 minutes

**Response 202:**
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

| HTTP | Condition |
|---|---|
| `404` | Device not found or not owned by this user |
| `404` | Package/version not found |
| `409` | This version is already installed |
| `409` | Requested version is not newer than installed |
| `409` | Another update is already in progress |
| `409` | Device OTA agent has never started |
| `429` | Rate limit exceeded (1 consent / device / 5 min) |

---

### GET /api/v1/ota/my/updates/{jobId}/status

Returns the current status of a user-initiated update.

**Security:** `jobId` must exist in `digilux_ota_user_consents` and belong to the calling user. Returns `404` if not found or not owned — users cannot enumerate other users' jobs.

**Response 200:**
```json
{
  "jobId":         "digilux-ota-controller-app-4-0-0-1785475706",
  "consentId":     "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "packageName":   "controller-app",
  "version":       "4.0.0",
  "status":        "IN_PROGRESS",
  "statusMessage": "Your device is downloading and installing the update.",
  "consentedAt":   1785475706560,
  "createdAt":     1785475706560,
  "completedAt":   null,
  "progress":      62,
  "statusDetail":  null
}
```

**`statusMessage` values by status:**

| Status | Message shown to user |
|---|---|
| `QUEUED` | Your device is queued for the update. |
| `IN_PROGRESS` | Your device is downloading and installing the update. |
| `SUCCEEDED` | The update was installed successfully. |
| `FAILED` | The update failed. Your device may have rolled back to the previous version. |
| `CANCELLED` | The update was cancelled. |
| `REJECTED` | Your device rejected the update. |

---

## Update Types & Rollout Stages

### Package Types

| Type | Description |
|---|---|
| `CONTROLLER_FIRMWARE` | Core OS-level firmware |
| `CONTROLLER_APP` | Main Python application |
| `DRIVER` | Hardware/peripheral drivers |
| `ZIGBEE_DEVICE` | Firmware for Zigbee end devices (via zigbee2mqtt) |
| `CONFIG` | Configuration file updates |
| `RULES` | Automation rules updates |

### Rollout Stages

| Stage | Rate | Use Case |
|---|---|---|
| `CANARY` | Max 2 devices/min | First 5 devices — smoke test |
| `BETA` | Exponential from 2/min | ~10% of fleet |
| `PRODUCTION` | Exponential from 5/min | Full fleet |

**Auto-abort thresholds (group deployments):**
- > 10% of devices FAIL (min 3 must have executed)
- > 20% of devices TIME OUT (min 3 must have executed)

---

## Deployment Lifecycle

```
POST /deployments
      │
      ▼
   QUEUED  ──── Device offline ────► (IoT holds; delivered on reconnect)
      │
      ▼
 IN_PROGRESS  ──── download ──── verify SHA256 + ECDSA ──── backup ──── install ──── health check
      │
      ├──── health check pass ───► SUCCEEDED  (inventory updated)
      │
      ├──── install/health fail ──► rollback from backup
      │           │
      │           ├── rollback OK ──► FAILED  (device on previous version)
      │           └── rollback fail ► FAILED  (statusDetail: NEEDS_RECOVERY)
      │
      └──── 3 download retries fail ► FAILED
```

**Download retry behaviour:**
- Attempt 1 → wait 30 s → Attempt 2 → wait 60 s → Attempt 3 → FAILED
- Only transient errors are retried (connection drops, read timeouts)
- HTTP 403, 404 or expired pre-signed URL → fail immediately, no retry
- Pre-signed URL TTL = 1 hour (matches IoT Jobs in-progress timeout)

---

## Failure Handling

### Installation Failure with Rollback

The device always takes a backup before stopping the service:

- **Normal failure:** install fails → backup restored → device on previous version → job `FAILED`
- **Critical failure:** install fails AND restore fails → job `FAILED` with `statusDetail: NEEDS_RECOVERY`

**Detecting NEEDS_RECOVERY:**

Option A — via API:
```bash
curl -s "{{base_url}}/api/v1/ota/deployments/$JOB_ID" -H "Authorization: $TOKEN" \
  | jq '.deviceStatuses | to_entries[] | select(.value.statusDetail | test("NEEDS_RECOVERY"))'
```

Option B — CloudWatch Logs Insights on `/aws/lambda/digilux_ota_status_handler`:
```
filter needsRecovery = 1
| fields @timestamp, resource.deviceId, resource.jobId
| sort @timestamp desc
```

NEEDS_RECOVERY requires manual/SSH access. Escalate to the field team.

### Device Offline at Deployment Time

The IoT Job is stored in AWS IoT. When the device comes back online:
- OTA agent subscribes to the jobs topic on startup
- IoT delivers the pending job automatically
- Device proceeds normally

### Internet Lost After Successful Install

- **Within 60 min:** paho-mqtt auto-reconnects → SUCCEEDED sent → inventory updated
- **After 60 min:** IoT marks `TIMED_OUT` → on next agent startup, re-registration corrects inventory automatically

---

## Device Inventory

Source of truth: `digilux_device_inventory` DynamoDB table.

| Field | Description |
|---|---|
| `deviceId` | UUID — primary key for all API calls |
| `thingName` | IoT Thing name (MAC-based, e.g. `digilux-94ba062a250c`) |
| `model` | Hardware model (e.g. `DGX-1000`) |
| `hwRevision` | Hardware revision |
| `installedVersions` | `{ "controller-app": "4.0.0" }` |
| `pendingJobId` | Active job ID; `null` if idle |
| `lastSeen` | Last agent registration timestamp |

**Inventory is updated by:**
- Device startup: OTA agent publishes installed versions → `digilux_ota_device_register` Lambda
- Job completion: SUCCEEDED → `digilux_ota_status_handler` Lambda

**A device appears in inventory only after the OTA agent has run at least once.**

---

## Security

| Control | Detail |
|---|---|
| **Admin auth** | Admin endpoints require Cognito `admin` group membership |
| **User auth** | User endpoints accept any valid Cognito token; no admin group needed |
| **Device ownership** | `digilux_device_data` is the ownership oracle — `userId` from JWT must match device record on every user request |
| **Artifact integrity** | SHA256 hash verified on device before install |
| **Artifact authenticity** | ECDSA P-256 signature verified using public key at `/etc/digilux/ota-agent.pub` |
| **Transport** | Binary download via pre-signed S3 HTTPS URL (1-hour expiry) |
| **Signing key** | Private key in Secrets Manager (`digilux-ota-signing-key`) — never on device |
| **Mandatory updates** | Device cannot decline — `"mandatory": true` in IoT job document |
| **S3** | Versioned, AES-256 SSE, public access fully blocked |
| **Rate limiting** | Max 1 user consent per device per 5 minutes |
| **Consent audit** | All user consents recorded in `digilux_ota_user_consents` (24h TTL) |
| **Cross-user isolation** | Status endpoint returns `404` (not `403`) for jobs not owned by the caller — avoids leaking job existence |
| **Least privilege** | User Lambda role (`digilux-ota-user-lambda-role`) has no admin permissions; cannot list all devices or all jobs |

---

## Observability

### CloudWatch Alarms (9 total)

| Alarm | Threshold |
|---|---|
| Per-Lambda error alarms (×6) | ≥ 3 errors / 5 min |
| DLQ depth — `artifact_processor-dlq` | ≥ 1 message |
| DLQ depth — `status_handler-dlq` | ≥ 1 message |
| IoT rule errors | ≥ 1 error |

All alarms notify the `digilux-ota-alerts` SNS topic.

### Dead Letter Queues

`artifact_processor` and `status_handler` are async-invoked. Both have SQS DLQs with 2 retries before a failed event is captured.

### Log Groups (30-day retention)

```
/aws/lambda/digilux_ota_upload_url
/aws/lambda/digilux_ota_artifact_processor
/aws/lambda/digilux_ota_job_create
/aws/lambda/digilux_ota_status_handler
/aws/lambda/digilux_ota_compatibility_check
/aws/lambda/digilux_ota_device_register
/aws/lambda/digilux_ota_user_check_updates
/aws/lambda/digilux_ota_user_consent
/aws/lambda/digilux_ota_user_update_status
/digilux/ota/rule-errors
```

### Dashboard

`digilux-ota-fleet` in CloudWatch (region: `ap-south-1`) — Lambda errors, invocation counts, DLQ depth, recent status events.

---

## Infrastructure

### AWS Resources

| Resource | Name/ARN |
|---|---|
| S3 bucket | `digilux-ota-artifacts` |
| DynamoDB tables | `digilux_ota_packages`, `digilux_ota_jobs`, `digilux_ota_compatibility`, `digilux_device_inventory`, `digilux_ota_user_consents` |
| IoT Thing Groups | `DGX-Canary`, `DGX-Beta`, `DGX-Production`, `DGX-Controllers` |
| IoT Rules | `digilux_ota_status_ingest`, `digilux_ota_device_register` |
| Signing key | `digilux-ota-signing-key` (Secrets Manager, ECDSA P-256) |
| Admin Lambda IAM role | `digilux-ota-lambda-role` |
| User Lambda IAM role | `digilux-ota-user-lambda-role` |

### Infrastructure Scripts (deploy order)

```
01_s3 → 02_secrets → 03_iot_setup → 04_dynamodb → 05_iam_roles
→ 06_lambdas/ → 07_deploy_lambdas → 08_iot_rules → 09_api_gateway
→ 10_cloudwatch → 11_s3_events → 12_production_hardening
→ 13_user_ota_setup   ← user-initiated OTA (run after 09)
```

All scripts are in `infrastructure/`.

### S3 Lifecycle

| Setting | Value |
|---|---|
| Versioning | Enabled |
| Encryption | AES-256 (SSE-S3) |
| Non-current versions → STANDARD_IA | After 30 days |
| Non-current versions → deleted | After 90 days |
| Incomplete multipart uploads | Aborted after 7 days |

### S3 Key Structure

```
s3://digilux-ota-artifacts/<prefix>/<packageName>/<version>/<fileName>
```

| Package Type | Prefix |
|---|---|
| `CONTROLLER_FIRMWARE` | `firmware/` |
| `CONTROLLER_APP` | `application/` |
| `DRIVER` | `drivers/` |
| `ZIGBEE_DEVICE` | `zigbee-devices/` |
| `CONFIG` | `config/` |
| `RULES` | `rules/` |

---

## Testing

### E2E Test Suite

```bash
# Reset test device before each run
DEVICE_ID="edb39bba-baf1-4700-968c-a42228e53aa0"
aws dynamodb update-item --table-name digilux_device_inventory \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"}}" \
  --update-expression "SET pendingJobId = :null, installedVersions.#pkg = :v, lastUpdatedAt = :ts" \
  --expression-attribute-names '{"#pkg":"controller-app"}' \
  --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"2.0.0\"},\":ts\":{\"N\":\"$(date +%s)000\"}}" \
  --region ap-south-1

# Run tests
bash infrastructure/e2e_test.sh
```

69 tests across 16 groups (T01–T16): authentication, input validation, package upload, deployment lifecycle, device status, abort, audit logs, DynamoDB consistency, IoT infrastructure, Lambda health.

Results are written to `infrastructure/e2e_test_results.txt`.

---

## Known Constraints

| Constraint | Detail |
|---|---|
| No A/B partition | Rollback uses filesystem backup (3 generations kept) |
| Version skipping | Fully supported — no sequential upgrade required |
| Group deployments | No per-device version check at creation time |
| Pre-signed URL TTL | 1 hour — download must complete within this window |
| Agent prerequisite | Device must run OTA agent at least once to appear in inventory |
| Zigbee OTA | Routed via zigbee2mqtt bridge — 10-minute per-device timeout |

---

## Version History

| Version | Date | Changes |
|---|---|---|
| `1.0` | 2026-07-31 | Initial release |
| `1.1` | 2026-07-31 | Production hardening: SNS alerts, DLQs, log retention, S3 lifecycle, CloudWatch alarms |
| `1.2` | 2026-07-31 | Added `REJECTED` status, NEEDS_RECOVERY alert path, semver comparison docs, `compatibleModels` docs |
| `1.3` | 2026-08-04 | User-initiated OTA flow: `digilux_ota_user_check_updates`, `digilux_ota_user_consent`, `digilux_ota_user_update_status`; new `digilux_ota_user_consents` DynamoDB table; `digilux-ota-user-lambda-role`; device ownership verification; rate limiting; consent audit trail |

---

## Feature Guide — Plain English

### What problem does it solve?

Imagine you have 500 Digilux smart home controller boxes installed across 500 different homes. One day you find a bug, or you want to add a new feature. Without OTA, someone has to physically visit each home with a USB drive and update the box manually. That's expensive, slow, and impractical.

OTA lets you update every box remotely, over the internet, while the homeowner doesn't even notice anything happening.

---

### The Two Actors

**The Admin** — Digilux's ops/engineering team. They prepare and publish new software versions. Think of them as the publisher.

**The End User (Homeowner)** — The person who bought the Digilux system. They get notified when an update is ready and decide when to apply it — like how your phone asks "Update available. Install now?"

---

### Feature 1: Packaging a Software Update

**Who does it:** Admin

The process of getting a new software version ready for deployment.

1. **Admin requests an upload slot** — calls the API and says "I want to upload `controller-app` version `4.0.0`". The system gives back a temporary, secure link to upload the file.

2. **Admin uploads the binary** — drops the actual `.tar.gz` file (the compiled software) directly to AWS S3 cloud storage using that link. No one else can access this link. It expires in 1 hour.

3. **System automatically processes it** — the moment the file lands in S3, a background process (Lambda) automatically wakes up and:
   - Calculates the **SHA256 fingerprint** of the file (like a unique stamp that proves the file wasn't corrupted or tampered with)
   - Digitally signs it using an **ECDSA private key** stored in AWS Secrets Manager (like a wax seal only Digilux can make)
   - Marks the package as `ACTIVE` — ready for deployment

4. **Admin can verify** — calls the list API to confirm the package shows `ACTIVE` status before proceeding.

**Why this matters:** No one can sneak a fake or corrupted file into the system. Even if someone intercepted the file mid-transfer, the device would reject it because the fingerprint and digital signature wouldn't match.

---

### Feature 2: Admin-Initiated Deployment (Mandatory Push)

**Who does it:** Admin

The ops team decides to push an update to a device or an entire group of devices — the device has no choice but to install it.

**How it works:**

1. Admin creates a "deployment" — specifies which package, which version, and which device(s) to target.

2. AWS IoT Core (Amazon's device messaging infrastructure) sends a tiny notification to the device via **MQTT** — a lightweight always-on messaging protocol, like a WhatsApp message but for machines.

3. The device wakes up, asks for the full job details, gets a **secure download link** (pre-signed HTTPS URL from S3, valid 1 hour).

4. The device downloads the binary file over HTTPS, verifies the SHA256 fingerprint and ECDSA signature, takes a **backup of the currently running software**, then installs the new version.

5. After installing, the device runs a health check. If everything is working, it reports `SUCCEEDED`. If not, it automatically rolls back to the backup and reports `FAILED`.

6. The admin can check the status at any time via the API.

**Rollout Stages:** You don't have to push to everyone at once. You can push to:
- **CANARY** — just 2 devices per minute, your 5 "test" devices first. If they're fine after 24 hours, you proceed.
- **BETA** — ~10% of the fleet, exponentially ramping up.
- **PRODUCTION** — the full fleet.

**Auto-abort:** If more than 10% of devices fail or 20% time out, the system automatically stops the rollout. You don't have to babysit it.

---

### Feature 3: User-Initiated Update (With Consent)

**Who does it:** End User (homeowner, via the Flutter app)

The homeowner gets to see available updates and choose when to apply them — like app updates on your phone.

**How it works:**

1. **User opens the app and checks for updates.** The app calls our API. The system:
   - Looks up which controller device(s) belong to this user (from the database)
   - Checks what software versions are installed on each device
   - Compares with the latest `ACTIVE` packages the admin has published
   - Returns: "You have an update available: controller-app 3.0.0 → 4.0.0, Zigbee 3.0 support"

2. **User taps "Update Now".** The app sends a consent request. Before doing anything, the system checks:
   - Is this device actually owned by this user? (Security gate — you can't trigger an update on someone else's device)
   - Is the package genuinely available and published by Digilux?
   - Is this version actually newer than what's installed?
   - Is there already an update running? (Can't start two at once)
   - Has this user already requested an update in the last 5 minutes? (Rate limit — prevents accidental double-taps or abuse)

3. **If all checks pass:** The system records the consent (for audit purposes), creates a job, and the device gets notified. The device downloads the binary over HTTPS from S3, verifies it, and installs it — exactly the same secure process as an admin push.

4. **User can track progress.** The app polls the status endpoint:
   - "Your device is queued for the update."
   - "Your device is downloading and installing the update."
   - "The update was installed successfully." ✅
   - Or "The update failed. Your device may have rolled back to the previous version." ❌

**Key distinction from admin push:** The admin publishes what's *available*. The user decides *when*. The admin cannot force an update through the user-initiated flow — and the user cannot install something the admin hasn't published.

---

### Feature 4: Device Registration (Self-Check-In)

**Who does it:** The device itself, automatically

Every time the controller device starts up, it sends a "hello, I'm online" message to the cloud over MQTT. This message includes:
- Its unique device ID
- What software versions are currently installed
- Its hardware model and revision

The cloud stores this in a database (`digilux_device_inventory`). This is how we always know the truth about what's installed — the device tells us on every boot, so even if the cloud record was stale, it gets corrected automatically.

---

### Feature 5: Compatibility Check

**Who does it:** Admin (or integration system)

Before pushing an update to a device, you can ask: "Is this device eligible for this update?"

The system checks:
- **Hardware model** — e.g. update only applies to `DGX-1000`, not `DGX-500`
- **Minimum hardware revision** — e.g. needs hardware revision `1.0` or higher
- **Already installed?** — no point pushing `4.0.0` if it's already on `4.0.0`
- **Version comparison** — done correctly: `4.10.0` is newer than `4.2.0` (not lexicographic string comparison, which would get that wrong)

---

### Feature 6: Failure Recovery

Three failure scenarios, all handled automatically:

**Scenario A — Download fails mid-way (e.g. internet drops)**
The device retries up to 3 times with increasing wait times (30s, then 60s). A partially downloaded file is always deleted before retrying — never a corrupt half-file. After 3 failures, the job is marked `FAILED`.

**Scenario B — Install fails or health check fails**
The device took a backup before stopping the service. It restores the backup, restarts the old version, and reports `FAILED` with detail "previous version restored". The device is back to normal automatically.

**Scenario C — Install fails AND the backup restore also fails**
This is a critical situation (e.g. disk corruption). The device reports `FAILED` with `statusDetail: NEEDS_RECOVERY`. The cloud logs this separately. Digilux field team is alerted to visit the device physically.

---

### Feature 7: Offline Device Handling

**Problem:** What if the device is offline when an update is triggered?

**Solution:** The IoT Job is held by AWS. The moment the device comes back online and reconnects, AWS automatically delivers the pending job. The device picks it up and proceeds normally. The pre-signed download URL is valid for 1 hour from when the deployment was created, so the download must complete within that window.

---

### Feature 8: Abort / Cancel

**Who does it:** Admin

If an update is going badly (many failures, wrong version pushed by mistake), the admin can abort it. Devices already downloading or installing are not interrupted mid-way — only devices that haven't started yet are cancelled. This avoids leaving a device in a broken half-installed state.

---

### Feature 9: Audit Trail & Observability

Everything is logged. Every action — package upload, deployment, consent, success, failure — produces a structured audit log entry. Specifically:

- **9 CloudWatch alarms** fire if Lambdas error or message queues back up, alerting the ops team by email
- **Dead Letter Queues (DLQs)** catch any messages that failed to process after 2 retries — nothing is silently dropped
- **Dashboard** in CloudWatch shows real-time fleet health
- **Consent records** are stored for 24 hours — you can always answer "did this user actually consent to this update?"
- **NEEDS_RECOVERY** events can be found via a CloudWatch Logs query on the status handler

---

### Feature 10: Security (Layer by Layer)

| Layer | What it does |
|---|---|
| **Cognito JWT** | Every API call is authenticated — no anonymous access |
| **Admin group check** | Admin endpoints reject any token not from the `admin` Cognito group |
| **Device ownership check** | User endpoints verify the device belongs to the calling user — checked against the database, not just the request body |
| **SHA256 + ECDSA** | The device independently verifies the downloaded file's integrity and authenticity before touching the filesystem |
| **Private signing key in Secrets Manager** | The key that signs packages never leaves AWS — it's not in any code or config file |
| **Pre-signed S3 URLs** | Download links expire in 1 hour and can only be used once for that specific file |
| **Rate limiting** | Max 1 consent per device per 5 minutes — prevents spam or accidental loops |
| **404 instead of 403** | When a user tries to act on a device they don't own, they get "not found" not "forbidden" — so they can't even confirm that device ID exists |
| **Separate IAM roles** | The user Lambda role has no admin permissions — even if it were compromised, it couldn't list all devices or push to arbitrary devices |

---

### Summary

| Feature | Triggered by | What happens |
|---|---|---|
| Upload package | Admin | Binary lands in S3, gets signed and marked ACTIVE |
| Admin deployment | Admin | IoT Job created, device downloads + installs over HTTPS |
| User check for updates | Homeowner | App shows available versions for their device |
| User consent & install | Homeowner | Consent recorded, IoT Job created, device downloads + installs |
| Status tracking | Admin or User | Real-time job status with per-device progress |
| Rollout stages | Admin | CANARY → BETA → PRODUCTION with auto-abort |
| Device self-registration | Device | Reports installed versions on every boot |
| Compatibility check | Admin | Filters by model, hardware revision, version |
| Download retry | Device | 3 attempts with backoff before failing |
| Rollback | Device | Automatic restore from backup on install failure |
| Offline handling | AWS IoT | Job held and delivered when device reconnects |
| Abort | Admin | Cancels pending devices without interrupting active installs |
| Alerting | AWS CloudWatch | Email alert on Lambda errors, DLQ depth, IoT rule failures |
