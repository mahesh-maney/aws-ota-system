# Digilux OTA Update System

Production-grade Over-The-Air (OTA) update system for Digilux controller devices. Enables admins to push software updates to field devices without physical access.

**Region:** `ap-south-1` | **Runtime:** Python 3.11 | **Transport:** MQTT (AWS IoT) + REST (API Gateway)

---

## Table of Contents

1. [Architecture](#architecture)
2. [Quick Start](#quick-start)
3. [API Reference](#api-reference)
4. [Update Types & Rollout Stages](#update-types--rollout-stages)
5. [Deployment Lifecycle](#deployment-lifecycle)
6. [Failure Handling](#failure-handling)
7. [Device Inventory](#device-inventory)
8. [Security](#security)
9. [Observability](#observability)
10. [Infrastructure](#infrastructure)
11. [Testing](#testing)

---

## Architecture

```
Admin / Integration App
        │
        │  REST (HTTPS)
        ▼
  API Gateway  ──────────────────────────────────────────────────────────┐
        │                                                                  │
        ├── POST /packages/upload-url ──► digilux_ota_upload_url          │
        │                                        │ Pre-signed S3 URL       │
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
        │                                        │ MQTT (port 8883, mTLS)
        │                                        ▼
        │                               Controller Device
        │                               (download → verify → install → report)
        │                                        │ MQTT status
        │                                        ▼
        │                     IoT Rule: digilux_ota_status_ingest
        │                                        │
        │                                        ▼
        │                              digilux_ota_status_handler
        │                              (DynamoDB update: jobs + inventory)
        │
        ├── GET /deployments/{jobId} ──► digilux_ota_job_create
        ├── GET /packages ─────────────► digilux_ota_upload_url
        └── GET /controllers/{id}/updates/available ──► digilux_ota_compatibility_check
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

### Postman

Import `postman/Digilux_OTA.postman_collection.json` and set `auth_token` in the collection variables. The **Full Deployment Flow** folder runs the complete sequence in order.

---

## API Reference

**Base URL:** `https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome`

**Auth:** All endpoints require `Authorization: <Cognito ID Token>` from the `admin` Cognito group. Non-admin tokens return `403`.

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
| **Auth** | All API endpoints require Cognito admin group membership |
| **Artifact integrity** | SHA256 hash verified on device before install |
| **Artifact authenticity** | ECDSA P-256 signature verified using public key at `/etc/digilux/ota-agent.pub` |
| **Transport** | Pre-signed S3 URLs (HTTPS only), 1-hour expiry |
| **Signing key** | Private key in Secrets Manager (`digilux-ota-signing-key`) — never on device |
| **Mandatory updates** | Device cannot decline — `"mandatory": true` in IoT job document |
| **S3** | Versioned, AES-256 SSE, public access fully blocked |

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
| DynamoDB tables | `digilux_ota_packages`, `digilux_ota_jobs`, `digilux_ota_compatibility`, `digilux_device_inventory` |
| IoT Thing Groups | `DGX-Canary`, `DGX-Beta`, `DGX-Production`, `DGX-Controllers` |
| IoT Rules | `digilux_ota_status_ingest`, `digilux_ota_device_register` |
| Signing key | `digilux-ota-signing-key` (Secrets Manager, ECDSA P-256) |
| Lambda IAM role | `digilux-ota-lambda-role` |

### Infrastructure Scripts (deploy order)

```
01_s3 → 02_secrets → 03_iot_setup → 04_dynamodb → 05_iam_roles
→ 06_lambdas/ → 07_deploy_lambdas → 08_iot_rules → 09_api_gateway
→ 10_cloudwatch → 11_s3_events → 12_production_hardening
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
