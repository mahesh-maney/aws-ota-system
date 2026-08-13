# OTA System — CDK Deployment Guide

**Version:** 1.0 | **Date:** 2026-08-12

This guide explains how to deploy the entire Digilux OTA system to any AWS account
using AWS CDK (Python). A single command stands up all infrastructure — storage,
IAM, Lambda, API Gateway, alarms — with zero manual steps.

---

## Overview

The CDK app lives in `cdk/` and synthesizes to 6 CloudFormation stacks:

| Stack | What it creates |
|-------|----------------|
| `{prefix}-ota-storage` | S3 artifact bucket + 4 DynamoDB tables (PITR enabled) |
| `{prefix}-ota-iam` | 2 Lambda execution roles + 1 IoT rule role |
| `{prefix}-ota-lambda` | 11 Lambda functions + CloudWatch log groups + IoT topic rules |
| `{prefix}-ota-events` | S3 event notification: upload → `artifact_processor` |
| `{prefix}-ota-api` | REST API Gateway + Cognito authorizer + all 11 endpoints |
| `{prefix}-ota-alarms` | SNS alert topic + 33 CloudWatch alarms (errors/throttles/duration) |

Stacks deploy in dependency order automatically. Each stack is idempotent — safe to
re-run after any change; CloudFormation computes and applies only the diff.

---

## Directory Structure

```
cdk/
├── app.py                    ← Entry point — instantiates all stacks
├── cdk.json                  ← CDK CLI config
├── requirements.txt          ← CDK Python dependencies (aws-cdk-lib, constructs)
├── deploy.sh                 ← Zero-touch deploy helper script
├── config/
│   ├── digilux.json          ← Digilux environment (reference)
│   └── honeywell-prod.json   ← Honeywell production (fill in before deploying)
└── stacks/
    ├── storage_stack.py      ← S3 + DynamoDB
    ├── iam_stack.py          ← IAM roles + policies
    ├── lambda_stack.py       ← Lambda functions + IoT rules
    ├── events_stack.py       ← S3 event triggers
    ├── api_stack.py          ← API Gateway + routes
    └── alarms_stack.py       ← CloudWatch alarms + SNS
```

---

## Prerequisites

### Tools (install once on the deployment machine)

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.11+ | `brew install python` / system package manager |
| Node.js | 18+ | `brew install node` |
| AWS CDK CLI | latest | `npm install -g aws-cdk` |
| AWS CLI | v2 | [aws.amazon.com/cli](https://aws.amazon.com/cli) |

### AWS Account Setup (one-time per environment)

Before the first deploy, perform these steps manually in the target AWS account:

**1. Create the OTA signing key in Secrets Manager**

This RSA private key signs firmware package manifests. Generate a 2048-bit RSA key:

```bash
openssl genrsa -out /tmp/ota-signing-key.pem 2048

aws secretsmanager create-secret \
  --name "{namePrefix}-ota-signing-key" \
  --secret-string file:///tmp/ota-signing-key.pem \
  --region {region}

rm /tmp/ota-signing-key.pem
```

**2. Create a CloudFront key pair and upload the private key**

```bash
# Create an RSA key pair for CloudFront signed URLs
openssl genrsa -out /tmp/cf-private.pem 2048
openssl rsa -pubout -in /tmp/cf-private.pem -out /tmp/cf-public.pem

# Upload to CloudFront via AWS Console:
#   CloudFront → Key management → Public keys → Add public key
# Note the Key Pair ID returned (format: K3XXXXXXXX) — add it to config.

# Upload private key to Secrets Manager
aws secretsmanager create-secret \
  --name "{namePrefix}-ota-cloudfront-key" \
  --secret-string file:///tmp/cf-private.pem \
  --region {region}

rm /tmp/cf-private.pem /tmp/cf-public.pem
```

**3. Create a CloudFront distribution**

- Origin: the OTA S3 bucket
- Restrict bucket access: Yes (Origin Access Control)
- Enable signed URLs / signed cookies
- Set the CloudFront domain and Key Pair ID in your config file

**4. Ensure `device_data` table exists**

The OTA system stores device state (installed versions, pending jobs, thing name) as
attributes on the existing `{namePrefix}_device_data` table, managed by the device
platform team. The CDK does not create this table — it only references it.

Confirm the table exists and has a `userId-index` GSI on the `userId` attribute.

**5. Ensure Cognito User Pool exists**

The OTA API uses an existing Cognito User Pool for authentication. Note the Pool ID
and add it to your config file.

---

## Configuration

Each environment has a JSON config file in `cdk/config/`. Copy the template and fill
in the values for your target environment:

```bash
cp cdk/config/honeywell-prod.json cdk/config/my-env.json
```

### Config file reference

```jsonc
{
  // AWS account ID and region for this deployment
  "account": "123456789012",
  "region": "ap-south-1",

  // Prefix applied to all resource names (e.g. "honeywell" → "honeywell_ota_packages")
  "namePrefix": "honeywell",

  // Existing Cognito User Pool ID (used by API Gateway authorizer)
  "cognitoUserPoolId": "ap-south-1_XXXXXXXXX",

  // Pre-existing device data table (managed by device platform team)
  "deviceDataTableName": "honeywell_device_data",

  // S3 bucket name for firmware artifacts (CDK creates this)
  "artifactBucketName": "honeywell-ota-artifacts",

  // Secrets Manager secret names (must exist before deploy — see Prerequisites)
  "signingSecretName": "honeywell-ota-signing-key",
  "cloudfrontKeySecretName": "honeywell-ota-cloudfront-key",

  // CloudFront distribution (must exist before deploy — see Prerequisites)
  "cloudfrontDomain": "XXXXX.cloudfront.net",
  "cloudfrontKeyPairId": "KXXXXXXXXXXX",

  // IoT Job timeout: how long a device has to complete an update (minutes)
  "iotJobTimeoutMinutes": 1440,

  // Canary group name in AWS IoT for staged rollouts
  "canaryGroup": "HW-Canary",
  "canaryMax": 5,

  // Pre-signed URL expiry tiers (by artifact size)
  "presignExpiryTiers": {
    "tier1MaxMb": 50,  "tier1Sec": 3600,    // ≤50MB  → 1 hour
    "tier2MaxMb": 200, "tier2Sec": 21600,   // ≤200MB → 6 hours
    "tier3MaxMb": 500, "tier3Sec": 86400,   // ≤500MB → 24 hours
    "tier4Sec": 172800                       // >500MB → 48 hours
  },

  // API Gateway stage name
  "apiStageName": "prod",

  // Email for CloudWatch alarm notifications (SNS subscription)
  "alertEmail": "ota-alerts@honeywell.com"
}
```

---

## Deploying

### Zero-touch deploy (recommended)

```bash
bash cdk/deploy.sh honeywell-prod
```

The script:
1. Checks CDK CLI and Python are installed
2. Installs CDK Python dependencies (`aws-cdk-lib`)
3. Runs `cdk bootstrap` (idempotent — safe to run again)
4. Synthesizes and validates all CloudFormation templates
5. Deploys all 6 stacks in dependency order
6. Saves stack outputs (API URL, table names, etc.) to `cdk/outputs-honeywell-prod.json`

**Typical deploy time:** 5–10 minutes for a fresh account; ~2 minutes for updates.

### Preview changes before deploying

```bash
bash cdk/deploy.sh honeywell-prod --diff
```

Shows exactly which resources will be created, modified, or deleted — before any
changes are made. Use this every time before deploying to production.

### Manual deploy (advanced)

```bash
cd cdk
pip install -r requirements.txt
cdk bootstrap aws://ACCOUNT_ID/REGION
cdk deploy --all -c env=honeywell-prod --require-approval never
```

---

## Deployed Endpoints

After deploy, the API base URL is in `cdk/outputs-{env}.json` under `ApiUrl`.

### Admin endpoints (Cognito token required, admin group)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/ota/packages` | List all firmware packages |
| `POST` | `/api/v1/ota/packages/upload-artefact` | Get pre-signed S3 upload URL |
| `POST` | `/api/v1/ota/deployments` | Create an OTA job |
| `GET` | `/api/v1/ota/deployments` | List all jobs |
| `GET` | `/api/v1/ota/deployments/{jobId}` | Job status + per-device progress |
| `POST` | `/api/v1/ota/deployments/{jobId}/abort` | Abort a job |
| `GET` | `/api/v1/controllers/{deviceId}/updates/available` | Check device compatibility |

### User endpoints (Cognito token required, any authenticated user)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/ota/device/available-updates` | List pending updates for device |
| `POST` | `/api/v1/ota/my/updates/consent` | Consent to update → creates IoT Job |
| `POST` | `/api/v1/ota/my/updates/download-link` | Get CloudFront signed download URL |
| `GET` | `/api/v1/ota/my/updates/{jobId}/status` | Track update progress |

---

## Updating After Code Changes

### Lambda code change

```bash
bash cdk/deploy.sh honeywell-prod
```

CDK detects which Lambda zip assets changed and updates only those functions.

### Config change (e.g. new CloudFront domain)

1. Edit `cdk/config/honeywell-prod.json`
2. Run `bash cdk/deploy.sh honeywell-prod`

CloudFormation updates only the affected resources (Lambda env vars in this case).

### New environment

```bash
cp cdk/config/honeywell-prod.json cdk/config/honeywell-staging.json
# Edit honeywell-staging.json with staging values
bash cdk/deploy.sh honeywell-staging
```

---

## Monitoring

All Lambda functions have alarms routing to the `{prefix}-ota-alerts` SNS topic:

- **Errors** — any error in a 60-second window
- **Throttles** — any throttle in a 60-second window
- **Duration p99** — if p99 latency exceeds 24s (80% of 30s timeout)

The alert email set in config receives a confirmation email on first deploy — click
**Confirm subscription** to activate notifications.

CloudWatch dashboards are not created by CDK but can be added independently using
the log groups `/aws/lambda/{functionName}`.

---

## Teardown

Stateful resources (S3 bucket, DynamoDB tables) use `RemovalPolicy.RETAIN` and will
**not** be deleted when a stack is destroyed. This is intentional — it prevents
accidental data loss.

```bash
# Remove all non-stateful resources (lambdas, API, alarms, IAM roles)
cd cdk
cdk destroy --all -c env=honeywell-prod

# To also delete data (IRREVERSIBLE):
# 1. Manually empty and delete the S3 bucket
# 2. Manually delete the DynamoDB tables
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ResourceNotFoundException` on Lambda invoke | `device_data` table missing | Ensure table exists in target account |
| `AccessDeniedException` on Secrets Manager | Secret not created | Run the prerequisite secret creation steps |
| CloudFront returns 403 | Key pair mismatch | Check `cloudfrontKeyPairId` matches the uploaded public key |
| Lambda `InvalidELF` error | Wrong platform binary | Ensure pip bundling uses `--platform manylinux2014_x86_64` |
| `cdk bootstrap` fails | Insufficient IAM permissions | Deploying user needs `AdministratorAccess` or equivalent |
| SNS no emails received | Subscription not confirmed | Check email inbox for AWS confirmation email |
