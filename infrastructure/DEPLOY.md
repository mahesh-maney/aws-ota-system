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
