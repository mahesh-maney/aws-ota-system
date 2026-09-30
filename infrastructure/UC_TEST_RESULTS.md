# OTA System UC Test Results

**Executed:** 2026-09-30
**Environment:** Live dev (`iot.digilux.co.in`, `ap-south-1`)
**Tester:** Claude (automated via Lambda invoke + DynamoDB direct)

---

## Results Summary

| UC | Title | Steps | Result | Notes |
|----|-------|-------|--------|-------|
| UC1 | Upload valid package | 1.1 POST upload-url, 1.2 presign works | **PASS** | — |
| UC2 | Create deployment | 2.1 PROD deployment, 2.2 BETA deployment | **PASS** | — |
| UC3 | Delete package | 3.1 DELETE with reason | **PASS** | Requires `{"reason":"..."}` body |
| UC4 | Abort deployment | 4.1 ACTIVE → CANCELLED | **PASS** | — |
| UC5 | Package activate | 5.1 `activated=true` | **PASS** | — |
| UC6 | Beta user routing | 6.1 non-beta sees PROD, 6.2 beta sees newer | **PASS** | IAM fix applied (`GetItem` on `digilux_ota_beta_users`) |
| UC7 | Custom deployment | 7.1 CUSTOM pointer, 7.2–7.3 supersede/abort cleanup | **PASS** | — |
| UC8 | Three stages active | 8.1 PROD only, 8.2 PROD+BETA+CUSTOM simultaneously | **PASS** | — |
| UC9 | BETA→PROD promote | 9.1 PATCH `/activate` with `promote:true`, 9.2 POST PRODUCTION | **PASS** | Endpoint is `/activate`, not bare version |
| UC10 | Single in-progress | 10.1 QUEUED job → JOB_ACTIVE | **PASS** | — |
| UC11 | Production group | 11.1 targets DGX-Production, 11.2 non-beta PROD, 11.3 beta BETA | **PASS** | — |
| UC12 | Release notes + abort reason | 12.1 no notes (API), 12.2 no reason, 12.3 with reason | **PARTIAL** | 12.1 GAP (API allows no notes; UI-only enforcement). 12.2 PASS, 12.3 PASS |
| UC13 | Delete S3+DDB | 13.1 hard delete PENDING, 13.2 RECALLED soft delete | **PASS** | RECALLED → DDB retained as `DELETED`, S3 cleaned |
| UC14 | Always tar+manifest | 14.1 `.tar` naming, 14.2 bad archive CORRUPTED, 14.3 valid tar ACTIVE | **PASS** | — |
| UC15 | Admin UI date sort | 15.1 SKIP, 15.2 packages, 15.3 deployments | **PARTIAL** | 15.2 GAP (packages list not sorted by createdAt). 15.3 PASS |
| UC16 | Beta list from DDB | 16.1 list, 16.2 add, 16.3 verify, 16.4 remove | **PASS** | DELETE uses `{userId}` path param (not deviceId) |
| UC17 | Idle shows latest PROD | 17.1 idle non-beta → PROD, 17.2 up-to-date → no update | **PASS** | — |
| UC18 | Beta always BETA until removed | 18.1 beta user sees BETA, 18.2 remove → sees PROD | **PASS** | — |
| UC19 | Consent QUEUED THING job | 19.1 YES consent → IoT QUEUED + pendingJobId set | **PASS** | — |
| UC20 | Timeout and retry | 20.1 SKIP, 20.2 TIMED_OUT retryable, 20.3 FAILED retryable, 20.4 SKIP | **PASS** | Both TIMED_OUT and FAILED return `lastFailedJob` + offer new update |
| UC21 | Progress message | 21.1 IN_PROGRESS → JOB_ACTIVE + progress message | **PASS** | — |
| UC22 | Completed then newer | 22.1 after SUCCEEDED sees newer, 22.2 up-to-date no update | **PASS** | — |
| UC23 | Duplicate consent | 23.1 second consent → 409 | **PASS** | — |
| UC24 | Corrupt fail copy | 24.1 CORRUPTED/PENDING cannot be deployed | **PASS** | — |
| UC25 | Failed until check newer | 25.1 FAILED job + PROD available → REGISTERED + lastFailedJob | **PASS** | — |

---

## Gaps / Failures

### GAP-1: UC12.1 — Release notes not enforced at API level
- **Expected:** `POST /ota/upload-url` without `releaseNotes` → 400
- **Actual:** 200, upload succeeds (notes are empty)
- **Enforcement:** UI-only (React frontend validates 20–500 char requirement)
- **Recommendation:** Add backend validation in `digilux_ota_upload_url` Lambda

### GAP-2: UC15.2 — Packages list not sorted by `createdAt` descending
- **Expected:** `GET /ota/packages` returns packages newest-first
- **Actual:** Returns packages in DynamoDB partition scan order (unordered)
- **Root cause:** `packages` endpoint uses a Scan with no sort
- **Recommendation:** Add a GSI on `createdAt` or sort in Lambda before returning

---

## Bugs Fixed During Testing

| Bug | Description | Fix Applied |
|-----|-------------|-------------|
| B1 | `job_create` did not write `LATEST#PROD`/`LATEST#BETA` pointers | Added `_write_stage_pointer()` + `_delete_stage_pointer()` functions, wired into create/supersede/abort flows |
| B2 | `_is_beta_user()` always returned `False` | IAM role `digilux-ota-user-lambda-role` was missing `dynamodb:GetItem` on `digilux_ota_beta_users`; added `BetaUsersRead` statement |

---

## Infrastructure Change (IAM)

Added to `digilux-ota-user-lambda-role-policy`:
```json
{
  "Sid": "BetaUsersRead",
  "Effect": "Allow",
  "Action": ["dynamodb:GetItem"],
  "Resource": "arn:aws:dynamodb:ap-south-1:986906626244:table/digilux_ota_beta_users"
}
```

---

## Test Data Used

| Device | User | Package | Installed |
|--------|------|---------|-----------|
| `edb39bba-baf1-4700-968c-a42228e53aa0` | `41f35d4a` (demotesthw5@yopmail.com) | HomeAssistantUtility | 1.0.0 |
| `32538180-bb19-4fb3-a0f9-ca0fbd8e2661` | `81533dba` (saxena2905@gmail.com) | HomeAssistantUtility | 4.0.0 |

Active packages used: `1.0.2-uc1`, `4.7.1`, `23.3.3.`, `uc14-valid2`
Active PROD deployment: `23.3.3.` → `DGX-Production`
Active BETA deployment: `4.7.1` → `[edb39bba]`
