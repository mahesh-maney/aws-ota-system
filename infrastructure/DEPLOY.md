# OTA Infrastructure — Zero-Touch Deployment

Single command deploys the entire OTA backend: S3, DynamoDB, IAM roles, Lambdas, IoT rules, API Gateway routes, CloudWatch, and the admin UI.

---

## Prerequisites

| Tool | Version |
|------|---------|
| AWS CLI | v2+ configured with deploy permissions |
| Python 3 | 3.8+ |
| pip | any |
| OpenSSL | any |
| zip | any |
| Node.js + npm | v18+ (admin UI only) |

---

## 1 — Configure

```bash
cd infrastructure/
cp deploy.config.template deploy.config
```

Open `deploy.config` and fill in the **REQUIRED** fields:

| Variable | Where to find it |
|----------|-----------------|
| `PREFIX` | Choose a short lowercase name, e.g. `honeywell`. All AWS resources are named `PREFIX-ota-*` / `PREFIX_ota_*`. |
| `REGION` | AWS region, e.g. `ap-south-1` |
| `API_GATEWAY_ID` | API Gateway console → your REST API → top of page (e.g. `ds6nxf8ac5`) |
| `API_GATEWAY_STAGE` | Stage name, e.g. `smarthome` |
| `ADMIN_COGNITO_AUTHORIZER_ID` | API Gateway → Authorizers → admin authorizer ID |
| `USER_COGNITO_AUTHORIZER_ID` | API Gateway → Authorizers → user/device authorizer ID |
| `USER_COGNITO_POOL_ID` | Cognito → User Pools → device pool ID (e.g. `ap-south-1_XXXXXXX`) |
| `DEVICE_DATA_TABLE` | Your existing device DynamoDB table name |
| `ALERT_EMAIL` | Email for CloudWatch alarm notifications |
| `SES_SENDER_EMAIL` | Verified SES sender address for consent emails |

**Admin UI** (optional — leave `ADMIN_UI_BUCKET` blank to skip):

| Variable | Description |
|----------|-------------|
| `ADMIN_UI_BUCKET` | S3 bucket name for the admin web interface |
| `ADMIN_UI_COGNITO_CLIENT` | Admin Cognito App Client ID |
| `LOGO_FILE` | Path to logo PNG/SVG (leave blank for default) |
| `BRAND_NAME` | Name shown in the UI navbar |

All other variables have sensible defaults and do not need to be changed.

---

## 2 — Preflight (optional but recommended)

Validates every config value and AWS resource before making any changes:

```bash
./preflight.sh
```

Exits 0 if all checks pass. Fix any `FAIL` items before deploying.

---

## 3 — Deploy

```bash
./deploy.sh
```

Runs all phases in order. A timestamped log is written to `/tmp/PREFIX_ota_deploy_YYYYMMDD_HHMMSS.log`.

### Flags

| Flag | Effect |
|------|--------|
| `--phase 04` | Run only phase `04_dynamodb.sh` |
| `--from 07` | Start from phase `07_deploy_lambdas.sh` onwards |
| `--ui-only` | Deploy only the admin UI (skips all infrastructure phases) |
| `--config path/to.config` | Use a different config file |

---

## What gets deployed

| Phase | Resource |
|-------|----------|
| 01 | S3 artifact bucket with versioning + lifecycle |
| 02 | Secrets Manager signing key (ECDSA key pair) |
| 03 | IoT Thing Group hierarchy (ROOT → PRODUCTION → leaf groups) |
| 04 | DynamoDB tables: packages, jobs, compatibility, deployments, consents, beta users |
| 05 | IAM roles for Lambda functions and IoT rules |
| 07 | 12 Lambda functions (artifact processor, check updates, job create, consent, etc.) |
| 08 | IoT rules: status ingestion + device registration |
| 09 | API Gateway routes + authorizers (admin + user/device endpoints) |
| 10 | CloudWatch log groups + dashboard + alarms |
| 11 | S3 event notifications → artifact processor Lambda |
| 12 | Production hardening: SNS alerts, DLQs, Lambda concurrency limits |
| UI | Admin web interface build + S3 sync |

All phases are **idempotent** — safe to re-run against a partially deployed account.

---

## Post-deploy steps

1. Confirm the SNS subscription email in your `ALERT_EMAIL` inbox.
2. Copy the signing public key printed at the end of deploy to each controller at `/etc/digilux/ota-signing.pub`.
3. Deploy the OTA agent to controllers.
4. Open the admin UI and upload a test package.

---

## Lambda Versioning

Every Lambda function is versioned on every deploy. Understanding the model:

| Concept | What it is |
|---------|------------|
| `$LATEST` | Mutable working copy — updated on every `update-function-code` |
| **Version** (1, 2, 3…) | Immutable snapshot of `$LATEST` at the moment of publish — code + env vars frozen |
| **Alias `prod`** | Named pointer that always points to the latest published version |

### How it works in `07_deploy_lambdas.sh`

After every Lambda update, the script automatically:
1. Calls `publish-version` — freezes the current code + config as an immutable version number, tagged with the git SHA and deploy timestamp.
2. Calls `update-alias prod` — moves the `prod` alias to point at the new version.

The version description contains the git SHA so you can trace any deployed version back to its exact commit:

```
git:a1b2c3d deployed:2026-09-29T10:30:00Z
```

### Checking what's deployed

```bash
# See the current prod alias for a function (shows version number + description)
aws lambda get-alias \
  --function-name digilux_ota_job_create \
  --name prod \
  --region ap-south-1

# List all published versions with their descriptions
aws lambda list-versions-by-function \
  --function-name digilux_ota_job_create \
  --region ap-south-1 \
  --query 'Versions[*].{Version:Version,Description:Description,Modified:LastModified}' \
  --output table
```

### Rolling back

If a deploy causes issues, point `prod` back to the previous version — no redeployment needed:

```bash
# Roll back to version 3
aws lambda update-alias \
  --function-name digilux_ota_job_create \
  --name prod \
  --function-version 3 \
  --region ap-south-1

# Roll back ALL core OTA Lambdas to version N in one shot
for FN in \
  digilux_ota_artifact_processor \
  digilux_ota_user_check_updates \
  digilux_ota_job_create \
  digilux_ota_user_consent \
  digilux_ota_package_register \
  digilux_ota_device_register \
  digilux_ota_status_handler \
  digilux_ota_user_update_status \
  digilux_ota_user_get_download_link; do
  aws lambda update-alias \
    --function-name "$FN" --name prod \
    --function-version <TARGET_VERSION> \
    --region ap-south-1
  echo "Rolled back $FN → v<TARGET_VERSION>"
done
```

### Initial baseline (version 1)

All 18 Lambdas were bootstrapped with version 1 on 2026-09-29 using description `"Initial versioning baseline — 2026-09-29"`. Every deploy from `07_deploy_lambdas.sh` onwards increments the version number automatically.
