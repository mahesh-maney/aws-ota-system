#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy_provisioning_validation.sh
#
# Packages, (optionally) obfuscates, and deploys the provisioning_validation
# Lambda to any customer AWS account.
#
# This Lambda is the IoT Fleet Provisioning pre-hook.  It is separate from the
# 12 OTA Lambdas and is deployed independently.
#
# Usage:
#   bash infrastructure/deploy_provisioning_validation.sh
#   OBFUSCATE=true bash infrastructure/deploy_provisioning_validation.sh
#
# All required values are read from deploy.config (same file used by deploy.sh).
# Additional required keys specific to provisioning_validation:
#
#   PV_DEVICE_STATE_TABLE   — DynamoDB table holding per-device endpoint states
#   PV_DATABASE             — Athena database name (e.g. nucleus_data)
#   PV_TABLE                — Athena table name   (e.g. device_table)
#   PV_SECRET_KEY           — AES-GCM key for MAC encryption (16/24/32 chars)
#   PV_BUCKET_ATHENA_OUTPUT — S3 bucket name for Athena query output
#   PV_TABLE_DEVICE_DATA    — DynamoDB device-data table (e.g. digilux_device_data)
#   PV_THING_GROUP          — IoT thing group for provisioned devices
#
# Optional:
#   OBFUSCATE=true          — obfuscate Python source with PyArmor before deploy
#
# Exit code: 0 = success, 1 = failure
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "${DIR}/../platform_lambdas/provisioning_validation" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _RED='\033[0;31m'; _YELLOW='\033[1;33m'
_BLUE='\033[0;34m';  _BOLD='\033[1m';   _NC='\033[0m'
log_ok()   { echo -e "$(date '+%H:%M:%S') ${_GREEN}OK   ${_NC} $*"; }
log_info() { echo -e "$(date '+%H:%M:%S') ${_BLUE}INFO ${_NC} $*"; }
log_warn() { echo -e "$(date '+%H:%M:%S') ${_YELLOW}WARN ${_NC} $*" >&2; }
log_err()  { echo -e "$(date '+%H:%M:%S') ${_RED}ERROR${_NC} $*" >&2; }

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ ! -f "$CONFIG_FILE" ]]; then
  log_err "Config file not found: $CONFIG_FILE"
  exit 1
fi
set -a; source "$CONFIG_FILE"; set +a

# ── Validate required config keys ─────────────────────────────────────────────
MISSING=0
for VAR in REGION ACCOUNT_ID \
           PV_DEVICE_STATE_TABLE PV_DATABASE PV_TABLE \
           PV_SECRET_KEY PV_BUCKET_ATHENA_OUTPUT \
           PV_TABLE_DEVICE_DATA PV_THING_GROUP; do
  if [[ -z "${!VAR:-}" ]]; then
    log_err "Required config value not set: $VAR"
    (( MISSING++ )) || true
  fi
done
if [[ $MISSING -gt 0 ]]; then
  echo ""
  echo "  Add the missing keys to $CONFIG_FILE and re-run."
  echo "  See the header of this script for descriptions."
  exit 1
fi

FUNC_NAME="provisioning_validation"
ROLE_NAME="provisioning-validation-role"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
RUNTIME="python3.11"
OBFUSCATE="${OBFUSCATE:-false}"
ZIP_FILE="/tmp/${FUNC_NAME}.zip"
PKG_TMP="/tmp/${FUNC_NAME}_pkg"

echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD}  Deploy: provisioning_validation${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
log_info "Source   : $SRC_DIR"
log_info "Function : $FUNC_NAME"
log_info "Region   : $REGION"
log_info "Account  : $ACCOUNT_ID"
log_info "Obfuscate: $OBFUSCATE"
echo ""

# ── Verify AWS credentials ────────────────────────────────────────────────────
log_info "Verifying AWS credentials..."
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [[ "$CALLER_ACCOUNT" != "$ACCOUNT_ID" ]]; then
  log_err "Logged-in account ($CALLER_ACCOUNT) does not match ACCOUNT_ID ($ACCOUNT_ID)"
  exit 1
fi
log_ok "Credentials valid — account $CALLER_ACCOUNT"
echo ""

# ── PyArmor setup ─────────────────────────────────────────────────────────────
PYARMOR_CMD=""
if [[ "$OBFUSCATE" == "true" ]]; then
  log_info "Obfuscation enabled — setting up PyArmor under python3.11..."
  if ! command -v python3.11 &>/dev/null; then
    log_err "python3.11 not found — required for obfuscation."
    echo "  Install: brew install python@3.11  (macOS)"
    exit 1
  fi
  python3.11 -m pip install -q pyarmor
  PYARMOR_CMD="$(python3.11 -c 'import sys; print(sys.prefix)')/bin/pyarmor"
  if [[ ! -x "$PYARMOR_CMD" ]]; then
    log_err "pyarmor executable not found at $PYARMOR_CMD"
    exit 1
  fi
  log_ok "$($PYARMOR_CMD --version 2>&1 | head -1)  (target: linux.x86_64 cp311)"
  echo ""
fi

# ── IAM role ──────────────────────────────────────────────────────────────────
log_info "── IAM role: $ROLE_NAME"
if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null | grep -q '"RoleName"'; then
  log_info "  Role already exists."
else
  log_info "  Creating role..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' \
    --description "Execution role for provisioning_validation Lambda" > /dev/null
  log_ok "  Role created."
fi

log_info "  Attaching inline policy..."
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "provisioning-validation-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"Logs\",
        \"Effect\": \"Allow\",
        \"Action\": [\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],
        \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/provisioning_validation:*\"
      },
      {
        \"Sid\": \"IoTPublish\",
        \"Effect\": \"Allow\",
        \"Action\": \"iot:Publish\",
        \"Resource\": \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:topic/things/*/validation/result\"
      },
      {
        \"Sid\": \"IoTCert\",
        \"Effect\": \"Allow\",
        \"Action\": \"iot:UpdateCertificate\",
        \"Resource\": \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:cert/*\"
      },
      {
        \"Sid\": \"DynamoDB\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\",\"dynamodb:PutItem\",
          \"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\",\"dynamodb:Query\"
        ],
        \"Resource\": [
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PV_TABLE_DEVICE_DATA}\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PV_TABLE_DEVICE_DATA}/index/*\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PV_DEVICE_STATE_TABLE}\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PV_DEVICE_STATE_TABLE}/index/*\"
        ]
      },
      {
        \"Sid\": \"Athena\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"athena:StartQueryExecution\",
          \"athena:GetQueryExecution\",
          \"athena:GetQueryResults\"
        ],
        \"Resource\": \"arn:aws:athena:${REGION}:${ACCOUNT_ID}:workgroup/*\"
      },
      {
        \"Sid\": \"GlueForAthena\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"glue:GetTable\",\"glue:GetDatabase\",
          \"glue:GetPartition\",\"glue:GetPartitions\"
        ],
        \"Resource\": [
          \"arn:aws:glue:${REGION}:${ACCOUNT_ID}:catalog\",
          \"arn:aws:glue:${REGION}:${ACCOUNT_ID}:database/${PV_DATABASE}\",
          \"arn:aws:glue:${REGION}:${ACCOUNT_ID}:table/${PV_DATABASE}/${PV_TABLE}\"
        ]
      },
      {
        \"Sid\": \"S3Athena\",
        \"Effect\": \"Allow\",
        \"Action\": [\"s3:GetObject\",\"s3:PutObject\",\"s3:GetBucketLocation\"],
        \"Resource\": [
          \"arn:aws:s3:::${PV_BUCKET_ATHENA_OUTPUT}\",
          \"arn:aws:s3:::${PV_BUCKET_ATHENA_OUTPUT}/*\"
        ]
      }
    ]
  }"
log_ok "  Policy attached."
echo ""

# ── Build Lambda package ──────────────────────────────────────────────────────
log_info "── Building Lambda package"
rm -rf "$PKG_TMP" && mkdir -p "$PKG_TMP"

# Step 1: Install pip dependencies (linux x86_64 wheels for Lambda runtime)
log_info "  Installing dependencies (linux/x86_64 wheels)..."
pip install -q \
  --platform manylinux2014_x86_64 \
  --python-version 3.11 \
  --only-binary=:all: \
  --implementation cp \
  --upgrade \
  -r "${SRC_DIR}/requirements.txt" \
  -t "$PKG_TMP/"
log_ok "  Dependencies installed."

# Step 2: Copy or obfuscate Python source
if [[ "$OBFUSCATE" == "true" ]]; then
  log_info "  Obfuscating source (linux.x86_64, cp311)..."
  OBF_TMP="/tmp/_pyarmor_pv_$$"
  rm -rf "$OBF_TMP" && mkdir -p "$OBF_TMP"

  PY_FILES=()
  for f in "$SRC_DIR"/*.py; do
    [[ -f "$f" ]] && PY_FILES+=("$f")
  done

  "$PYARMOR_CMD" gen \
    --platform linux.x86_64 \
    --output "$OBF_TMP" \
    "${PY_FILES[@]}" 2>/dev/null

  for f in "$OBF_TMP"/*.py; do
    [[ -f "$f" ]] && cp "$f" "$PKG_TMP/"
  done
  if [[ -d "$OBF_TMP/pyarmor_runtime_000000" ]]; then
    cp -r "$OBF_TMP/pyarmor_runtime_000000" "$PKG_TMP/"
    log_ok "  pyarmor_runtime_000000 bundled."
  fi
  rm -rf "$OBF_TMP"
else
  for f in "$SRC_DIR"/*.py; do
    [[ -f "$f" ]] && cp "$f" "$PKG_TMP/"
  done
  log_ok "  Source copied (plain)."
fi

# Step 3: Zip
cd "$PKG_TMP" && zip -qr "$ZIP_FILE" . && cd - > /dev/null
ZIP_KB=$(( $(wc -c < "$ZIP_FILE") / 1024 ))
rm -rf "$PKG_TMP"
OBFUSCATED_TAG=""; [[ "$OBFUSCATE" == "true" ]] && OBFUSCATED_TAG=" [obfuscated]"
log_ok "  Package: $ZIP_FILE  (${ZIP_KB} KB${OBFUSCATED_TAG})"
echo ""

# ── Environment variables ─────────────────────────────────────────────────────
ENV_JSON=$(python3 -c "
import json
env = {
  'Variables': {
    'REGION':               '${REGION}',
    'DEVICE_STATE_TABLE':   '${PV_DEVICE_STATE_TABLE}',
    'DATABASE':             '${PV_DATABASE}',
    'TABLE':                '${PV_TABLE}',
    'SECRET_KEY':           '${PV_SECRET_KEY}',
    'BUCKET_ATHENA_OUTPUT': '${PV_BUCKET_ATHENA_OUTPUT}',
    'TABLE_DEVICE_DATA':    '${PV_TABLE_DEVICE_DATA}',
    'THING_GROUP':          '${PV_THING_GROUP}',
  }
}
print(json.dumps(env))
")

# ── Create or update Lambda ───────────────────────────────────────────────────
log_info "── Deploying Lambda: $FUNC_NAME"
if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" \
     2>/dev/null | grep -q '"FunctionName"'; then
  log_info "  Function exists — updating code..."
  aws lambda update-function-code \
    --function-name "$FUNC_NAME" \
    --zip-file "fileb://$ZIP_FILE" \
    --region "$REGION" > /dev/null
  aws lambda wait function-updated \
    --function-name "$FUNC_NAME" --region "$REGION"
  aws lambda update-function-configuration \
    --function-name "$FUNC_NAME" \
    --runtime "$RUNTIME" \
    --handler "lambda_function.lambda_handler" \
    --timeout 30 \
    --memory-size 256 \
    --environment "$ENV_JSON" \
    --region "$REGION" > /dev/null
  aws lambda wait function-updated \
    --function-name "$FUNC_NAME" --region "$REGION"
  log_ok "  $FUNC_NAME — updated."
else
  log_info "  Function does not exist — creating (waiting for role to propagate)..."
  # IAM role propagation can take a few seconds
  sleep 10
  aws lambda create-function \
    --function-name "$FUNC_NAME" \
    --runtime "$RUNTIME" \
    --role "$ROLE_ARN" \
    --handler "lambda_function.lambda_handler" \
    --zip-file "fileb://$ZIP_FILE" \
    --timeout 30 \
    --memory-size 256 \
    --tracing-config Mode=Active \
    --environment "$ENV_JSON" \
    --description "IoT Fleet Provisioning pre-hook — validates MAC before device registration" \
    --region "$REGION" > /dev/null
  aws lambda wait function-active \
    --function-name "$FUNC_NAME" --region "$REGION"
  log_ok "  $FUNC_NAME — created."
fi
echo ""

# ── CloudWatch log group ──────────────────────────────────────────────────────
LOG_GROUP="/aws/lambda/${FUNC_NAME}"
log_info "── CloudWatch log group: $LOG_GROUP"
aws logs create-log-group \
  --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
aws logs put-retention-policy \
  --log-group-name "$LOG_GROUP" \
  --retention-in-days 30 \
  --region "$REGION"
log_ok "  Log group ready (30-day retention)."
echo ""

# ── IoT Fleet Provisioning invoke permission ──────────────────────────────────
log_info "── IoT Fleet Provisioning invoke permission"
aws lambda remove-permission \
  --function-name "$FUNC_NAME" \
  --statement-id "iot-fleet-provisioning-invoke" \
  --region "$REGION" 2>/dev/null || true
aws lambda add-permission \
  --function-name "$FUNC_NAME" \
  --statement-id "iot-fleet-provisioning-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "iot.amazonaws.com" \
  --source-account "$ACCOUNT_ID" \
  --region "$REGION" > /dev/null
log_ok "  IoT Fleet Provisioning can invoke $FUNC_NAME."
echo ""

# ── Publish version + prod alias ─────────────────────────────────────────────
GIT_SHA="$(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
log_info "── Publishing version (git:${GIT_SHA})"
VERSION=$(aws lambda publish-version \
  --function-name "$FUNC_NAME" \
  --description "git:${GIT_SHA} deployed:$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region "$REGION" \
  --query 'Version' --output text)
log_ok "  Version $VERSION published."

if aws lambda get-alias --function-name "$FUNC_NAME" --name prod \
     --region "$REGION" &>/dev/null; then
  aws lambda update-alias \
    --function-name "$FUNC_NAME" \
    --name prod \
    --function-version "$VERSION" \
    --region "$REGION" > /dev/null
  log_ok "  Alias 'prod' → v${VERSION}"
else
  aws lambda create-alias \
    --function-name "$FUNC_NAME" \
    --name prod \
    --function-version "$VERSION" \
    --region "$REGION" > /dev/null
  log_ok "  Alias 'prod' created → v${VERSION}"
fi
echo ""

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -f "$ZIP_FILE"

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_GREEN}${_BOLD}  provisioning_validation deployed successfully.${_NC}"
echo ""
echo -e "  Function : arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNC_NAME}"
echo -e "  Version  : $VERSION  (alias: prod)"
echo -e "  Obfuscate: $OBFUSCATE"
echo ""
echo -e "  ${_BOLD}Next step:${_NC}"
echo -e "  Set this Lambda as the pre-hook in your Fleet Provisioning template:"
echo ""
echo     "    aws iot describe-provisioning-template --template-name <your-template>"
echo     "    aws iot update-provisioning-template \\"
echo     "      --template-name <your-template> \\"
echo     "      --pre-provisioning-hook '{\"targetArn\":\"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNC_NAME}\",\"payloadVersion\":\"2020-04-01\"}' \\"
echo     "      --region ${REGION}"
echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo ""
