# OTA System Redesign — Summary Document

> Last updated: 2026-09-28
> Status: Architecture confirmed, pending implementation approval

---

## The Problem

The current system collapses three separate concepts into one status on `digilux_ota_jobs`:

- `AWAITING_CONSENT` is used as a **deployment-level** status
- But consent is a **per-device, per-user** decision that varies independently
- A deployment to 10 devices can have 3 accepted, 4 pending, 3 declined — calling the whole campaign `AWAITING_CONSENT` is meaningless
- For PRODUCTION deployments targeting a `THING_GROUP`, you cannot pre-create consent rows for every device at deploy time — you don't know who is in the group
- This is the root cause of `check_updates` returning empty results and broken flows

---

## The Core Principle

```
DEPLOYMENT = "this firmware version is being offered to this audience"   → campaign level
CONSENT    = "this specific device/user agreed to install it"            → per device level
IOT JOB    = "what is actually happening on the device right now"        → execution level
```

**These three must never be collapsed into one status.**

---

## The Four Layers

```
OTA PACKAGE
│  firmware artifact, SHA256, encryption keys, ECDSA signature
│  unchanged — artifact layer stays as-is
│
▼
OTA DEPLOYMENT  (new table: digilux_ota_deployments)
│  campaign: which version, to which audience, in which stage
│  statuses: ACTIVE → COMPLETED / CANCELLED / FAILED
│
▼
USER CONSENT  (existing: digilux_ota_user_consents)
│  per device/user decision
│  ACCEPTED written when user taps YES
│  CANCELLED written by system when deployment superseded
│  DECLINED — NOT written to DB, logged to audit + CloudWatch only
│
▼
AWS IoT JOB  (existing: digilux_ota_jobs)
   per device execution record
   QUEUED → IN_PROGRESS → SUCCEEDED / FAILED / TIMED_OUT / CANCELLED
```

---

## Architecture Decisions — Confirmed

### 1. Separate deploymentIds for every stage

Beta, Production and Custom **always have separate deploymentIds** — even for the same package version. No promotion or mutation of the same record across stages. Each rollout stage is an independent campaign.

### 2. DGX-Production Thing Group

Every device that gets registered **automatically joins `DGX-Production`** at registration time. Production deployments target this group — no explicit device list needed at deploy time. The group is self-maintaining.

### 3. BETA always uses explicit DEVICE_LIST

Beta users table identifies who is *eligible* for beta. Each beta deployment selects an **explicit subset** from that list — different deployments can target different subsets. No `DGX-Beta` Thing Group — that would remove per-deployment granularity.

```
Beta users: A B C D E F G H I J

Deployment 4.6 BETA → targets A, B, C
Deployment 4.7 BETA → targets A, D, F, H
Deployment 4.8 BETA → targets B only
```

### 4. One active deployment per package per stage

At any time:
- One `ACTIVE` BETA deployment for a given package
- One `ACTIVE` PRODUCTION deployment for a given package
- One `ACTIVE` CUSTOM deployment for a given package

BETA and PRODUCTION can coexist (different audiences). When a new version is deployed for the same stage, the previous one is superseded.

### 5. IN_PROGRESS jobs are never touched

Whatever is running on the device — **let it complete or time out naturally.** No cancellation, no interference. Interrupting a mid-install risks bricking the device. This rule has no exceptions.

### 6. Consent sticks to its original deploymentId

If a user accepted `deploymentId-4.7.0` and 4.7.1 is released, their consent record remains linked to `deploymentId-4.7.0`. The IoT Job for that consent must run to completion or time out before the device is offered 4.7.1. The new deployment does not cancel or override an existing accepted consent.

### 7. DECLINED is not written to the consent table

When a user taps NO:
- Nothing is written to `digilux_ota_user_consents`
- The decline is logged to CloudWatch structured logs
- The decline is written to the audit trail
- The user will see the update again on their next `check_updates` call — intentional

### 8. Consent states

| State | Written to DB | Who sets it | When |
|---|---|---|---|
| `ACCEPTED` | Yes | End user taps YES | IoT Job created immediately |
| `CANCELLED` | Yes | System | Deployment superseded |
| `DECLINED` | No | End user taps NO | Logged only, no DB record |

---

## Deployment Structure

### BETA deployment record
```json
{
  "deploymentId": "digilux-ota-HomeAssistantUtility-4-7-0-<timestamp>",
  "packageName":  "HomeAssistantUtility",
  "version":      "4.7.0",
  "rolloutStage": "BETA",
  "targetType":   "DEVICE_LIST",
  "targetIds":    ["device-uuid-1", "device-uuid-2", "device-uuid-3"],
  "targetGroup":  null,
  "status":       "ACTIVE",
  "createdAt":    1790072620000,
  "createdBy":    "admin@digilux.co.in",
  "counters": {
    "accepted":  0,
    "declined":  0,
    "succeeded": 0,
    "cancelled": 0
  }
}
```

### PRODUCTION deployment record
```json
{
  "deploymentId": "digilux-ota-HomeAssistantUtility-4-7-0-<timestamp>",
  "packageName":  "HomeAssistantUtility",
  "version":      "4.7.0",
  "rolloutStage": "PRODUCTION",
  "targetType":   "THING_GROUP",
  "targetIds":    null,
  "targetGroup":  "DGX-Production",
  "status":       "ACTIVE",
  "createdAt":    1790100500000,
  "createdBy":    "admin@digilux.co.in",
  "counters": {
    "accepted":  0,
    "declined":  0,
    "succeeded": 0,
    "cancelled": 0
  }
}
```

### When superseded
```json
{
  "status":          "CANCELLED",
  "cancelledReason": "SUPERSEDED_BY_4.7.1",
  "supersededBy":    "digilux-ota-HomeAssistantUtility-4-7-1-<timestamp>",
  "cancelledAt":     1790200000000,
  "cancelledBy":     "admin@digilux.co.in"
}
```

### Deployment statuses

| Status | Meaning |
|---|---|
| `ACTIVE` | Firmware is currently being offered to the target audience |
| `COMPLETED` | All devices done or rollout window closed |
| `CANCELLED` | Admin stopped or a newer version superseded this campaign |
| `FAILED` | Operational failure |

No `AWAITING_CONSENT`, no `QUEUED`, no `IN_PROGRESS` at deployment level.

---

## check_updates Logic (new)

```
Device calls check_updates
      │
      ├── 1. Does device have ACCEPTED consent with QUEUED or IN_PROGRESS job?
      │         → honour that job, do not offer new version yet
      │
      ├── 2. Does device have ACCEPTED consent but job TIMED_OUT or FAILED?
      │         → offer retry (new IoT Job, same consent, no new consent needed)
      │           OR offer newer active deployment version if one exists
      │
      ├── 3. No active consent/job — find applicable ACTIVE deployment:
      │         → Is device in a BETA deployment targetIds? → use BETA (priority over PROD)
      │         → Else is device in DGX-Production? → use PROD
      │         → No applicable deployment → no update
      │
      ├── 4. installedVersion == deploymentVersion → no update, 200 success
      ├── 5. installedVersion >  deploymentVersion → no update, never downgrade
      └── 6. installedVersion <  deploymentVersion → offer update, new consent required
```

**BETA always takes priority** — a device targeted by an active BETA deployment is never accidentally offered the PROD version.

**Consent creation for PRODUCTION is lazy** — consent rows are not pre-created at deploy time. A PROD consent row is created when the device first calls `check_updates` and the user accepts.

---

## Use Case Flows

### Timeout — user accepted but device was offline

```
User taps YES → consent: ACCEPTED → IoT Job: QUEUED
Device offline → AWS IoT fires TIMED_OUT → job: TIMED_OUT
status_handler clears pendingJobId on device record

Device calls check_updates:
  Deployment: ACTIVE
  Consent: ACCEPTED (original deploymentId)
  Job: TIMED_OUT
  installedVersion: still old
  → Response: update available + "previous attempt timed out, retry?"

User taps Retry → POST /consent { accepted: true }
  → system finds existing ACCEPTED consent + TIMED_OUT job
  → no new consent needed — user already agreed
  → new IoT Job created against same consent record
  → Job: QUEUED again
```

### Stale/buggy version — 4.7.0 marked buggy, 4.7.1 released

```
4.7.0 deployment → CANCELLED (supersededBy 4.7.1)
4.7.0 package    → marked STALE (never offered again)
4.7.1 deployment → ACTIVE

Devices already on 4.7.0:
  installedVersion 4.7.0 < 4.7.1 → offered 4.7.1 → new consent required

Devices with ACCEPTED consent for 4.7.0, job QUEUED:
  → attempt cancel_job (best effort, no guarantee)
  → if cancel lands: offered 4.7.1 → new consent
  → if cancel misses: job completes, device ends up on 4.7.0, then offered 4.7.1

Devices with ACCEPTED consent for 4.7.0, job IN_PROGRESS:
  → do nothing — let install complete
  → device ends up on 4.7.0
  → next check_updates: offered 4.7.1 → new consent required

Recovery in all cases: device calls check_updates after install, 4.7.1 is offered.
Controller OTA agent MUST call check_updates after every install.
```

### Beta user removed from beta list

| Scenario | Result |
|---|---|
| PROD version == installed version | No update — already current, 200 success |
| PROD version > installed version | Offer PROD version — new consent required |
| Installed version > PROD version (beta was ahead) | No update — never downgrade |

### Already on latest version (any path)

```
Active deployment version == device installedVersion
→ 200 success
→ "No new updates available"
Applies regardless of how device reached that version (beta, prod, rollback, manual)
```

### New version supersedes old — mixed consent state

```
4.7.0 BETA ACTIVE — 3 devices targeted

Device A: ACCEPTED 4.7.0, job IN_PROGRESS
Device B: ACCEPTED 4.7.0, job QUEUED
Device C: no consent yet

Admin publishes 4.7.1 BETA:
  4.7.0 BETA → CANCELLED
  4.7.1 BETA → ACTIVE (new deploymentId)

Device A: IN_PROGRESS → untouched, completes 4.7.0 install
          next check_updates → offered 4.7.1
Device B: QUEUED → attempt cancel (best effort)
          if cancelled → offered 4.7.1
          if not cancelled → completes 4.7.0, then offered 4.7.1
Device C: no active consent → check_updates offers 4.7.1 directly
```

---

## What Changes

| Component | Change |
|---|---|
| New table `digilux_ota_deployments` | Campaign records with `ACTIVE/COMPLETED/CANCELLED/FAILED` statuses |
| `job_create` Lambda | Creates deployment as `ACTIVE`; consent rows created for BETA device list at deploy time; lazy for PROD |
| `check_updates` Lambda | Queries deployment table; respects in-flight consent/jobs; BETA priority; lazy PROD consent creation; never downgrade |
| `user_consent` Lambda | Consent lookup via deploymentId from new table; retry path for TIMED_OUT jobs |
| `status_handler` Lambda | Updates deployment counters on each device status change |
| Device registration | Auto-adds device to `DGX-Production` Thing Group |
| Admin UI | Deployment statuses `ACTIVE/COMPLETED/CANCELLED/FAILED`; counters shown; no AWAITING_CONSENT |

---

## What Does NOT Change

| Component | Reason |
|---|---|
| `digilux_ota_packages` table | Artifact layer — untouched |
| `digilux_ota_user_consents` table schema | Consent states refined but table stays |
| `digilux_ota_jobs` table | Stays as IoT execution records |
| Beta users API / table / UI | Still used to identify eligible beta users |
| Upload flow | Completely untouched |
| ECDSA signing | Untouched |
| AES-256-GCM encryption | Untouched |
| Key server | Untouched |
| Artifact processor Lambda | Untouched |

---

## IoT Job Rules Summary

| Job state when deployment superseded | Action |
|---|---|
| `QUEUED` | Attempt `cancel_job` — best effort only, no guarantee |
| `IN_PROGRESS` | Never touch — let complete or time out |
| `TIMED_OUT` | Clear `pendingJobId` on device; offer retry or newer version |
| `FAILED` | Clear `pendingJobId` on device; offer retry or newer version |
| `SUCCEEDED` | Device is on new version; check if newer deployment exists |

---

## Controller Team Requirement

The device OTA agent **must call `check_updates` after every completed install.** This is the recovery mechanism for all superseded/stale version scenarios. Without this, a device that installs a buggy version will never receive the fix.

---

## Status

- Architecture: confirmed
- Ambiguities resolved:
  - Beta/Prod/Custom always separate deploymentIds ✓
  - IN_PROGRESS jobs never touched ✓
  - Consent sticks to original deploymentId when superseded ✓
  - DECLINED not written to DB, logged only ✓
- Implementation: not started — pending explicit go-ahead
