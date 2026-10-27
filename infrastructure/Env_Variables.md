# OTA Lambda — Environment Variables Reference & Patch Script

> **Purpose:** This file is a living reference + executable patch tool.
> Review the tables manually, then run `patch_lambda_env.py` to idempotently
> patch specific env vars across all relevant Lambda functions.

---

## Lambda Function Master List (19 deployed)

| # | Function Name | Role | Notes |
|---|--------------|------|-------|
| 1 | `ota_upload_url` | Admin | Initiates upload, lists packages |
| 2 | `ota_upload_complete` | Admin | Completes multipart upload |
| 3 | `ota_package_status` | Admin | GET/DELETE package |
| 4 | `ota_package_activate` | Admin | Publish / recall / promote / restore |
| 5 | `ota_artifact_processor` | Admin | S3-triggered — processes uploads |
| 6 | `ota_artifact_key` | Admin | AES key delivery (MQTT + HTTP) |
| 7 | `ota_beta_users` | Admin | Manages beta user list |
| 8 | `ota_job_create` | Admin | Create / list / abort deployments |
| 9 | `ota_job_sync` | Admin | EventBridge + IoT lifecycle sync |
| 10 | `ota_key_server` | Admin | Internal key wrap/unwrap service |
| 11 | `ota_status_handler` | Admin | IoT rule — syncs job status to DynamoDB |
| 12 | `ota_device_register` | Admin | IoT rule — registers device on agent startup |
| 13 | `ota_user_check_updates` | User | Available updates for user's devices |
| 14 | `ota_user_consent` | User | User accepts / declines OTA update |
| 15 | `ota_user_update_status` | User | Poll job execution status |
| — | `ota_dev_simulate_job` | Excluded | Dev/test only — not deployed to production |

---

## Target Environment Variables & Selective Lambda Mapping

The script patches **only** these 5 variables. All other existing env vars are preserved untouched.

| Env Var | Value to Set | Applied to Lambda(s) | Source Confirmed |
|---------|-------------|----------------------|-----------------|
| `COGNITO_POOL_ID` | `us-east-1_DuegVptxS` | `ota_beta_users` | Yes — `os.environ["COGNITO_POOL_ID"]` found in source |
| `COGNITO_USER_POOL_ID` | `us-east-1_DuegVptxS` | **None (excluded)** | No — not found in any Lambda source file |
| `SES_SENDER` | `noreply@nucleusqa.honeywell.com` | **None (excluded)** | No — not found in any Lambda source file |
| `PRODUCTION_GROUP` | `HON-PROD` | `ota_job_create`, `ota_package_activate`, `ota_device_register` | Yes — `os.environ.get("PRODUCTION_GROUP", ...)` found in source |
| `GATEWAYS_GROUP` | `HON-ZigbeeGateway` | `ota_device_register` | Yes — `os.environ.get("GATEWAYS_GROUP", ...)` found in source |

> **Note on COGNITO_USER_POOL_ID and SES_SENDER:** Source code grep across all 17
> Lambda `.py` files found no `os.environ` reference for either variable.

> `07_deploy_lambdas.sh` sets them on all Lambdas via the shared env JSON, but
> since no Lambda source is confirmed to read them, the patch script excludes them.
> If a Lambda is found to use either variable, add it to the respective list in
> `PATCH_MAP` inside `patch_lambda_env.py`.

---

## What the Script Does

For each of the 19 Lambdas:
1. **Reads** the current environment variables from AWS.
2. **For each of the 5 managed keys:**
   - Key belongs to this Lambda → `[ADD]` if missing, `[FIX]` if wrong value, `[OK]` if correct.
   - Key does NOT belong to this Lambda but EXISTS on it → `[REMOVE]` (cleanup stale var).
3. **All other existing env vars** (not in the 5 managed keys) → **preserved untouched**.
4. **Calls** `update-function-configuration` only if at least one key changed or removed.
5. **Waits** for the update to complete before moving to the next Lambda.

---

## Patch Script

```python
# See infrastructure/patch_lambda_env.py
python3 infrastructure/patch_lambda_env.py
```

---
