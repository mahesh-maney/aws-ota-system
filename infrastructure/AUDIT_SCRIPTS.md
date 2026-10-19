# OTA Audit Scripts

Three Python scripts to extract live AWS state for gap analysis.
Run these to compare what is **actually deployed** against what the deploy scripts are expected to create.

---

## Scripts

| Script | Extracts | Output files |
|---|---|---|
| `extract_ota_roles.py` | IAM roles + IoT Core policies | `ota_roles_<ts>.json` + `.md` |
| `extract_ota_iot_rules.py` | IoT Topic Rules + EventBridge rules | `ota_iot_rules_<ts>.json` + `.md` |
| `extract_ota_lambdas.py` | Lambda functions + env vars + aliases | `ota_lambdas_<ts>.json` + `.md` |

---

## Usage

```bash
# Digilux (ap-south-1)
python3.9 infrastructure/extract_ota_roles.py     --region ap-south-1 --output-dir ./audit
python3.9 infrastructure/extract_ota_iot_rules.py --region ap-south-1 --output-dir ./audit
python3.9 infrastructure/extract_ota_lambdas.py   --region ap-south-1 --output-dir ./audit

# Honeywell QA (us-east-1) — switch profile first
export AWS_PROFILE=honeywell-qa
python3.9 infrastructure/extract_ota_roles.py     --region us-east-1 --output-dir ./audit-honeywell
python3.9 infrastructure/extract_ota_iot_rules.py --region us-east-1 --output-dir ./audit-honeywell
python3.9 infrastructure/extract_ota_lambdas.py   --region us-east-1 --output-dir ./audit-honeywell
```

**Options (all three scripts):**

| Flag | Default | Description |
|---|---|---|
| `--region` | AWS config default | AWS region to scan |
| `--filter` | `ota` | Name filter (word-boundary match) |
| `--output-dir` | `.` (current dir) | Where to write output files |

**`extract_ota_lambdas.py` only:**

| Flag | Description |
|---|---|
| `--show-secrets` | Show sensitive env var values in Markdown (default: redacted) |

> Sensitive env var values (names containing KEY, SECRET, TOKEN, etc.) are always written in full to the JSON file. The Markdown redacts them unless `--show-secrets` is passed.

---

## What to expect

### `extract_ota_roles.py`
- Lists all IAM roles whose name contains `ota` as a whole word segment
- For each role: ARN, trust policy (who can assume it), inline policies, attached managed policies
- Also lists IoT Core policies attached to device certificates
- Compare against: `05_iam_roles.sh`, `03_iot_setup.sh`

### `extract_ota_iot_rules.py`
- Lists all IoT Topic Rules whose name contains `ota`
- For each rule: SQL query (topic + data extraction), Lambda/action target ARN, error action
- Also lists EventBridge scheduled rules (e.g. daily staleness check)
- The Markdown includes an **Expected Rules** reference table at the top for direct comparison
- Compare against: `08_iot_rules.sh`

### `extract_ota_lambdas.py`
- Lists all Lambda functions whose name contains `ota`
- For each function: runtime, handler, memory, timeout, last modified, IAM role
- **Full environment variable table** — this is the primary use case
- Aliases (e.g. `prod → v7`) so you can see which version is live
- The Markdown includes an **Expected Functions** reference table at the top
- Compare against: `07_deploy_lambdas.sh`, `06_lambdas/`

---

## Prerequisites

```bash
pip3.9 install boto3
aws configure   # or set AWS_PROFILE / AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
```
