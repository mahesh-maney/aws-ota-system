# Digilux OTA System — Flow Document

**Version:** 1.2
**Date:** 2026-08-12
**Audience:** Engineering, Integration, QA Teams

This document describes all key flows in the Digilux OTA update system — from package upload through to device installation and failure recovery. Each flow shows the components involved, the sequence of operations, and the MQTT topics or API calls used.

---

## System Components

| Component | Type | Role |
|---|---|---|
| **Admin / Integration App** | Client | Uploads packages, triggers deployments, monitors status |
| **API Gateway** | AWS | Routes HTTP requests to Lambda functions |
| `digilux_ota_upload_url` | Lambda | Issues pre-signed S3 upload URLs, writes PENDING record. Derives `packageName` and `fileName` from `deviceType` automatically. |
| `digilux_ota_artifact_processor` | Lambda | Triggered by S3 event; verifies upload token + checksum; computes SHA256, signs with ECDSA, promotes package to ACTIVE (still hidden until activated). Quarantines invalid uploads (deletes S3 object, marks `CORRUPTED`). |
| `digilux_ota_job_create` | Lambda | Creates IoT Jobs, lists/gets/aborts deployments |
| `digilux_ota_compatibility_check` | Lambda | Returns available updates for a specific device |
| `digilux_ota_status_handler` | Lambda | Receives device status via IoT Rule; updates DynamoDB |
| `digilux_ota_device_register` | Lambda | Receives device registration on agent startup; upserts OTA fields (`thingName`, `installedVersions`, `pendingJobId`) on `digilux_device_data` |
| **S3** (`digilux-ota-artifacts`) | Storage | Stores Network_controller_firmware/app binaries |
| **DynamoDB** | Storage | `digilux_ota_packages`, `digilux_ota_jobs`, `digilux_ota_compatibility`, `digilux_device_data` (OTA fields: `installedVersions`, `pendingJobId`, `thingName`) |
| **AWS IoT Core** | Messaging | Delivers jobs to devices over MQTT (port 8883, mTLS) |
| **IoT Rule** `digilux_ota_status_ingest` | Rule | Topic `iot/device/+/ota/status` → `status_handler` Lambda |
| **IoT Rule** `digilux_ota_device_register` | Rule | Topic `iot/device/+/ota/register` → `device_register` Lambda |
| **Secrets Manager** | Security | Stores ECDSA P-256 private signing key (`digilux-ota-signing-key`) and CloudFront RSA private key (`digilux-ota-cloudfront-key`) |
| **OTA Agent** | Device | Python service on Debian Linux controller; connects via MQTT, handles jobs |

---

## Flow 1: Package Upload

An admin registers a new firmware or application package so it can be deployed to devices.

```
Admin                  API Gateway         upload_url Lambda    DynamoDB (packages)    S3
  │                        │                     │                     │                │
  │  POST /packages/       │                     │                     │                │
  │  upload-url            │                     │                     │                │
  │  {deviceType,          │                     │                     │                │
  │   version,             │                     │                     │                │
  │   releaseType,         │                     │                     │                │
  │   checksum?}           │                     │                     │                │
  │───────────────────────>│                     │                     │                │
  │                        │  Validate OTA admin │                     │                │
  │                        │  pool Cognito token │                     │                │
  │                        │  + ota-admin group  │                     │                │
  │                        │────────────────────>│                     │                │
  │                        │                     │  Check duplicate    │                │
  │                        │                     │  (409 if ACTIVE)    │                │
  │                        │                     │────────────────────>│                │
  │                        │                     │  Generate UUID      │                │
  │                        │                     │  uploadToken        │                │
  │                        │                     │                     │                │
  │                        │                     │  Generate pre-      │                │
  │                        │                     │  signed PUT URL     │                │
  │                        │                     │  (token baked in,   │                │
  │                        │                     │   expires 5 min)    │                │
  │                        │                     │─────────────────────────────────────>│
  │                        │                     │  ← signed URL                        │
  │                        │                     │                     │                │
  │                        │                     │  Write PENDING      │                │
  │                        │                     │  record (stores     │                │
  │                        │                     │  uploadToken +      │                │
  │                        │                     │  expectedChecksum)  │                │
  │                        │                     │────────────────────>│                │
  │  ← 200 { uploadUrl,    │                     │                     │                │
  │    uploadToken,        │                     │                     │                │
  │    s3Key, status:      │                     │                     │                │
  │    "PENDING",          │                     │                     │                │
  │    expiresIn: 300 }    │                     │                     │                │
  │<───────────────────────│                     │                     │                │
  │                        │                     │                     │                │
  │  PUT <uploadUrl>       │                     │                     │                │
  │  Content-Type:         │                     │                     │                │
  │   application/         │                     │                     │                │
  │   octet-stream         │                     │                     │                │
  │  x-amz-meta-           │                     │                     │                │
  │   upload-token: <tok>  │                     │                     │                │
  │  (S3 verifies token    │                     │                     │                │
  │   is in URL signature  │                     │                     │                │
  │   → 403 if wrong)      │                     │                     │                │
  │──────────────────────────────────────────────────────────────────>│                │
  │  ← HTTP 200            │                     │                     │                │
```

### Step-by-step

1. Admin calls `POST /api/v1/ota/packages/upload-url` with `deviceType`, `version`, `releaseType`, and optional `checksum`
2. API Gateway validates the token against the **OTA admin Cognito pool** (`ap-south-1_jUErEu7CL`) — rejects unknown pool tokens with `401`
3. `upload_url` Lambda checks `ota-admin` group membership — rejects with `403` if not an admin
4. Lambda checks DynamoDB — rejects with `409` if version already exists and is `ACTIVE`
5. Lambda generates a UUID `uploadToken` and bakes it into the presigned URL as a required signed header (`x-amz-meta-upload-token`)
6. Lambda generates a pre-signed S3 `PUT` URL (**5-minute expiry**)
7. Lambda writes a `PENDING` record to `digilux_ota_packages` storing `uploadToken` and `expectedChecksum` (if provided)
8. Admin receives `{ uploadUrl, uploadToken, s3Key, expiresIn: 300, status: "PENDING" }`
9. Admin `PUT`s the binary to S3 with `Content-Type: application/octet-stream` AND `x-amz-meta-upload-token: <uploadToken>`. S3 enforces the token — wrong or missing = `403`
10. S3 responds `200` — upload complete → **triggers Flow 2**

---

## Flow 2: Artifact Processing (S3 Event → ACTIVE)

Runs automatically after the binary is uploaded to S3. No admin action needed.

```
S3                  artifact_processor Lambda    Secrets Manager    DynamoDB (packages)
 │                           │                         │                    │
 │  s3:ObjectCreated:Put     │                         │                    │
 │  (on digilux-ota-         │                         │                    │
 │   artifacts bucket)       │                         │                    │
 │──────────────────────────>│                         │                    │
 │                           │  Parse key:             │                    │
 │                           │  {prefix}/{pkg}/{ver}/  │                    │
 │                           │  {file}                 │                    │
 │                           │                         │                    │
 │                           │  Look up PENDING        │                    │
 │                           │  record                 │                    │
 │                           │────────────────────────────────────────────>│
 │                           │  ← item (has uploadToken│                    │
 │                           │    + expectedChecksum)  │                    │
 │                           │                         │                    │
 │                           │  head_object: read      │                    │
 │                           │  x-amz-meta-upload-token│                    │
 │<──────────────────────────│  from S3 metadata       │                    │
 │                           │                         │                    │
 │                           │  ── Security check 1 ── │                    │
 │                           │  Token from S3 metadata │                    │
 │                           │  vs stored uploadToken  │                    │
 │                           │  Mismatch → quarantine  │                    │
 │                           │  (delete S3 + CORRUPTED)│                    │
 │                           │                         │                    │
 │                           │  Stream binary from S3  │                    │
 │                           │  Compute SHA256         │                    │
 │<──────────────────────────│  (chunked, 1MB blocks)  │                    │
 │                           │                         │                    │
 │                           │  ── Security check 2 ── │                    │
 │                           │  Computed SHA256 vs     │                    │
 │                           │  expectedChecksum       │                    │
 │                           │  Mismatch → quarantine  │                    │
 │                           │  (delete S3 + CORRUPTED)│                    │
 │                           │                         │                    │
 │                           │  Get ECDSA private key  │                    │
 │                           │─────────────────────────>                   │
 │                           │  ← PEM private key      │                    │
 │                           │                         │                    │
 │                           │  Sign SHA256 with       │                    │
 │                           │  ECDSA P-256            │                    │
 │                           │                         │                    │
 │                           │  Update record:         │                    │
 │                           │  status PENDING→ACTIVE  │                    │
 │                           │  + sha256, signature,   │                    │
 │                           │  artifactSize           │                    │
 │                           │  REMOVE uploadToken,    │                    │
 │                           │  expectedChecksum       │                    │
 │                           │────────────────────────────────────────────>│
 │                           │                         │                    │
 │                           │  Emit audit log:        │                    │
 │                           │  PACKAGE_REGISTERED_    │                    │
 │                           │  ACTIVE                 │                    │
```

### Step-by-step

1. S3 fires `s3:ObjectCreated:Put` event to `artifact_processor` Lambda
2. Lambda parses the S3 key: `{prefix}/{packageName}/{version}/{fileName}`
3. Lambda looks up the `PENDING` DynamoDB record. Skips if already `ACTIVE` (idempotent). Deletes S3 object if no record found (orphan upload protection).
4. **Security check 1 — Upload token binding:** Lambda calls `head_object` to read `x-amz-meta-upload-token` from S3 object metadata, compares against `uploadToken` stored in DynamoDB. Mismatch = rogue upload → quarantine (delete S3 object, mark `CORRUPTED`, emit audit log)
5. Lambda streams the binary from S3 and computes SHA256 (chunked, 1 MB blocks)
6. **Security check 2 — Checksum validation:** If `expectedChecksum` was stored, compares computed SHA256 against it. Mismatch = corrupted/tampered binary → quarantine
7. Lambda fetches the ECDSA P-256 private key from Secrets Manager
8. Lambda signs the SHA256 hex string with ECDSA → base64 signature
9. Lambda updates the DynamoDB record: `status = ACTIVE`, writes `sha256`, `signature`, `artifactSize`; **removes** `uploadToken` and `expectedChecksum` (no longer needed)
10. Lambda emits `PACKAGE_REGISTERED_ACTIVE` audit log

> **CORRUPTED status:** A package marked `CORRUPTED` is permanently rejected. Any future upload attempts to the same `packageName@version` are automatically deleted by the processor.
9. Package is now deployable — typically completes within **2–3 seconds** of S3 upload

> **Idempotency:** If S3 fires a duplicate event (e.g. retry), the Lambda skips processing if the record is already `ACTIVE`.

---

## Flow 3: Device Registration (Agent Startup)

Runs every time the OTA agent starts on a controller. Self-registers the device into the inventory.

```
Controller (OTA Agent)    IoT Core              device_register Lambda    DynamoDB (inventory)
         │                    │                          │                        │
         │  systemd starts    │                          │                        │
         │  OTA agent         │                          │                        │
         │                    │                          │                        │
         │  TLS connect       │                          │                        │
         │  (port 8883,       │                          │                        │
         │   device cert)     │                          │                        │
         │───────────────────>│                          │                        │
         │  ← CONNACK         │                          │                        │
         │                    │                          │                        │
         │  Subscribe to:     │                          │                        │
         │  jobs/notify-next  │                          │                        │
         │  jobs/$next/get/   │                          │                        │
         │  accepted          │                          │                        │
         │───────────────────>│                          │                        │
         │                    │                          │                        │
         │  Publish to:       │                          │                        │
         │  iot/device/       │                          │                        │
         │  {thingName}/      │                          │                        │
         │  ota/register      │                          │                        │
         │  { deviceId,       │                          │                        │
         │    thingName,      │                          │                        │
         │    model,          │                          │                        │
         │    hwRevision,     │                          │                        │
         │    installedVers } │                          │                        │
         │───────────────────>│                          │                        │
         │                    │  IoT Rule fires:         │                        │
         │                    │  digilux_ota_            │                        │
         │                    │  device_register         │                        │
         │                    │─────────────────────────>│                        │
         │                    │                          │  Upsert device record  │
         │                    │                          │  (deviceId, thingName, │
         │                    │                          │   model, hwRevision,   │
         │                    │                          │   installedVersions,   │
         │                    │                          │   lastSeen)            │
         │                    │                          │───────────────────────>│
         │                    │                          │                        │
         │  Publish to        │                          │                        │
         │  jobs/$next/get    │                          │                        │
         │  (request jobs)    │                          │                        │
         │───────────────────>│                          │                        │
         │  ← pending job     │                          │                        │
         │    (if any)        │                          │                        │
```

### Step-by-step

1. systemd starts the OTA agent on boot
2. Agent reads installed versions from disk (`/var/lib/digilux/ota/installed_versions.json`)
3. Agent connects to AWS IoT Core over MQTT/TLS port 8883 using its device certificate
4. Agent subscribes to IoT Jobs notification topics
5. Agent publishes a registration message to `iot/device/{thingName}/ota/register` with device metadata and installed versions
6. IoT Rule `digilux_ota_device_register` invokes `device_register` Lambda
7. Lambda upserts OTA fields (`thingName`, `installedVersions`, `pendingJobId`) on the device record in `digilux_device_data` (creates on first boot, updates on subsequent boots — corrects installed version after TIMED_OUT jobs)
8. Agent publishes to `jobs/$next/get` to fetch any pending jobs
9. If a pending job exists, IoT Core delivers it immediately → **Flow 4 begins**

---

## Flow 4: Deployment Creation

Admin triggers a deployment to push a package to a device or device group.

```
Admin            API Gateway       job_create Lambda    DynamoDB (packages/inventory/jobs)    IoT Core
  │                  │                   │                           │                            │
  │  POST            │                   │                           │                            │
  │  /deployments    │                   │                           │                            │
  │  { packageName,  │                   │                           │                            │
  │    version,      │                   │                           │                            │
  │    targetType:   │                   │                           │                            │
  │    "THING",      │                   │                           │                            │
  │    targetId,     │                   │                           │                            │
  │    rolloutStage} │                   │                           │                            │
  │─────────────────>│                   │                           │                            │
  │                  │  Validate admin   │                           │                            │
  │                  │  token            │                           │                            │
  │                  │──────────────────>│                           │                            │
  │                  │                   │  Verify package ACTIVE    │                            │
  │                  │                   │──────────────────────────>│                            │
  │                  │                   │  Verify device not        │                            │
  │                  │                   │  already on version       │                            │
  │                  │                   │  Resolve thingName        │                            │
  │                  │                   │──────────────────────────>│                            │
  │                  │                   │  Generate pre-signed      │                            │
  │                  │                   │  GET URL for artifact     │                            │
  │                  │                   │  (1-hour expiry)          │                            │
  │                  │                   │                           │                            │
  │                  │                   │  Create IoT Job with:     │                            │
  │                  │                   │  - job document           │                            │
  │                  │                   │    (presignedUrl,         │                            │
  │                  │                   │     sha256, signature,    │                            │
  │                  │                   │     packageName, version) │                            │
  │                  │                   │  - rollout config         │                            │
  │                  │                   │  - abort config           │                            │
  │                  │                   │  - 24-hr timeout          │                            │
  │                  │                   │───────────────────────────────────────────────────────>│
  │                  │                   │  ← { jobId, jobArn }      │                            │
  │                  │                   │                           │                            │
  │                  │                   │  Write job record         │                            │
  │                  │                   │  status=QUEUED            │                            │
  │                  │                   │  Set device pendingJobId  │                            │
  │                  │                   │──────────────────────────>│                            │
  │                  │                   │                           │                            │
  │                  │                   │  Emit audit log:          │                            │
  │                  │                   │  DEPLOYMENT_CREATED       │                            │
  │  ← 201           │                   │                           │                            │
  │  { jobId,        │                   │                           │                            │
  │    status:       │                   │                           │                            │
  │    "QUEUED" }    │                   │                           │                            │
```

### Step-by-step

1. Admin calls `POST /api/v1/ota/deployments`
2. `job_create` Lambda validates admin token and request body
3. Lambda fetches the package from `digilux_ota_packages` — returns `404` if not found, `400` if not `ACTIVE`
4. For `THING` target: Lambda queries `digilux_device_data` — returns `400` if device already has this version installed
5. Lambda generates a **CloudFront signed URL** for the artifact (tiered expiry: 1hr for <50MB, 6hr for <200MB, 24hr for <500MB, 48hr for larger) and embeds it in the IoT Job document
6. Lambda calls `iot:CreateJob` with the job document, rollout config (CANARY/BETA/PRODUCTION rates), and 24-hour in-progress timeout (`IOT_JOB_TIMEOUT_MINUTES=1440`)
7. Lambda writes a `QUEUED` record to `digilux_ota_jobs` and sets `pendingJobId` on `digilux_device_data`
8. Admin receives `{ jobId, status: "QUEUED" }` → **IoT Core queues the job for the device → Flow 5 begins**

### Validation errors

| Condition | Response |
|---|---|
| Package not found | `404` |
| Package not `ACTIVE` | `400` |
| Device already on this version | `400` |
| Device not in inventory | `404` |
| Invalid `targetType` | `400` |

---

## Flow 5: Device Update — Happy Path

The device receives the job, downloads and verifies the artifact, installs it, and reports success.

```
Controller (OTA Agent)    IoT Core (Jobs)    status_handler Lambda    DynamoDB (jobs/inventory)
         │                     │                      │                         │
         │  (job delivered     │                      │                         │
         │   via notify-next   │                      │                         │
         │   or get/accepted)  │                      │                         │
         │<────────────────────│                      │                         │
         │                     │                      │                         │
         │  Phase 1/3:         │                      │                         │
         │  Publish IN_PROGRESS│                      │                         │
         │  progress=5         │                      │                         │
         │  "Starting download"│                      │                         │
         │────────────────────>│                      │                         │
         │                     │  IoT Rule fires      │                         │
         │                     │─────────────────────>│  Update job status      │
         │                     │                      │  deviceStatuses         │
         │                     │                      │────────────────────────>│
         │                     │                      │                         │
         │  Download artifact  │                      │                         │
         │  from pre-signed    │                      │                         │
         │  S3 URL (HTTPS)     │                      │                         │
         │  Verify SHA256      │                      │                         │
         │  Verify ECDSA sig   │                      │                         │
         │  (with local        │                      │                         │
         │  public key)        │                      │                         │
         │                     │                      │                         │
         │  Phase 2/3:         │                      │                         │
         │  Publish IN_PROGRESS│                      │                         │
         │  progress=20-95     │                      │                         │
         │  (handler updates   │                      │                         │
         │   as it progresses) │                      │                         │
         │────────────────────>│ (IoT Rule → Lambda) │────────────────────────>│
         │                     │                      │                         │
         │  Run handler:       │                      │                         │
         │  - Backup current   │                      │                         │
         │    version          │                      │                         │
         │  - Stop service     │                      │                         │
         │  - Extract artifact │                      │                         │
         │  - Health check     │                      │                         │
         │  - Start service    │                      │                         │
         │                     │                      │                         │
         │  Phase 3/3:         │                      │                         │
         │  Save version to    │                      │                         │
         │  disk               │                      │                         │
         │                     │                      │                         │
         │  Publish SUCCEEDED  │                      │                         │
         │  progress=100       │                      │                         │
         │────────────────────>│                      │                         │
         │                     │  IoT Rule fires      │                         │
         │                     │─────────────────────>│                         │
         │                     │                      │  Update job → SUCCEEDED │
         │                     │                      │  Update device:         │
         │                     │                      │  installedVersions      │
         │                     │                      │  pendingJobId = null    │
         │                     │                      │────────────────────────>│
         │                     │                      │                         │
         │                     │                      │  Emit audit logs:       │
         │                     │                      │  DEVICE_UPDATE_SUCCEEDED│
         │                     │                      │                         │
         │  Update Device      │                      │                         │
         │  Shadow (ota-state) │                      │                         │
         │────────────────────>│                      │                         │
         │                     │                      │                         │
         │  Request next job   │                      │                         │
         │────────────────────>│                      │                         │
```

### Step-by-step

1. IoT Core delivers the job document to the device via `jobs/notify-next` MQTT topic
2. Agent reports `IN_PROGRESS (5%)` — "Starting download"
3. **Download:** Agent downloads artifact from pre-signed S3 URL via HTTPS. Uses `.tmp` extension during download
4. **Verify SHA256:** Agent computes SHA256 of downloaded file — aborts if mismatch
5. **Verify ECDSA:** Agent verifies ECDSA P-256 signature using the public key at `/etc/digilux/ota-agent.pub` — aborts if invalid
6. Agent reports `IN_PROGRESS (20%)` — "Running handler"
7. **Handler runs** (progress mapped 20–95%):
   - Backs up current installation (up to 3 rolling backups kept)
   - Stops the running service
   - Extracts and installs the new artifact
   - Runs health check
   - Restarts service
8. Agent saves new installed version to disk (`/var/lib/digilux/ota/installed_versions.json`)
9. Agent publishes `SUCCEEDED (100%)` to both `$aws/things/{thing}/jobs/{jobId}/update` and `iot/device/{thing}/ota/status`
10. IoT Rule `digilux_ota_status_ingest` fires → `status_handler` Lambda:
    - Updates job status to `SUCCEEDED` in `digilux_ota_jobs`
    - Updates `installedVersions` in `digilux_device_data`
    - Clears `pendingJobId`
    - Emits `DEVICE_UPDATE_SUCCEEDED` audit log
11. Agent updates Device Shadow with new versions
12. Agent requests next pending job

---

## Flow 6: Partial Download Failure (Internet Lost)

The download is interrupted. The agent retries automatically with backoff.

```
Controller (OTA Agent)                    S3 (Pre-signed URL)
         │                                        │
         │  Attempt 1: Download artifact          │
         │───────────────────────────────────────>│
         │  ← 40% received                        │
         │  Connection drops                      │
         │  Delete .tmp file                      │
         │  Wait 30 seconds                       │
         │                                        │
         │  Attempt 2: Download artifact          │
         │───────────────────────────────────────>│
         │  ← 80% received                        │
         │  Connection drops                      │
         │  Delete .tmp file                      │
         │  Wait 60 seconds                       │
         │                                        │
         │  Attempt 3: Download artifact          │
         │───────────────────────────────────────>│
         │  ← Connection drops again              │
         │  Delete .tmp file                      │
         │  Raise DownloadError                   │
         │                                        │
         │  Publish FAILED to IoT Jobs
         │  + iot/device/{thing}/ota/status
         │  detail: "Download failed after
         │           3 attempts"
         │──────────────────────> IoT Core
         │                        → status_handler Lambda
         │                        → job FAILED in DynamoDB
         │                        → pendingJobId cleared
```

### Step-by-step

1. Agent begins downloading from the pre-signed S3 URL
2. Network error occurs mid-download (`ConnectionError`, `ChunkedEncodingError`, or `ReadTimeout`)
3. Agent deletes the partial `.tmp` file — a corrupt partial file is never installed
4. Agent waits **30 seconds** and retries (Attempt 2)
5. If Attempt 2 also fails: wait **60 seconds** and retry (Attempt 3)
6. If Attempt 3 fails: agent raises `DownloadError`
7. Agent publishes `FAILED` status — cloud marks job as failed and clears `pendingJobId`
8. Admin must create a **new deployment** to retry (which generates a fresh pre-signed URL)

**Retry matrix:**

| Attempt | Max wait before retry | Retryable errors |
|---|---|---|
| 1 | 30 s | `ConnectionError`, `ChunkedEncodingError`, `ReadTimeout`, `Timeout` |
| 2 | 60 s | Same |
| 3 | — (final) | Same — raises on failure |

**Non-retryable errors** (e.g. HTTP 403 expired URL, 404 not found) fail immediately on the first occurrence — no retry.

---

## Flow 7: Installation Failure with Rollback

The artifact downloads and verifies successfully, but installation fails. The agent automatically rolls back.

```
Controller (OTA Agent)
         │
         │  Download OK ✓
         │  SHA256 OK ✓
         │  ECDSA OK ✓
         │
         │  Backup current version
         │  (stored at /var/lib/digilux/ota/backups/)
         │
         │  Stop service
         │
         │  Extract artifact → FAILS
         │  (e.g. corrupted tar, disk full)
         │
         │  Exception raised → rollback:
         │  Restore backup → service restarted
         │  with previous version
         │
         │  Publish FAILED
         │  detail: "Install failed —
         │           previous version restored"
         │────────────────────────────────>IoT Core
         │                                 → status_handler
         │                                 → job FAILED
         │                                 → pendingJobId cleared
         │                                 (device still healthy
         │                                  on old version)
```

### Step-by-step

1. Download and verification succeed
2. Handler takes a **backup** of the current installation before making any changes (up to 3 rolling backups retained)
3. Service is stopped
4. Installation begins — a failure occurs (corrupted archive, disk full, health check failure, etc.)
5. Handler catches the exception and restores the backup automatically
6. Service is restarted with the previous version
7. Agent publishes `FAILED` with `statusDetail: "Install failed — previous version restored"`
8. Cloud marks job as `FAILED`, clears `pendingJobId`
9. Device remains healthy on the previous version

---

## Flow 8: Critical Failure — NEEDS_RECOVERY

Install fails AND rollback also fails. Device may be in a broken state.

```
Controller (OTA Agent)
         │
         │  Install FAILS
         │  Attempt rollback → ALSO FAILS
         │  (e.g. disk corruption)
         │
         │  Publish FAILED
         │  detail: "NEEDS_RECOVERY: install
         │           failed and rollback failed
         │           — manual intervention
         │           required"
         │────────────────────────────────>IoT Core
         │                                 → status_handler
         │                                 → job FAILED
         │                                 → needsRecovery=true
         │                                   in audit log
         │                                 → pendingJobId cleared

CloudWatch Logs Insights (monitor for):
  filter needsRecovery = 1
  → Alert engineering team
  → Field escalation required
```

### Step-by-step

1. Installation fails
2. Rollback attempt also fails (e.g. backup files corrupted, filesystem issue)
3. Agent catches `RecoveryNeededError` (distinct error type from normal failure)
4. Agent publishes `FAILED` with `statusDetail: "NEEDS_RECOVERY: install failed and rollback failed — manual intervention required"`
5. `status_handler` Lambda records the failure and emits audit log with `needsRecovery: true`
6. **Detection:** Monitor `/aws/lambda/digilux_ota_status_handler` in CloudWatch Logs Insights:
   ```
   filter needsRecovery = 1
   | fields @timestamp, resource.deviceId, resource.jobId
   | sort @timestamp desc
   ```
7. Engineering identifies the device via `deviceStatuses[deviceId].statusDetail` in `GET /deployments/{jobId}`
8. Field team dispatches for physical or SSH recovery

> **Note:** `NEEDS_RECOVERY` does not trigger a Lambda error alarm — the cloud handles it gracefully. Detection relies on CloudWatch Logs monitoring.

---

## Flow 9: Abort Deployment

Admin cancels a deployment before all devices complete it.

```
Admin            API Gateway        job_create Lambda    IoT Core       DynamoDB (jobs/inventory)
  │                  │                    │                  │                   │
  │  POST            │                    │                  │                   │
  │  /deployments/   │                    │                  │                   │
  │  {jobId}/abort   │                    │                  │                   │
  │─────────────────>│                    │                  │                   │
  │                  │───────────────────>│                  │                   │
  │                  │                    │  Fetch job record│                   │
  │                  │                    │  (targetType,    │                   │
  │                  │                    │   targetId)      │                   │
  │                  │                    │─────────────────────────────────────>│
  │                  │                    │                  │                   │
  │                  │                    │  Cancel IoT Job  │                   │
  │                  │                    │  (force=false —  │                   │
  │                  │                    │   in-progress    │                   │
  │                  │                    │   devices not    │                   │
  │                  │                    │   interrupted)   │                   │
  │                  │                    │─────────────────>│                   │
  │                  │                    │  ← OK            │                   │
  │                  │                    │                  │                   │
  │                  │                    │  Update job:     │                   │
  │                  │                    │  CANCELLED       │                   │
  │                  │                    │  Clear device    │                   │
  │                  │                    │  pendingJobId    │                   │
  │                  │                    │─────────────────────────────────────>│
  │                  │                    │                  │                   │
  │                  │                    │  Emit audit log: │                   │
  │                  │                    │  DEPLOYMENT_     │                   │
  │                  │                    │  ABORTED         │                   │
  │  ← 200           │                    │                  │                   │
  │  { status:       │                    │                  │                   │
  │    "CANCELLED" } │                    │                  │                   │
```

### Step-by-step

1. Admin calls `POST /api/v1/ota/deployments/{jobId}/abort`
2. `job_create` Lambda fetches the job record to get `targetType` and `targetId`
3. Lambda calls `iot:CancelJob` with `force=false` — devices already installing are not interrupted; only queued devices are stopped
4. Lambda updates job status to `CANCELLED` in `digilux_ota_jobs`
5. Lambda clears `pendingJobId` on the device inventory (for `THING` targets)
6. Admin receives `{ status: "CANCELLED" }`

> Aborting an already-cancelled job returns `400` — idempotency check.

---

## Flow 10: Device Offline at Deployment Time

Admin creates a deployment but the target device is offline. The job is held by IoT Core and delivered automatically when the device reconnects.

```
Admin                  IoT Core                Controller (OTA Agent)
  │                        │                           │
  │  POST /deployments     │                           │  [OFFLINE]
  │───────────────────────>│                           │
  │  ← { jobId, QUEUED }   │                           │
  │                        │  Job stored as QUEUED     │
  │                        │  for device               │
  │                        │                           │
  │   [minutes / hours later]                          │
  │                        │                           │  Device boots
  │                        │                           │  / reconnects
  │                        │<──────────────────────────│
  │                        │  Agent subscribes to      │
  │                        │  jobs/notify-next         │
  │                        │                           │
  │                        │  Agent publishes:         │
  │                        │  ota/register             │
  │                        │  → device_register Lambda │
  │                        │  → inventory updated      │
  │                        │                           │
  │                        │  Agent requests next job: │
  │                        │  jobs/$next/get           │
  │                        │──────────────────────────>│
  │                        │  ← Job document delivered │
  │                        │  → Flow 5 begins          │
```

### Step-by-step

1. Admin creates deployment — IoT Core stores the job as `QUEUED`
2. Device is offline — IoT Core holds the job (no timeout on queued state)
3. When device comes back online and reconnects, it subscribes to job notification topics
4. Device publishes registration → inventory updated with current installed versions
5. Device requests next pending job via `jobs/$next/get`
6. IoT Core delivers the stored job document
7. Device proceeds with **Flow 5** (happy path)

> The in-progress **24-hour timeout** (`IOT_JOB_TIMEOUT_MINUTES=1440`) only starts once the device acknowledges the job and begins execution — not from when the job was created.

---

## Flow 11: Staged Fleet Rollout (CANARY → BETA → PRODUCTION)

A package is rolled out progressively across the fleet using IoT Thing Groups.

```
Stage 1: CANARY (targetType: THING_GROUP, targetId: DGX-Canary)
────────────────────────────────────────────────────────────────
  POST /deployments { rolloutStage: "CANARY", targetId: "DGX-Canary" }
  → IoT Job created with maximumPerMinute: 2
  → Deployed to up to 2 devices per minute in DGX-Canary group
  → Monitor for 24h → all SUCCEEDED?

Stage 2: BETA (targetType: THING_GROUP, targetId: DGX-Beta)
────────────────────────────────────────────────────────────
  POST /deployments { rolloutStage: "BETA", targetId: "DGX-Beta" }
  → IoT Job with exponentialRate: starts at 2/min, doubles
    every 5 SUCCEEDED devices, up to max rate
  → Deployed to ~10% of fleet
  → Monitor → all SUCCEEDED?

Stage 3: PRODUCTION (targetType: THING_GROUP, targetId: DGX-Production)
─────────────────────────────────────────────────────────────────────────
  POST /deployments { rolloutStage: "PRODUCTION", targetId: "DGX-Production" }
  → IoT Job with exponentialRate: starts at 5/min, doubles
    every 20 SUCCEEDED devices
  → Full fleet rollout
```

### Rollout configuration

| Stage | Initial Rate | Scaling | Group |
|---|---|---|---|
| `CANARY` | 2 devices/min (fixed) | None | `DGX-Canary` |
| `BETA` | 2 devices/min | ×2 every 5 successes | `DGX-Beta` |
| `PRODUCTION` | 5 devices/min | ×2 every 20 successes | `DGX-Production` |

### Auto-abort (applies to all group deployments)

IoT Jobs automatically cancels the rollout if:
- **>10% of devices FAIL** (minimum 3 devices must have executed)
- **>20% of devices TIME OUT** (minimum 3 devices must have executed)

This prevents a bad update from propagating to the full fleet.

---

## Flow 12: Status Lost After Successful Install (Reconnect Recovery)

The device installs successfully but loses MQTT connection before the `SUCCEEDED` message reaches the cloud.

```
Controller (OTA Agent)    IoT Core           status_handler Lambda
         │                    │                      │
         │  Install OK ✓      │                      │
         │  Save version      │                      │
         │  to disk ✓         │                      │
         │                    │                      │
         │  [Network drops]   │                      │
         │                    │                      │
         │  Attempt: publish  │                      │
         │  SUCCEEDED         │                      │
         │  → fails (offline) │                      │
         │                    │                      │
    ─────────────────── Within 24 hours ────────────────────────
         │                    │                      │
         │  paho-mqtt         │                      │
         │  auto-reconnects   │                      │
         │───────────────────>│                      │
         │                    │                      │
         │  Publish SUCCEEDED │                      │
         │  (buffered/retry)  │                      │
         │───────────────────>│                      │
         │                    │  IoT Rule → Lambda   │
         │                    │─────────────────────>│
         │                    │                      │  Job → SUCCEEDED
         │                    │                      │  Inventory updated
         │                    │                      │
    ──────── After 24 hours (IoT Jobs timeout) ─────────────────
         │                    │                      │
         │  [Device reboots   │                      │
         │   or reconnects]   │                      │
         │                    │                      │
         │  Agent starts →    │                      │
         │  Reads version     │                      │
         │  from disk         │                      │
         │                    │                      │
         │  Publishes         │                      │
         │  ota/register      │                      │
         │  with NEW version  │                      │
         │───────────────────>│                      │
         │                    │  device_register     │
         │                    │  Lambda fires        │
         │                    │  → inventory         │
         │                    │    corrected with    │
         │                    │    actual version    │
```

### Step-by-step

**Scenario A — Reconnects within 24 hours:**
1. paho-mqtt library auto-reconnects
2. Buffered `SUCCEEDED` message is published
3. `status_handler` Lambda updates the job and inventory normally

**Scenario B — Reconnects after 24 hours (IoT Jobs timeout):**
1. IoT Core marks the job execution as `TIMED_OUT`
2. On next startup or reconnect, the OTA agent publishes `ota/register` with the version it actually has on disk
3. `device_register` Lambda upserts the inventory with the correct installed version
4. Inventory is eventually consistent — no manual intervention required

---

## Flow 13: Compatibility Check

Admin checks what updates are available for a specific device before deploying.

```
Admin            API Gateway    compatibility_check Lambda    DynamoDB
  │                  │                   │                       │
  │  GET             │                   │                       │
  │  /controllers/   │                   │                       │
  │  {deviceId}/     │                   │                       │
  │  updates/        │                   │                       │
  │  available       │                   │                       │
  │─────────────────>│                   │                       │
  │                  │──────────────────>│                       │
  │                  │                   │  Get device record    │
  │                  │                   │  (model, hwRev,       │
  │                  │                   │   installedVersions)  │
  │                  │                   │──────────────────────>│
  │                  │                   │  Scan ACTIVE packages │
  │                  │                   │──────────────────────>│
  │                  │                   │  For each package:    │
  │                  │                   │  - Find latest version│
  │                  │                   │    (semver comparison)│
  │                  │                   │  - Skip if device     │
  │                  │                   │    already at latest  │
  │                  │                   │  - Check compat table:│
  │                  │                   │    compatibleModels,  │
  │                  │                   │    minHwRevision      │
  │                  │                   │──────────────────────>│
  │  ← 200           │                   │                       │
  │  { deviceId,     │                   │                       │
  │    installedVers,│                   │                       │
  │    pendingJobId, │                   │                       │
  │    available     │                   │                       │
  │    Updates }     │                   │                       │
```

### Compatibility filtering logic

1. Scan all `ACTIVE` packages in `digilux_ota_packages`
2. Group by `packageName`, keep the **latest version** per package (semver-style integer comparison)
3. For each package, skip if the device is already at the latest version
4. Check `digilux_ota_compatibility` table:
   - If `compatibleModels` is non-empty and device `model` is not in the list → skip
   - If `minHwRevision` is set and device `hwRevision` is below it → skip
5. Return remaining packages as `availableUpdates`

---

## MQTT Topics Reference

| Topic | Direction | Purpose |
|---|---|---|
| `iot/device/{thingName}/ota/register` | Device → Cloud | Agent startup registration |
| `iot/device/{thingName}/ota/status` | Device → Cloud | Job status updates (IN_PROGRESS, SUCCEEDED, FAILED) |
| `$aws/things/{thingName}/jobs/notify-next` | Cloud → Device | New job notification |
| `$aws/things/{thingName}/jobs/$next/get` | Device → Cloud | Request next pending job |
| `$aws/things/{thingName}/jobs/$next/get/accepted` | Cloud → Device | Job document response |
| `$aws/things/{thingName}/jobs/{jobId}/update` | Device → Cloud | IoT Jobs status update |
| `$aws/things/{thingName}/shadow/named/ota-state/update` | Device → Cloud | Device Shadow version sync |

---

## DynamoDB Tables Reference

### `digilux_ota_packages`
Key: `packageName` (hash) + `version` (range)

| Field | Description |
|---|---|
| `status` | `PENDING` → `ACTIVE` |
| `sha256` | SHA256 hex of binary (set by artifact_processor) |
| `signature` | ECDSA P-256 base64 signature (set by artifact_processor) |
| `s3Key` / `s3Bucket` | Artifact location |
| `artifactSize` | Binary size in bytes |

### `digilux_ota_jobs`
Key: `jobId` (hash)

| Field | Description |
|---|---|
| `status` | `QUEUED` → `IN_PROGRESS` → `SUCCEEDED` / `FAILED` / `CANCELLED` |
| `deviceStatuses` | Map of `thingName → { status, progress, statusDetail, error }` |
| `iotJobArn` | ARN of the AWS IoT Job |
| `pendingJobId` | Set on device inventory when QUEUED, cleared on terminal status |

### OTA fields on `digilux_device_data`
Key: `deviceId` (hash) + `macAddress` (range) — OTA Agent fields are attributes on the existing device record.

| Field | Description |
|---|---|
| `installedVersions` | `{ "controller-app": "4.0.0", ... }` — source of truth |
| `pendingJobId` | Current in-flight job, `null` if idle |
| `thingName` | IoT Thing name (MAC-based, e.g. `digilux-94ba062a250c`) |
| `lastSeen` / `lastUpdatedAt` | Timestamps |

### `digilux_ota_compatibility`
Key: `packageName` (hash) + `version` (range)

| Field | Description |
|---|---|
| `compatibleModels` | `["DGX-1000"]` — empty means all models |
| `minHwRevision` | Minimum hardware revision required |
| `dependsOn` | Package dependencies |
| `incompatibleWith` | Conflicting packages |

---

## Version History

| Version | Date | Changes |
|---|---|---|
| `1.0` | 2026-07-31 | Initial release — covers all 13 flows including package upload, device registration, deployment, happy path, failure, rollback, NEEDS_RECOVERY, abort, offline, staged rollout, reconnect recovery, and compatibility check |
| `1.1` | 2026-08-12 | Consolidated `digilux_device_inventory` into `digilux_device_data` (OTA fields as attributes); CloudFront signed URLs replace S3 presigned URLs for artifact delivery; tiered presign expiry based on artifact size (1hr–48hr); IoT Job in-progress timeout raised from 60min to 24hr (`IOT_JOB_TIMEOUT_MINUTES=1440`) |
