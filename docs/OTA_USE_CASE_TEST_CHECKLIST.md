# Digilux OTA — Use-case test checklist

**Date:** 2026-09-30  
**Base URL:** `https://iot.digilux.co.in/smarthome` (or `{{base_url}}` in Postman)  
**Postman:** `postman/Digilux_OTA.postman_collection.json` + `postman/Digilux_OTA.postman_environment.json`

Auth:

| Token | Header | Who |
|---|---|---|
| `{{admin_token}}` | `Authorization: {{admin_token}}` | Cognito `ota-admin` |
| `{{user_token}}` | `Authorization: {{user_token}}` | Homeowner (device owner) |

**How to use**

- Mark **Pass / Fail / Blocked** in the last column (or in the companion CSV).
- **Expected (today)** = what this repo actually returns.
- **Desired (your UC)** = what you asked for, if different. Fail the row if product wants Desired and API still returns Expected.
- Gateway controller `deviceType`: `Network_controller_firmware` → package `HomeAssistantUtility`.
- Build a valid artefact with `infrastructure/make_test_artifact.py` (tar + `manifest.json`). Processor **rejects** uploads that are not a tar with `manifest.json`.

**Endpoints used**

| Action | Method | Path |
|---|---|---|
| Upload URL | POST | `/api/v1/ota/packages/upload-artefact` |
| Package status | GET | `/api/v1/ota/packages/{packageName}/{version}` |
| List packages | GET | `/api/v1/ota/packages` |
| Activate / promote / recall | PATCH | `/api/v1/ota/packages/{packageName}/{version}/activate` |
| Delete package | DELETE | `/api/v1/ota/packages/{packageName}/{version}` |
| Create deployment | POST | `/api/v1/ota/deployments` |
| List deployments | GET | `/api/v1/ota/deployments` |
| Abort | POST | `/api/v1/ota/deployments/{jobId}/abort` |
| Beta users | GET/POST/DELETE | `/api/v1/ota/beta-users` |
| Available updates | GET | `/api/v1/ota/device/available-updates` |
| Consent | POST | `/api/v1/ota/my/updates/consent` |
| Job status | GET | `/api/v1/ota/my/updates/{jobId}/status` |

Current deployment body (Lambda, not the older Postman CANARY example):

```json
{
  "packageName": "HomeAssistantUtility",
  "version": "1.1.0",
  "rolloutStage": "BETA",
  "targetIds": ["<device-uuid>"]
}
```

`rolloutStage`: `BETA` | `PRODUCTION` | `CUSTOM`.  
PRODUCTION needs **no** `targetIds` (targets IoT group `DGX-Production`).

---

## UC1 — Admin upload (Gateway + S3 encryption)

| ID | Step | Request | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 1.1 | Upload Gateway package | POST upload-artefact | 200, `packageName=HomeAssistantUtility`, `deviceType=Network_controller_firmware`, `status=PENDING`, `s3Key` starts with `Network_controller_firmware/Network_controller_firmware/` | Same + always succeeds for valid types | |
| 1.2 | PUT binary | PUT `uploadUrl`, headers `Content-Type: application/octet-stream` and `x-amz-meta-upload-token: <token>` | 200 from S3 | Same | |
| 1.3 | Poll until processed | GET package status | `status=ACTIVE`, encrypted artefact (`encS3Key` / opaque key), SHA256 + signature | Encrypted in S3 | |
| 1.4 | Invalid `deviceType` | `"deviceType": "Unknown_box"` | 400 `Invalid deviceType. Must be one of: ...` | Managed types only | |
| 1.5 | Default filename | omit `fileName` | `HomeAssistantUtility-{version}.jar` in response | You asked Gateway `.tar` (UC14) | |

**Upload body (valid):**

```json
{
  "deviceType": "Network_controller_firmware",
  "version": "9.9.1",
  "releaseType": "BETA",
  "checksum": "<sha256 of the file you will PUT>",
  "releaseNotes": "QA UC1 gateway upload",
  "fileName": "HomeAssistantUtility-9.9.1.tar"
}
```

Force `.tar` via `fileName` even though the map default is `.jar`. The processor still requires a real tar + `manifest.json`.

---

## UC2 — Upload ≠ deploy

| ID | Step | Request | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 2.1 | After ACTIVE, **before** any deployment | GET available-updates as homeowner | No new version from this package (no `LATEST#*` pointer yet) | Not offered until admin deploys | |
| 2.2 | Create BETA/CUSTOM/PROD deployment | POST deployments | Deployment `status=ACTIVE` (not IoT job at campaign level) | Same | |
| 2.3 | After deploy | GET available-updates | Version appears per pointer rules | Same | |
| 2.4 | Do **not** call PATCH `activated:true` in this test | — | That path can write LATEST pointers **without** a deployment | Upload must not publish | |

`releaseType` on upload may be `UAT` / `BETA` / `PROD` / `CUSTOM`. Deployments only accept `BETA` / `PRODUCTION` / `CUSTOM`.

---

## UC3 / UC4 — Delete package vs jobs

| ID | Step | Request | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 3.1 | Delete **PENDING** (never ACTIVE) | DELETE + reason | 200 hard delete, `recordRetained: false`, `s3Deleted: true` | Allowed if never deployed | |
| 3.2 | Delete **ACTIVE** | DELETE | **409** must recall/supersede first | UC3: allow if no IN_PROGRESS job | |
| 3.3 | Delete with jobs **QUEUED** | DELETE | **409** + `activeJobs` | UC3 might allow (not IN_PROGRESS) | |
| 4.1 | Delete with job **IN_PROGRESS** | DELETE | **409** | Block + dialog | |
| 4.2 | Delete with job **COMPLETED** | DELETE | **409** | Block + count of consumers **excluding beta-list devices** | |
| 4.3 | Dialog payload | 409 body | `{ "error": "...QUEUED, IN_PROGRESS, or COMPLETED...", "activeJobs": ["job-id"] }` | Count of devices that consumed firmware, minus beta list | |

**Delete body:**

```json
{
  "reason": "QA cleanup UC3",
  "force": false
}
```

**Missing reason:**

```json
{}
```

Expect **400** `A deletion reason is required.`

---

## UC5 — CUSTOM cannot promote to PROD / beta list

| ID | Step | Request | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 5.1 | Promote CUSTOM package | PATCH `{ "promote": true }` | **409** only BETA can be promoted | Cannot promote CUSTOM | |
| 5.2 | Add CUSTOM target user to beta list | POST `/ota/beta-users` `{ "email": "..." }` | **201** — **not blocked** | Must not land on beta list | |
| 5.3 | Create BETA deploy with same `targetIds` as CUSTOM | POST deployments BETA | **201** — **not blocked** | Must not promote those IDs into BETA | |

---

## UC6 — Version priority Custom → Beta → Prod

| ID | Setup | GET available-updates | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 6.1 | CUSTOM pointer for this device + BETA + PROD | `releaseType` / `availableVersion` | **CUSTOM wins** always | Custom > Beta > Prod | |
| 6.2 | Beta **user** (in DDB list), no CUSTOM, BETA 1.1 and PROD 2.0 | | **PROD 2.0** (newer wins) | Always Beta until delisted (UC18) | |
| 6.3 | Non-beta user, BETA + PROD | | **PROD only** | Prod | |

---

## UC7 — Abort / delete CUSTOM

| ID | Step | Request | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 7.1 | Abort CUSTOM, no IN_PROGRESS jobs | POST abort + reason | 200 `status=CANCELLED`, `abortReason` stored | Allowed | |
| 7.2 | Abort while IN_PROGRESS | POST abort | **409** `inProgressJobs` | Cannot abort | |
| 7.3 | Abort with COMPLETED jobs only | POST abort | **200** (COMPLETED does not block abort) | Align with product | |
| 7.4 | Delete CUSTOM package after abort, no IN_PROGRESS | DELETE | Still **409 if package ACTIVE or COMPLETED jobs remain** | Delete if no IN_PROGRESS | |

**Abort body (required):**

```json
{
  "reason": "QA abort CUSTOM — wrong target list"
}
```

Empty reason → **400** `An abort reason is required.`

---

## UC8 — One active deployment per category

| ID | Step | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 8.1 | Second **BETA** deploy for same package | Previous BETA deployment `CANCELLED` (`SUPERSEDED_BY_...`), new one `ACTIVE` | Same | |
| 8.2 | BETA + PROD + CUSTOM all exist | All three can be ACTIVE together (one each) | Confirm this is intended | |
| 8.3 | List deployments | GET `/deployments` newest `createdAt` first | — | |

---

## UC9 — Same BETA package to PROD via **new** deployment

| ID | Step | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 9.1 | PATCH `{ "promote": true }` on BETA package | Flips `releaseType` to PROD, writes `LATEST#PROD`, **deletes `LATEST#BETA`**, **no new deployment row** | You asked for a **new PRODUCTION deployment** | |
| 9.2 | POST deployments `rolloutStage=PRODUCTION` same version | New deployment, `targetGroup=DGX-Production` | Preferred path for UC9 | |

---

## UC10 — One IN_PROGRESS job visible until timeout/complete

| ID | GET available-updates | Expected (today) | Result |
|---|---|---|---|
| 10.1 | Device has QUEUED or IN_PROGRESS | `otaStatus: JOB_ACTIVE`, `activeJob.status` in `QUEUED`/`IN_PROGRESS`, **no** `availableVersion` | |

```json
{
  "status": "success",
  "message": "",
  "devices": [
    {
      "deviceId": "<uuid>",
      "otaStatus": "JOB_ACTIVE",
      "package": "HomeAssistantUtility",
      "installedVersion": "1.0.0",
      "activeJob": {
        "jobId": "digilux-ota-HomeAssistantUtility-1-1-0-...",
        "status": "IN_PROGRESS",
        "version": "1.1.0",
        "message": "Your Firmware update ver 1.1.0 is in progress, please check after some time for status. Note: Please ensure the controller is Powered on."
      }
    }
  ]
}
```

---

## UC11 — Production for all DGX-Production (incl. beta devices)

| ID | Step | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 11.1 | PRODUCTION deploy | `targetGroup`: **`DGX-Production` only** | Also **`DGX-Gateways`** | |
| 11.2 | Beta user, PROD newer than BETA | Sees **PROD** | All production members including beta | |
| 11.3 | Beta user, BETA newer than PROD | Sees **BETA**, not PROD | Conflicts with UC18 | |

---

## UC12 — Release notes + abort reason

| ID | Step | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 12.1 | Upload **without** `releaseNotes` | **400** (required, 20–500 chars) | **Mandatory** | |
| 12.2 | Abort **without** `reason` | **400** | Mandatory | |
| 12.3 | Abort **with** `reason` | Stored `cancelledReason` / response `abortReason` | Stored | |

---

## UC13 — Delete S3 + DynamoDB

| ID | Package status | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 13.1 | PENDING / CORRUPTED / SUPERSEDED (after cooling-off or `force`) | S3 deleted + DDB **hard** delete | Both gone | |
| 13.2 | RECALLED then DELETE | S3 deleted, DDB **kept** `status=DELETED`, `recordRetained: true` | You asked both deleted | |

---

## UC14 — Always `.tar` + manifest

| ID | Step | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 14.1 | Default Gateway filename | `.jar` | Always `.tar` | |
| 14.2 | PUT a non-tar / no `manifest.json` | Package `CORRUPTED`, S3 object quarantined/deleted | Reject | |
| 14.3 | Valid tar + `manifest.json` with `files[]` | ACTIVE, manifest enriched with per-file sha256/size | Required | |

`make_test_artifact.py` example:

```bash
python infrastructure/make_test_artifact.py \
  --device-type Network_controller_firmware \
  --version 9.9.1 \
  --release-type BETA
```

---

## UC15 — Admin UI sort by create/update time

| ID | Surface | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 15.1 | This repo | **No Admin UI** | Sort by create/update | Test on portal separately |
| 15.2 | GET `/packages` | Sort `createdAt` **desc** (fixed 2026-10-05) | Date sort | |
| 15.3 | GET `/deployments` | Sort `createdAt` **desc** | Date sort | |

---

## UC16 — Beta users from DynamoDB

| ID | Step | Expected (today) | Result |
|---|---|---|---|
| 16.1 | GET `/ota/beta-users` | Rows from `digilux_ota_beta_users` | |
| 16.2 | POST add by email | Resolves Cognito → `userId` → device, PutItem | |
| 16.3 | DELETE by `userId` | Removed | |
| 16.4 | Create BETA deploy **without** copying that table | Admin must send `targetIds`; list is **not** auto-applied | |

```json
{ "email": "beta.tester@example.com" }
```

---

## UC17 — Idle device shows latest PROD

| ID | Setup | GET available-updates | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 17.1 | Non-beta, no job, PROD pointer | `availableVersion` = PROD | Latest prod | |
| 17.2 | Beta user, BETA newer | May show **BETA** | Always prod if job not started | |

---

## UC18 — Beta list users always see BETA until removed

| ID | Setup | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 18.1 | In beta table, BETA < PROD | Shows **PROD** | Show **BETA** until delisted | |
| 18.2 | Remove from beta table | BETA pointer ignored | Prod (or none) | |

---

## UC19 — Consent → THING job QUEUED

| ID | POST consent | Expected (today) | Result |
|---|---|---|---|
| 19.1 | Happy path | **202**, `status: "QUEUED"`, new `jobId`, IoT target `thing/{thingName}` | |

**Request:**

```json
{
  "deviceId": "{{device_id}}",
  "packageName": "HomeAssistantUtility",
  "version": "1.1.0",
  "accepted": true
}
```

**Response (shape):**

```json
{
  "jobId": "digilux-ota-HomeAssistantUtility-1-1-0-...",
  "deviceId": "...",
  "packageName": "HomeAssistantUtility",
  "version": "1.1.0",
  "status": "QUEUED",
  "message": "Update accepted. Your device will download and install the update shortly. Use the status endpoint to track progress."
}
```

---

## UC20 — Timeout + retry new jobId

| ID | Step | Expected (today) | Result |
|---|---|---|---|
| 20.1 | Device offline longer than IoT `inProgressTimeoutInMinutes` (~24h) | Job `TIMED_OUT` in DDB | |
| 20.2 | GET available-updates | `lastFailedJob.status: "TIMED_OUT"`, message *Your firmware update ver {version} timed out. Please retry.* | |
| 20.3 | POST consent same version | **202**, **new** `jobId`, `"retried": true`, `status: QUEUED` | |
| 20.4 | Job stale ~7 days (job_sync) | May become **CANCELLED** not TIMED_OUT | |

Timed-out message:

`Your firmware update ver 1.1.0 timed out. Please retry.`

---

## UC21 — IN_PROGRESS progress copy

Covered by UC10 `activeJob.message` (see JSON above). **Pass** if that string matches.

---

## UC22 — Completed then check for newer

| ID | Setup | GET available-updates | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|---|
| 22.1 | Job SUCCEEDED, installed == offered | `otaStatus: JOB_COMPLETED`, `lastCompletedJob.message` success copy | Show completed | |
| 22.2 | Newer deployment exists | `REGISTERED` + `availableVersion` (may include `lastCompletedJob`) | After user taps “check newer” | |

Success copy:

`Your firmware ver 1.1.0 update was successful.`

There is **no** query param for “user tapped check newer”; second GET is the check.

---

## UC23 — Duplicate consent while in progress

| ID | POST consent again | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 23.1 | pendingJobId still QUEUED/IN_PROGRESS | **409** | IN_PROGRESS message | |

```json
{
  "error": "An update is already in progress on this device.",
  "pendingJobId": "digilux-ota-..."
}
```

Not the same string as available-updates `in_progress`.

---

## UC24 — Corrupt / malicious fail copy

| ID | GET available-updates after FAILED | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 24.1 | Any FAILED job | `Your last firmware ver {version} update failed. Please contact support.` | *There is some problem with this firmware version to download v.1.0.0, kindly talk to customer support* | |

No separate reason code for corrupt vs other fail.

---

## UC25 — Keep FAILED until user asks for newer

| ID | GET available-updates | Expected (today) | Desired (UC) | Result |
|---|---|---|---|---|
| 25.1 | Last job FAILED, newer (or same offerable) version exists | `REGISTERED` + `availableVersion` **and** `lastFailedJob` in **one** payload | Failed **only** until app button; then offer new version | |

```json
{
  "deviceId": "...",
  "otaStatus": "REGISTERED",
  "package": "HomeAssistantUtility",
  "installedVersion": "1.0.0",
  "availableVersion": "1.2.0",
  "lastFailedJob": {
    "jobId": "...",
    "status": "FAILED",
    "version": "1.1.0",
    "message": "Your last firmware ver 1.1.0 update failed. Please contact support."
  }
}
```

App should hide `availableVersion` until “check newer” if that is the product rule.

---

## Product conflicts (do not fail both as bugs)

| Pair | Issue |
|---|---|
| UC11 vs UC18 | Prod-for-everyone including beta vs always-show-BETA-while-listed |
| UC3 vs UC4 | Delete if not IN_PROGRESS vs never delete if COMPLETED |
| UC1 vs UC14 | Gateway default `.jar` vs always `.tar` |

Resolve these in writing before marking related rows Fail.

---

## Smoke sequence (happy path)

1. Admin token → POST upload-artefact (`releaseType: BETA`, unique version).  
2. PUT artefact (tar + manifest).  
3. Poll GET package until `ACTIVE`.  
4. GET available-updates as user → **no** this version.  
5. POST deployments BETA with `targetIds`.  
6. GET available-updates → `availableVersion` matches.  
7. POST consent → 202 `QUEUED`.  
8. GET available-updates → `JOB_ACTIVE`.  
9. POST consent again → 409.  
10. After success/fail/timeout, re-check UC20–25 messages.

---

## Files in this folder

| File | Use |
|---|---|
| `OTA_USE_CASE_TEST_CHECKLIST.md` | This document |
| `OTA_USE_CASE_TEST_CHECKLIST.csv` | Spreadsheet Pass/Fail tracker |
