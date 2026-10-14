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
| 7 | `ota_package_register` | Admin | Registers package in catalog |
| 8 | `ota_compatibility_check` | Admin | Per-device update check |
| 9 | `ota_beta_users` | Admin | Manages beta user list |
| 10 | `ota_job_create` | Admin | Create / list / abort deployments |
| 11 | `ota_job_sync` | Admin | EventBridge + IoT lifecycle sync |
| 12 | `ota_key_server` | Admin | Internal key wrap/unwrap service |
| 13 | `ota_status_handler` | Admin | IoT rule — syncs job status to DynamoDB |
| 14 | `ota_device_register` | Admin | IoT rule — registers device on agent startup |
| 15 | `ota_user_check_updates` | User | Available updates for user's devices |
| 16 | `ota_user_consent` | User | User accepts / declines OTA update |
| 17 | `ota_user_update_status` | User | Poll job execution status |
| 18 | `ota_user_get_download_link` | User | Presigned S3 download URL |
| — | `ota_dev_simulate_job` | Excluded | Dev/test only — not deployed to production |

---

## Target Environment Variables & Selective Lambda Mapping

The script patches **only** these 5 variables. All other existing env vars are preserved untouched.

| Env Var | Value to Set | Applied to Lambda(s) | Source of Truth |
|---------|-------------|----------------------|-----------------|
| `COGNITO_POOL_ID` | `us-east-1_DuegVptxS` | `ota_beta_users`, `ota_package_register` | Confirmed in source — `os.environ["COGNITO_POOL_ID"]` |
| `COGNITO_USER_POOL_ID` | `us-east-1_DuegVptxS` | ALL 19 Lambdas | Set by `07_deploy_lambdas.sh` to all — var not directly found in source (may be consumed at runtime) |
| `SES_SENDER` | `noreply@nucleusqa.honeywell.com` | ALL 19 Lambdas | Set by `07_deploy_lambdas.sh` to all — var not directly found in source (may be consumed at runtime) |
| `PRODUCTION_GROUP` | `HON-PROD` | `ota_job_create`, `ota_package_activate`, `ota_device_register` | Confirmed in source — `os.environ.get("PRODUCTION_GROUP", ...)` |
| `GATEWAYS_GROUP` | `HON-ZigbeeGateway` | `ota_device_register` | Confirmed in source — `os.environ.get("GATEWAYS_GROUP", ...)` |

> **Note on COGNITO_USER_POOL_ID and SES_SENDER:** These are not found by direct
> source code grep (grep searches plain `.py` files). However `07_deploy_lambdas.sh`
> explicitly sets them on every Lambda in the shared env JSON. To stay consistent
> with the deploy script and avoid runtime surprises, the patch script applies them
> to ALL Lambdas.

---

## What the Script Does

For each Lambda in the target list:
1. **Reads** the current environment variables from AWS.
2. **Compares** each of the 5 target keys against the required value.
3. **Reports** status: `[OK]` (correct), `[FIX]` (wrong value), `[ADD]` (key missing).
4. **Merges** only the changed keys into the existing env — all other vars preserved.
5. **Calls** `update-function-configuration` only if at least one key changed.
6. **Waits** for the update to complete before moving to the next Lambda.

---

## Patch Script

```python
# See infrastructure/patch_lambda_env.py
python3 infrastructure/patch_lambda_env.py
```

---
