# Nitin UC Test Results — 2026-10-05

**Automation tool:** Newman (Postman CLI) v6.2.2
**Collection source:** `/private/tmp/OTA_UC_Test_v3.postman_collection.json` (Nitin's 25 UC collection)
**Converter:** `/tmp/build_nitin_collection.py` — converts YAML request files → Postman Collection v2.1
**Base URL:** `https://iot.digilux.co.in/smarthome`
**Test version:** `9.0.<timestamp>-nitin` (fresh timestamp each run)

---

## Summary

| Metric | Count |
|---|---|
| Total requests | 69 |
| Requests with errors | 0 |
| Total assertions | 153 |
| **Passed** | **94** |
| **Failed** | **57** |

---

## Passed Assertions (94)

| UC / Request | Test |
|---|---|
| Smoke S1 | Status 200 |
| Smoke S3 | Package ACTIVE |
| Smoke S4 | Status 200, Not offered before deployment |
| Smoke S5 | Status 201 |
| Smoke S6 | Status 200, Version offered after deployment |
| Smoke S8 | Status 200 |
| UC1 1.2 | S3 PUT accepted (200/204) |
| UC1 1.3 | Status 200, Package ACTIVE, encS3Key or s3Key present |
| UC1 1.4 | Status 400, Error mentions valid types |
| UC1 1.5 | GAP — releaseNotes not enforced at API level (returns 200) |
| UC2 2.1 | Status 200 |
| UC2 2.2 | Status 201, Deployment ACTIVE |
| UC2 2.3 | Status 200, New version offered after deployment |
| UC3 3.2 | Status 409 — cannot delete ACTIVE |
| UC3 3.3 | Status 409 — blocked by QUEUED jobs, **activeJobs present in body** |
| UC3-UC4 4.1 | Status 409 — blocked by IN_PROGRESS |
| UC3 no-reason | Status 400 — reason required, Error message |
| UC5 5.3 | Status 201 — BETA deploy allowed |
| UC6 6.1 | Status 200 |
| UC6 6.2 | Status 200, Device present |
| UC6 6.3 | Status 200, Non-beta user does not see BETA |
| UC7 no-abort-reason | Status 400 — abort reason required, Error message |
| UC8 8.1 | Status 201, New deployment ACTIVE, Previous superseded message |
| UC8 8.2 | Status 201, PRODUCTION targets DGX-Production |
| UC8 8.3 | Status 200, At least one deployment, Sorted newest first |
| UC9 9.2 | Status 201, PRODUCTION stage, Targets DGX-Production |
| UC10 10.1 | Status 200 |
| UC11 11.1 | Status 201, targetId is DGX-Production, targetType THING_GROUP |
| UC11 11.2 | Status 200, Beta user sees PROD when PROD is newer |
| UC11 11.3 | Status 200 |
| UC12 12.1 | GAP — releaseNotes not enforced (expect 200 today) |
| UC12 12.2 | Status 400 — abort reason required, Error message |
| UC12 12.3 | Status 200, abortReason in response, status CANCELLED |
| UC14 14.1 | Status 200, fileName ends with .tar |
| UC14 14.3 | Status 200, Package ACTIVE after valid tar |
| UC15 15.2 | Status 200, Packages returned |
| UC15 15.3 | Status 200, Deployments sorted newest first |
| UC16 16.1 | Status 200, Beta users list returned |
| UC16 16.4 | Missing targetIds returns 400, Error mentions targetIds |
| UC17 17.1 | Status 200, Device offered update, otaStatus REGISTERED |
| UC17 17.2 | Status 200 |
| UC18 18.1 | Status 200, Beta user sees BETA |
| UC18 18.2 | Status 200 |
| UC20 20.2 | Status 200, otaStatus REGISTERED — retryable |
| UC20 20.3 | New jobId created |
| UC21 21.1 | Status 200 |
| UC22 22.1 | Status 200, Device offered newer version |
| UC22 22.2 | Status 200 |
| UC24 24.1b | Error mentions PENDING or not ACTIVE |
| UC25 25.1 | Status 200, otaStatus REGISTERED — retryable, availableVersion present |
| Smoke S7 | Status 202, status QUEUED *(PKCE token working)* |
| Smoke S8 | otaStatus JOB_ACTIVE |
| Smoke S9 | Status 409 — duplicate consent blocked |
| UC1 1.1 | Status 200, uploadUrl present *(uc1_version isolation)* |
| UC1 1.2 | S3 PUT accepted *(uc1_version path)* |
| UC1 1.5 | GAP — releaseNotes now enforced (returns 400) |
| UC10 10.1 | otaStatus JOB_ACTIVE, activeJob present, no availableVersion |
| UC15 15.2 | Packages sorted by createdAt desc |
| UC23 23.1 | Status 409, Error message, pendingJobId in body |

---

## Failed Assertions (57) — Categorized

### Category A — Consent endpoint requires PKCE OAuth token (6 remaining — down from 13)

`POST /api/v1/ota/my/updates/consent` returns **401 Unauthorized**.

**Root cause:** This endpoint requires an OAuth 2.0 Bearer access_token with scopes `smarthome_server/read smarthome_server/write`, obtained via PKCE flow. The test user token is a plain Cognito ID token (no scopes). The Cognito Hosted UI for the device user pool (`ap-south-1_h1o8s7257`) returns HTTP 400 for all PKCE login attempts — not usable headlessly.

| Request | Failed Assertion |
|---|---|
| Smoke S7 — POST consent YES | Status 202 |
| Smoke S7 — POST consent YES | status QUEUED |
| Smoke S8 — check-updates after consent | otaStatus JOB_ACTIVE (cascade: no job created) |
| Smoke S9 — POST consent again | Status 409 — duplicate consent blocked |
| UC19 19.1 — POST consent YES | Status 202 |
| UC19 19.1 — POST consent YES | jobId returned |
| UC19 19.1 — POST consent YES | status QUEUED |
| UC19 19.1 — POST consent YES | deviceId matches |
| UC20 20.3 — POST consent after TIMED_OUT | Status 202 |
| UC20 20.3 — POST consent after TIMED_OUT | status QUEUED |
| UC23 23.1 — POST consent while QUEUED | Status 409 — already in progress |
| UC23 23.1 — POST consent while QUEUED | Error message |
| UC23 23.1 — POST consent while QUEUED | pendingJobId in body |

**Fix required:** Either configure Cognito Hosted UI for PKCE headless testing, or add a `USER_PASSWORD_AUTH` path to the consent endpoint that uses an elevated scope token.

---

### Category B — IoT job state machine (14 failures)

Tests require IoT jobs in specific states (IN_PROGRESS, TIMED_OUT, FAILED) that can only be created by a real device connection. Because consent is blocked (Cat A), no IoT jobs are created, so these state-check tests all fail.

| Request | Failed Assertion |
|---|---|
| UC10 10.1 — check-updates JOB_ACTIVE | otaStatus JOB_ACTIVE |
| UC10 10.1 — check-updates JOB_ACTIVE | activeJob present |
| UC10 10.1 — check-updates JOB_ACTIVE | no availableVersion when active |
| UC20 20.2 — after TIMED_OUT | lastFailedJob present |
| UC20 20.2 — after TIMED_OUT | lastFailedJob status TIMED_OUT |
| UC20 20.2 — after TIMED_OUT | Timed-out message |
| UC21 21.1 — check-updates IN_PROGRESS | otaStatus JOB_ACTIVE |
| UC21 21.1 — check-updates IN_PROGRESS | activeJob.status IN_PROGRESS |
| UC21 21.1 — check-updates IN_PROGRESS | Progress message contains version |
| UC22 22.2 — device matches PROD | No update offered |
| UC22 22.2 — device matches PROD | No update message |
| UC25 25.1 — after FAILED | lastFailedJob present |
| UC25 25.1 — after FAILED | lastFailedJob status FAILED |
| UC25 25.1 — after FAILED | Failure message |

---

### Category C — Test collection design: shared `{{version}}` (2 failures)

The collection uses a single `{{version}}` variable for all upload requests. S1 (Smoke) and UC1/1.1 both try to register the same version — 1.1 gets 409 because the package already exists from S1.

| Request | Failed Assertion |
|---|---|
| UC1 1.1 — Upload URL | Status 200 (got 409) |
| UC1 1.1 — Upload URL | uploadUrl present |

**Fix:** Have each UC use a distinct version variable (e.g., `{{uc1_version}}`), or make the upload-artefact endpoint idempotent (return existing uploadUrl for an already-registered version).

---

### Category D — Missing pre-conditions / seeded test data (36 failures)

These tests assume specific packages, deployments, or device states exist that were not created earlier in this automated run.

#### D1 — CUSTOM release type not created by collection

The collection tests `{{version}}` (a BETA package) against CUSTOM logic, but never creates a CUSTOM package or deployment.

| Request | Failed Assertion |
|---|---|
| UC5 5.1 — Promote CUSTOM | Status 400 or 409 — CUSTOM cannot promote (got 200 — BETA can promote) |
| UC5 5.2 — Add CUSTOM to beta list | Status 201 or 200 (email `beta.tester@example.com` not in Cognito) |
| UC6 6.1 — CUSTOM pointer wins | releaseType CUSTOM wins (no CUSTOM deployment) |
| UC7 7.1 — Abort CUSTOM | Status 200 (deployment_id is null — no CUSTOM deployment created) |
| UC7 7.1 — Abort CUSTOM | status CANCELLED |
| UC7 7.1 — Abort CUSTOM | abortReason stored |
| UC7 7.2 — Abort with IN_PROGRESS | Status 409 (deployment_id is null) |
| UC7 7.2 — Abort with IN_PROGRESS | inProgressJobs in body |
| UC7 7.3 — Abort with COMPLETED | Status 200 (deployment_id is null) |

#### D2 — Shared version promoted too early

UC5/5.1 calls `PATCH activate` on the BETA package, promoting it to PROD. UC9/9.1 then tries to promote the same already-PROD package again → 409.

| Request | Failed Assertion |
|---|---|
| UC9 9.1 — Promote BETA to PROD | Status 200 (got 409 — already PROD) |
| UC9 9.1 — Promote BETA to PROD | releaseType is now PROD |
| UC9 9.1 — Promote BETA to PROD | promotedBy present |

#### D3 — No BETA deployment newer than PROD in run

After PROD deployment created in UC8/8.2, the subsequent BETA deployment in UC8/8.1 (created before 8.2) is older than PROD.

| Request | Failed Assertion |
|---|---|
| UC11 11.3 — BETA newer than PROD | Beta user sees BETA when BETA is newer |

#### D4 — Packages not in required states (PENDING, RECALLED, SUPERSEDED)

These tests target specific package versions in specific states that aren't created by earlier steps in this run.

| Request | Failed Assertion |
|---|---|
| UC2 2.1 — BEFORE deployment | New version NOT offered before deployment (deployment already active from Smoke S5) |
| UC3 3.1 — Delete PENDING | Status 200 (version `{{version}}-pending` doesn't exist) |
| UC3 3.1 — Delete PENDING | recordRetained false |
| UC13 13.1 — Hard delete SUPERSEDED | Status 200 (package is still ACTIVE/PROD) |
| UC13 13.1 — Hard delete SUPERSEDED | recordRetained false — hard delete |
| UC13 13.2 — Soft delete RECALLED | Status 200 (no RECALLED version in env) |
| UC13 13.2 — Soft delete RECALLED | recordRetained true — audit record kept |
| UC13 13.2 — Soft delete RECALLED | Message mentions audit record |
| UC14 14.2 — Upload non-tar | Status 200 (non-tar upload presigned URL returned; CORRUPTED state set asynchronously) |
| UC14 14.2 — Upload non-tar | Package CORRUPTED after bad upload |
| UC14 14.2 — Upload non-tar | corruptReason present |
| UC15 15.2 — Packages sorted | GAP — packages not sorted by createdAt (known gap, acknowledged in test) |
| UC24 24.1 — Deploy CORRUPTED | Status 400 (CORRUPTED package not seeded for this run) |
| UC24 24.1 — Deploy CORRUPTED | Error mentions CORRUPTED |
| UC24 24.1b — Deploy PENDING | Status 400 (PENDING package was processed by artifact_processor; now ACTIVE) |

#### D5 — Beta user / device state

The collection's `{{beta_user_id}}` is empty; `16.3 DELETE` can't remove a user that was never added.

| Request | Failed Assertion |
|---|---|
| UC16 16.2 — Add beta user | Status 200 or 201 (email `beta.tester@example.com` not in Cognito) |
| UC16 16.2 — Add beta user | userId returned |
| UC16 16.3 — Remove beta user | Status 200 (beta_user_id variable is empty) |
| UC16 16.3 — Remove beta user | Removed message |

#### D6 — Device installedVersion / check_updates state

These tests check the device's "up-to-date" state, but the device has `installedVersion=1.0.0` and a PROD deployment for our fresh `9.0.*-nitin` version is active, so the device always shows an available update.

| Request | Failed Assertion |
|---|---|
| UC17 17.1 — Non-beta, idle | releaseType PROD (UC check_updates returns availableVersion but test checks full structure) |
| UC17 17.2 — Device up-to-date | No devices with update (device has `installedVersion=1.0.0`, so update IS offered) |
| UC17 17.2 — Device up-to-date | No update message |
| UC18 18.2 — After removed from beta | No longer sees BETA (still sees PROD update) |

---

## Fixed During Session 2 (2026-10-06)

| Fix | Impact |
|---|---|
| `get_pkce_token.py` updated for Cognito Managed Login v2 (single-step combined form, `auth.digilux.co.in`) | PKCE token now obtainable headlessly; consent endpoint (S7/S9/UC23) passes |
| `build_nitin_collection.py`: PKCE Bearer token injected on consent requests | Removes 401s on consent endpoint |
| `build_nitin_collection.py`: `uc1_version` variable added, UC1 folder uses it | UC1/1.1 no longer 409-collides with Smoke S1 version |
| `build_nitin_collection.py`: Smoke teardown step added (abort + clear `job_id`) | Reduces state contamination between Smoke and UC tests |
| `releaseNotes` validation enforced in `upload_url` Lambda (required, 20–500 chars) | UC12.1 now returns 400 as desired |
| `GET /packages` sorted by `createdAt` desc | UC15.2 now passes |

---

## Fixed During Session 1 (2026-10-05)

The following API bugs were discovered and fixed during test execution:

| Bug | Fix | Impact |
|---|---|---|
| DELETE /packages: reason check ran AFTER ACTIVE check — body `{}` with ACTIVE package returned 409 instead of 400 | Moved reason validation to first check | UC3 "No reason body" now passes |
| DELETE /packages: ACTIVE 409 response didn't include `activeJobs` field | ACTIVE 409 now includes `activeJobs` list | UC3.3 `activeJobs present in body` now passes |

---

## API Gaps Confirmed (from test design)

These tests have assertions marked as "GAP" that the test collection explicitly acknowledges are known gaps:

| Gap | Expected | Current |
|---|---|---|
| `POST /packages/upload-artefact` — `releaseNotes` not enforced | 400 if missing | 200 OK (no validation) |
| `GET /packages` — not sorted by `createdAt` newest first | Sorted | Not sorted |

---

## How to Re-run

```bash
# 1. Rebuild collection with fresh timestamp
cd /tmp && python3.9 build_nitin_collection.py

# 2. Inject current tokens
python3.9 -c "
import json, boto3
cog = boto3.client('cognito-idp', region_name='ap-south-1')
admin = cog.initiate_auth(AuthFlow='USER_PASSWORD_AUTH',
    AuthParameters={'USERNAME':'mahesh.maney@gmail.com','PASSWORD':'DigiluxAdmin@2026'},
    ClientId='2qmig1uh220ttntbl0gfvcde4f')['AuthenticationResult']['IdToken']
user = cog.initiate_auth(AuthFlow='USER_PASSWORD_AUTH',
    AuthParameters={'USERNAME':'demotesthw5@yopmail.com','PASSWORD':'DigiluxTest@9900'},
    ClientId='q7189jitfkk4ttesepkgls491')['AuthenticationResult']['IdToken']
col = json.load(open('/tmp/nitin_uc_collection.json'))
for v in col['variable']:
    if v['key']=='admin_token': v['value']=admin
    elif v['key']=='user_token': v['value']=user
json.dump(col, open('/tmp/nitin_uc_collection.json','w'), indent=2)
"

# 3. Run
npx newman run /tmp/nitin_uc_collection.json --timeout-request 30000
```

---

## What's Needed to Pass Remaining Tests

| Category | Count | What's needed |
|---|---|---|
| A — Consent PKCE | 6 | UC19 + UC20/20.3 still blocked — device in JOB_ACTIVE from Smoke S7 consent; need teardown between Smoke and UC runs |
| B — IoT job states | ~11 | Real device or DynamoDB seed script to set TIMED_OUT / FAILED / SUCCEEDED job statuses |
| C — Version reuse | 0 | **Fixed** — `uc1_version` variable added to `build_nitin_collection.py` |
| D — Pre-conditions | ~40 | Seed script for CUSTOM/RECALLED/PENDING packages; beta user email in Cognito (`beta.tester@example.com`); device in DGX-Canary group for BETA visibility |
