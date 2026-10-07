# OTA System — Corrective Action Record
**Document:** OTA-CAR-2026-10-07
**Standard:** ASD-STE100 Issue 7
**Date:** 2026-10-07
**System:** Digilux OTA Update System
**Author:** Mahesh Maney
**Status:** RELEASED

---

## 1. PURPOSE

This document describes ten defects found in the Digilux OTA system.
It describes the cause of each defect and the corrective action taken.
Use this document as a reference for future system changes.

---

## 2. SCOPE

This document covers changes to:

- Lambda functions in the `aws-ota-system` repository
- Admin web interface in the `digilux-ota-admin-ui` repository
- End-to-end test scripts

---

## 3. DEFINITIONS

| Term | Definition |
|------|-----------|
| Artifact | The firmware file that the system delivers to a device |
| Deployment | A rollout campaign that offers an artifact to a set of devices |
| Package | The metadata record and artifact for a specific firmware version |
| Recall | The action of marking a package as unsafe for deployment |
| Rollout stage | The scope of a deployment: BETA, PRODUCTION, or CUSTOM |
| S3 | Amazon Simple Storage Service — the artifact storage system |
| Thing Group | A named group of IoT devices in AWS IoT Core |

---

## 4. CORRECTIVE ACTIONS

---

### 4.1 ISSUE 1 — Package Recall Allowed With Active Deployment

**Reference:** CAR-2026-10-07-01

#### 4.1.1 Problem Statement

The system allowed an administrator to recall a package while an active deployment existed for that package.
This action caused inconsistent system state.
The deployment record remained ACTIVE in the database.
Devices that were mid-download continued to receive the recalled artifact.
No audit record linked the recall action to the deployment.

#### 4.1.2 Root Cause

The `digilux_ota_package_activate` Lambda did not check for active deployments before it processed a recall request.

#### 4.1.3 Corrective Action

**Lambda:** `digilux_ota_package_activate`

The Lambda now performs the following checks before it processes a recall:

1. The system checks that the package status is ACTIVE.
   If the status is not ACTIVE, the system returns HTTP 409.

2. The system queries `digilux_ota_deployments` for any ACTIVE deployment that references this package version.
   If an active deployment exists, the system returns HTTP 409 with the deployment ID.
   The administrator must abort the deployment before the system allows the recall.

3. When no active deployment exists, the system marks the package as RECALLED.
   The system deletes the `LATEST#` pointer so devices stop receiving the update.
   The system cancels all QUEUED IoT jobs for this package version.

**UI:** `PackagesPage.jsx`

The Recall button is restored on ACTIVE packages.
The browser `prompt()` dialog is replaced with a modal that contains a text area for the recall reason.
The Confirm Recall button is disabled until the administrator enters a reason.
If the backend returns HTTP 409, the modal shows the error message inline.

**Lambda:** `digilux_ota_job_create`

The Lambda now returns HTTP 400 when the package status is RECALLED.
The error message tells the administrator to upload a new version.

#### 4.1.4 Enforced Procedure

The administrator must follow this procedure to recall a package:

1. Go to the Deployments page.
2. Find the active deployment for the package version.
3. Click Abort and enter a reason.
4. Go to the Packages page.
5. Click Recall on the package and enter a reason.

---

### 4.2 ISSUE 2 — Abort Used Browser Dialog With No Reason Field

**Reference:** CAR-2026-10-07-02

#### 4.2.1 Problem Statement

The Abort button on the Deployment Detail page used the browser `confirm()` dialog.
This dialog does not have a text input field.
The system sent an empty body to the abort endpoint.
The `digilux_ota_job_create` Lambda required a non-empty reason field.
All abort requests returned HTTP 400.
Abort was non-functional.

#### 4.2.2 Root Cause

The `handleAbort` function in `DeploymentDetailPage.jsx` sent `{}` as the POST body.
The backend required `{ reason: "..." }`.

#### 4.2.3 Corrective Action

**UI:** `DeploymentDetailPage.jsx`

The browser `confirm()` dialog is replaced with an inline modal.
The modal contains a full-width text area for the abort reason.
The Confirm Abort button is disabled until the administrator enters a reason.
The system sends `{ reason: abortReason.trim() }` in the POST body.

The backend stores the reason in the `cancelledReason` field of the deployment record.
The backend writes the reason to the CloudWatch audit log.

---

### 4.3 ISSUE 3 — Upload Page Blocked Re-Upload of Deleted Versions

**Reference:** CAR-2026-10-07-03

#### 4.3.1 Problem Statement

The upload page fetched all packages and stored their version numbers for validation.
The validation included DELETED packages.
When an administrator tried to upload a version that had been deleted, the UI blocked the upload.
The error message said the version already existed.
The backend correctly allowed re-upload of deleted versions.
The frontend validation was more restrictive than the backend.

#### 4.3.2 Root Cause

The `useEffect` hook in `UploadPage.jsx` stored only the version string for each package.
The `validateVersion` function compared the new version against all stored versions, including DELETED ones.

#### 4.3.3 Corrective Action

**UI:** `UploadPage.jsx`

The system now stores `{ version, status }` for each package instead of only the version string.
The `validateVersion` function filters out packages with status DELETED before it performs any comparison.

The following status values still block re-upload of the same version:

| Status | Blocks re-upload |
|--------|-----------------|
| ACTIVE | Yes |
| PENDING | Yes |
| CORRUPTED | Yes |
| SUPERSEDED | Yes |
| RECALLED | Yes |
| DELETED | No — version slot is free |

---

### 4.4 ISSUE 4 — Encrypted Artifacts Stored With No Folder Organisation

**Reference:** CAR-2026-10-07-04

#### 4.4.1 Problem Statement

The `digilux_ota_artifact_processor` Lambda stored encrypted artifacts and signature files as flat UUID paths:

```
enc/<uuid>.enc
sig/<uuid>.sig
```

All artifacts appeared in the same S3 prefix regardless of device type.
An administrator could not identify which artifacts belonged to which device type.
S3 lifecycle policies and IAM policies could not be scoped per device type.

#### 4.4.2 Root Cause

The `_enc_s3_key` and `_sig_s3_key` functions generated paths with no device type prefix.

#### 4.4.3 Corrective Action

**Lambda:** `digilux_ota_artifact_processor`

The functions now include the device type as a prefix:

```
{deviceType}/enc/<uuid>.enc
{deviceType}/sig/<uuid>.sig
```

Example:
```
Network_controller_firmware/enc/a3f7c2d1-...enc
Network_controller_firmware/sig/d3aa143b-...sig
```

The UUID component is preserved.
The package name and version do not appear in the path.
Presigned URLs do not leak package or version information.

---

### 4.5 ISSUE 5 — Duplicate Deployments Were Auto-Superseded Without Warning

**Reference:** CAR-2026-10-07-05

#### 4.5.1 Problem Statement

When an administrator created a deployment for a package that already had an active deployment at the same rollout stage, the system silently cancelled the existing deployment.
The existing deployment was cancelled with no human-readable reason.
The administrator received no warning.
Devices that had already accepted the first deployment had their IoT jobs orphaned.

#### 4.5.2 Root Cause

The `digilux_ota_job_create` Lambda called `_supersede_deployment` when it found an active deployment.
This function cancelled the existing deployment automatically.

#### 4.5.3 Corrective Action

**Lambda:** `digilux_ota_job_create`

The system now returns HTTP 409 when an active deployment exists for the same package and rollout stage.
The response body contains the existing deployment ID and version.
The administrator must abort the existing deployment before the system creates a new one.

The following rule applies:

> One package + One rollout stage = At most one ACTIVE deployment at any time.

---

### 4.6 ISSUE 6 — Promote BETA to PROD Did Not Create a Deployment Record

**Reference:** CAR-2026-10-07-06

#### 4.6.1 Problem Statement

When an administrator promoted a package from BETA to PROD, the system performed three actions:

1. Changed `releaseType` from BETA to PROD.
2. Superseded lower PROD versions.
3. Wrote a `LATEST#PROD` pointer.

The system did not write a record to `digilux_ota_deployments`.
Devices received the update but no deployment record existed.
The Deployments page showed no record for the PRODUCTION rollout.
The administrator could not abort the rollout.
The duplicate deployment guard (Issue 5) did not detect the promoted package as an active deployment.
An administrator could create a second PRODUCTION deployment for the same package without warning.

#### 4.6.2 Root Cause

The promote path in `digilux_ota_package_activate` Lambda did not write to `digilux_ota_deployments`.

#### 4.6.3 Corrective Action

**Lambda:** `digilux_ota_package_activate`

The promote path now performs two additional actions:

1. Before promotion, the system checks for an existing ACTIVE PRODUCTION deployment.
   If one exists, the system returns HTTP 409.
   The administrator must abort the existing deployment first.

2. After promotion, the system writes a deployment record to `digilux_ota_deployments` with:
   - `status = ACTIVE`
   - `rolloutStage = PRODUCTION`
   - `targetGroup = DGX-Gateways`
   - `promotedFrom = BETA`

The response body now includes the deployment ID.

---

### 4.7 ISSUE 7 — Direct Package Activation Bypassed the Deployment Flow

**Reference:** CAR-2026-10-07-07

#### 4.7.1 Problem Statement

The system allowed an administrator to publish or withdraw a package directly using `{ activated: true/false }`.
This action made the package visible to devices with no deployment record.
The action bypassed rollout stage selection, target group selection, consent flow, and audit trail.
The duplicate deployment guard did not detect directly activated packages.
There was no mechanism to abort a directly activated package.

#### 4.7.2 Root Cause

The `digilux_ota_package_activate` Lambda processed the `activated` field and wrote or deleted the `LATEST#` pointer directly.
The UI showed Publish and Withdraw buttons on every ACTIVE package.

#### 4.7.3 Corrective Action

**Lambda:** `digilux_ota_package_activate`

The system returns HTTP 400 for any request that contains the `activated` field.
The error message directs the administrator to use the deployment flow.

**UI:** `PackagesPage.jsx`

The Publish and Withdraw buttons are removed.

All package visibility is now controlled through deployments only.
The administrator must use one of the following methods to make a package visible to devices:

| Method | Endpoint |
|--------|---------|
| BETA rollout | POST `/api/v1/ota/deployments` with `rolloutStage=BETA` |
| PRODUCTION rollout | POST `/api/v1/ota/deployments` with `rolloutStage=PRODUCTION` |
| Promote BETA to PROD | PATCH `/activate` with `{ promote: true }` |
| Custom targeted rollout | POST `/api/v1/ota/deployments` with `rolloutStage=CUSTOM` |

---

### 4.8 ISSUE 8 — Direct Package Recall Bypassed the Deployment Flow

**Reference:** CAR-2026-10-07-08

#### 4.8.1 Problem Statement

The system allowed an administrator to recall a package while an active deployment was running.
The recall marked the package as RECALLED and deleted the `LATEST#` pointer.
The deployment record remained ACTIVE in the database.
Devices that were in-progress continued to download the recalled artifact.
The deployment page showed the deployment as ACTIVE with no indication that the package was recalled.

#### 4.8.2 Root Cause

The recall path in `digilux_ota_package_activate` Lambda did not check for active deployments.
The UI showed a Recall button without requiring the administrator to abort the deployment first.

#### 4.8.3 Corrective Action

This issue is resolved by the corrective action described in Issue 1 (CAR-2026-10-07-01).

The recall operation is preserved but gated.
The administrator must abort the active deployment before the system allows a recall.

The distinction between abort and recall is:

| Operation | Target | Effect |
|-----------|--------|--------|
| Abort deployment | The rollout campaign | Stops this specific rollout. Package remains ACTIVE. Future deployments of this version are allowed. |
| Recall package | The artifact | Permanently blacklists this version. Future deployments of this version are blocked. |

---

### 4.9 ISSUE 9 — Package Delete Left Encrypted Artifacts in S3

**Reference:** CAR-2026-10-07-09

#### 4.9.1 Problem Statement

When an administrator deleted a package, the system only deleted the original raw upload file (`s3Key`).
The encrypted artifact (`encS3Key`) and the signature file (`sigS3Key`) remained in S3.
These orphaned files consumed storage indefinitely.
The system state was inconsistent — the DynamoDB record was deleted but the S3 artifacts remained.

#### 4.9.2 Root Cause

The `_delete_package` function in `digilux_ota_package_activate` Lambda only called `s3.delete_object` for `s3Key`.
The function did not process `encS3Key` or `sigS3Key`.

#### 4.9.3 Corrective Action

**Lambda:** `digilux_ota_package_activate`

The `_delete_package` function now deletes all three S3 objects:

| Field | Description |
|-------|-------------|
| `s3Key` | Original raw upload (safety net — may already be deleted by `artifact_processor`) |
| `encS3Key` | AES-256-GCM encrypted artifact |
| `sigS3Key` | ECDSA signature file |

Each key is deleted independently.
A failure to delete one key does not stop deletion of the others.
The function logs a warning for any key that cannot be deleted.

---

### 4.10 ISSUE 10 — PRODUCTION Deployments Targeted Wrong IoT Thing Group

**Reference:** CAR-2026-10-07-10

#### 4.10.1 Problem Statement

PRODUCTION deployments targeted the IoT Thing Group `DGX-Production`.
`DGX-Production` is a parent group.
It contains sub-groups but no devices directly.
AWS IoT Core delivers a job to Things that are direct members of the targeted group.
AWS IoT Core does not deliver the job to Things in child groups by default.
All PRODUCTION deployments delivered jobs to zero devices.

#### 4.10.2 Root Cause

The `PRODUCTION_GROUP` environment variable in `digilux_ota_job_create` Lambda defaulted to `"DGX-Production"`.
The deployed environment variable was also set to `"DGX-Production"`.

The IoT Thing Group hierarchy is:

```
DGX-Production  (parent — no direct device members)
├── DGX-Gateways    ← devices are members of this group
└── DGX-Controllers
```

#### 4.10.3 Corrective Action

**Lambda:** `digilux_ota_job_create`

The code default is changed to `"DGX-Gateways"`:

```python
PRODUCTION_GROUP = os.environ.get("PRODUCTION_GROUP", "DGX-Gateways")
```

The environment variable on the deployed Lambda is set to `"DGX-Gateways"`.

The same change is applied to `digilux_ota_package_activate` Lambda for the auto-created deployment on promote (Issue 6).

---

## 5. TEST EVIDENCE

All corrective actions were verified by the end-to-end test suite.

**Test script:** `infrastructure/e2e_test.sh`
**Result:** 138 passed / 0 failed / 7 warnings
**Date:** 2026-10-07

The 7 warnings are known non-blocking conditions:
- T11 and T12 skipped — require a separate BETA deployment after T10 aborts the test deployment
- T21.14 skipped — decline path requires a second deployment
- S3 bucket event notification count mismatch — pre-existing, unrelated to these fixes
- CloudWatch audit log timing — eventual consistency, not a system failure

---

## 6. AFFECTED COMPONENTS

| Component | Repository | Branch |
|-----------|-----------|--------|
| `digilux_ota_package_activate` | `aws-ota-system` | `mahesh_fix_for_nitin_suggestion` |
| `digilux_ota_job_create` | `aws-ota-system` | `mahesh_fix_for_nitin_suggestion` |
| `digilux_ota_artifact_processor` | `aws-ota-system` | `mahesh_fix_for_nitin_suggestion` |
| `e2e_test.sh` | `aws-ota-system` | `mahesh_fix_for_nitin_suggestion` |
| `PackagesPage.jsx` | `digilux-ota-admin-ui` | `master` |
| `DeploymentDetailPage.jsx` | `digilux-ota-admin-ui` | `master` |
| `UploadPage.jsx` | `digilux-ota-admin-ui` | `master` |

---

## 7. REVISION HISTORY

| Version | Date | Author | Description |
|---------|------|--------|-------------|
| 1.0 | 2026-10-07 | Mahesh Maney | Initial release — 10 corrective actions |
