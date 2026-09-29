#!/bin/bash
# Phase 7 — Deploy all OTA Lambda functions (admin + user)
set -euo pipefail
export AWS_PAGER="" PAGER=cat
source "$(dirname "$0")/_lib.sh"

log_section "Phase 7: Deploy Lambda Functions"

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

ADMIN_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME:-${PREFIX}-ota-lambda-role}"
USER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${USER_LAMBDA_ROLE_NAME:-${PREFIX}-ota-user-lambda-role}"

ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"
SIGNING_SECRET="${SIGNING_SECRET:-${PREFIX}-ota-signing-key}"
PACKAGES_TABLE="${PACKAGES_TABLE:-${PREFIX}_ota_packages}"
OTA_JOBS_TABLE="${OTA_JOBS_TABLE:-${PREFIX}_ota_jobs}"
COMPAT_TABLE="${COMPAT_TABLE:-${PREFIX}_ota_compatibility}"
DEPLOYMENTS_TABLE="${DEPLOYMENTS_TABLE:-${PREFIX}_ota_deployments}"
CONSENTS_TABLE="${CONSENTS_TABLE:-${PREFIX}_ota_user_consents}"
BETA_USERS_TABLE="${BETA_USERS_TABLE:-${PREFIX}_ota_beta_users}"
DEVICE_DATA_TABLE="${DEVICE_DATA_TABLE:-${PREFIX}_device_data}"
USER_COGNITO_POOL_ID="${USER_COGNITO_POOL_ID:-}"
SES_SENDER="${SES_SENDER_EMAIL:-noreply@example.com}"

IOT_ROOT_GROUP="${IOT_ROOT_GROUP:-DIGILUX}"
IOT_PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-PRODUCTION}"
IOT_GATEWAYS_GROUP="${IOT_GATEWAYS_GROUP:-GATEWAYS}"
IOT_TOUCH_PANELS_GROUP="${IOT_TOUCH_PANELS_GROUP:-TOUCH-PANELS}"
IOT_GATEWAY_MODELS="${IOT_GATEWAY_MODELS:-DGW-100,DGW-200}"
IOT_TOUCH_PANEL_MODELS="${IOT_TOUCH_PANEL_MODELS:-TP-100,TP-200}"
CLOUDFRONT_DOMAIN="${CLOUDFRONT_DOMAIN:-}"
CLOUDFRONT_KEY_PAIR_ID="${CLOUDFRONT_KEY_PAIR_ID:-}"
CLOUDFRONT_KEY_SECRET="${CLOUDFRONT_KEY_SECRET_NAME:-${PREFIX}-ota-cloudfront-key}"
PRESIGN_EXPIRY_TIER1_MAX_MB="${PRESIGN_EXPIRY_TIER1_MAX_MB:-50}"
PRESIGN_EXPIRY_TIER1_SEC="${PRESIGN_EXPIRY_TIER1_SEC:-3600}"
PRESIGN_EXPIRY_TIER2_MAX_MB="${PRESIGN_EXPIRY_TIER2_MAX_MB:-200}"
PRESIGN_EXPIRY_TIER2_SEC="${PRESIGN_EXPIRY_TIER2_SEC:-21600}"
PRESIGN_EXPIRY_TIER3_MAX_MB="${PRESIGN_EXPIRY_TIER3_MAX_MB:-500}"
PRESIGN_EXPIRY_TIER3_SEC="${PRESIGN_EXPIRY_TIER3_SEC:-86400}"
PRESIGN_EXPIRY_TIER4_SEC="${PRESIGN_EXPIRY_TIER4_SEC:-172800}"
IOT_JOB_TIMEOUT_MINUTES="${IOT_JOB_TIMEOUT_MINUTES:-1440}"
CANARY_MAX="${CANARY_MAX:-5}"

LAMBDA_DIR="$(cd "$(dirname "$0")/06_lambdas" && pwd)"
RUNTIME="python3.11"

log_info "Prefix          : $PREFIX"
log_info "Region          : $REGION"
log_info "Account         : $ACCOUNT_ID"
log_info "Admin role      : $ADMIN_ROLE_ARN"
log_info "User role       : $USER_ROLE_ARN"
log_info "Artifact bucket : $ARTIFACT_BUCKET"
log_info "Lambda dir      : $LAMBDA_DIR"

require_cmd python3 pip zip

# ── Build environment JSON file ────────────────────────────────────────────────
# AWS CLI --environment accepts: {"Variables":{"KEY":"VALUE",...}}
# We write this to a temp file to avoid all shell quoting issues.
ENV_JSON_FILE="/tmp/${PREFIX}_ota_lambda_env.json"
python3 -c "
import json, sys
env = {
    'Variables': {
        'REGION': '${REGION}',
        'ACCOUNT_ID': '${ACCOUNT_ID}',
        'DEVICE_DATA_TABLE': '${DEVICE_DATA_TABLE}',
        'PACKAGES_TABLE': '${PACKAGES_TABLE}',
        'OTA_JOBS_TABLE': '${OTA_JOBS_TABLE}',
        'COMPAT_TABLE': '${COMPAT_TABLE}',
        'CONSENTS_TABLE': '${CONSENTS_TABLE}',
        'DEPLOYMENTS_TABLE': '${DEPLOYMENTS_TABLE}',
        'DEPLOYMENTS_PKG_STATUS_INDEX': 'packageName-status-index',
        'BETA_USERS_TABLE': '${BETA_USERS_TABLE}',
        'ARTIFACT_BUCKET': '${ARTIFACT_BUCKET}',
        'SIGNING_SECRET': '${SIGNING_SECRET}',
        'CLOUDFRONT_DOMAIN': '${CLOUDFRONT_DOMAIN}',
        'CLOUDFRONT_KEY_PAIR_ID': '${CLOUDFRONT_KEY_PAIR_ID}',
        'CLOUDFRONT_PRIVATE_KEY_SECRET': '${CLOUDFRONT_KEY_SECRET}',
        'COGNITO_POOL_ID': '${USER_COGNITO_POOL_ID}',
        'COGNITO_USER_POOL_ID': '${USER_COGNITO_POOL_ID}',
        'SES_SENDER': '${SES_SENDER}',
        'PRESIGN_EXPIRY_TIER1_MAX_MB': '${PRESIGN_EXPIRY_TIER1_MAX_MB}',
        'PRESIGN_EXPIRY_TIER1_SEC': '${PRESIGN_EXPIRY_TIER1_SEC}',
        'PRESIGN_EXPIRY_TIER2_MAX_MB': '${PRESIGN_EXPIRY_TIER2_MAX_MB}',
        'PRESIGN_EXPIRY_TIER2_SEC': '${PRESIGN_EXPIRY_TIER2_SEC}',
        'PRESIGN_EXPIRY_TIER3_MAX_MB': '${PRESIGN_EXPIRY_TIER3_MAX_MB}',
        'PRESIGN_EXPIRY_TIER3_SEC': '${PRESIGN_EXPIRY_TIER3_SEC}',
        'PRESIGN_EXPIRY_TIER4_SEC': '${PRESIGN_EXPIRY_TIER4_SEC}',
        'IOT_JOB_TIMEOUT_MINUTES': '${IOT_JOB_TIMEOUT_MINUTES}',
        'CANARY_GROUP': '${IOT_ROOT_GROUP}',
        'CANARY_MAX': '${CANARY_MAX}',
        'ROOT_GROUP': '${IOT_ROOT_GROUP}',
        'PRODUCTION_GROUP': '${IOT_PRODUCTION_GROUP}',
        'GATEWAYS_GROUP': '${IOT_GATEWAYS_GROUP}',
        'TOUCH_PANELS_GROUP': '${IOT_TOUCH_PANELS_GROUP}',
        'GATEWAY_MODELS': '${IOT_GATEWAY_MODELS}',
        'TOUCH_PANEL_MODELS': '${IOT_TOUCH_PANEL_MODELS}',
        'DEVICE_DATA_USER_INDEX': 'userId-index',
        'CONSENTS_USER_INDEX': 'userId-deviceId-index',
        'CONSENTS_JOB_INDEX': 'jobId-index',
        'CONSENTS_DEPLOYMENT_INDEX': 'deploymentId-index',
        'LOG_LEVEL': 'INFO',
    }
}
print(json.dumps(env))
" > "$ENV_JSON_FILE"
log_ok "Environment JSON written to $ENV_JSON_FILE  ($(wc -c < "$ENV_JSON_FILE") bytes)"

# ── deploy_lambda ──────────────────────────────────────────────────────────────
# Usage: deploy_lambda <src_suffix> <role_arn>
#   src_suffix: the part after "digilux_ota_" in the source directory name
#   Deployed function name: ${PREFIX}_ota_<src_suffix>
deploy_lambda() {
  local SRC_SUFFIX="$1"
  local ROLE_ARN="$2"
  local SRC_DIR_NAME="digilux_ota_${SRC_SUFFIX}"
  local FUNC_NAME="${PREFIX}_ota_${SRC_SUFFIX}"
  local SRC_DIR="${LAMBDA_DIR}/${SRC_DIR_NAME}"
  local ZIP_FILE="/tmp/${FUNC_NAME}.zip"

  log_step "$FUNC_NAME"

  if [[ ! -d "$SRC_DIR" ]]; then
    log_warn "Source directory not found: $SRC_DIR — skipping."
    return
  fi

  # ── Build zip ────────────────────────────────────────────────────────────────
  if [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    log_info "  Installing dependencies for $FUNC_NAME (linux/x86_64)..."
    local PKG_TMP="/tmp/${FUNC_NAME}_pkg"
    rm -rf "$PKG_TMP" && mkdir -p "$PKG_TMP"
    pip install -q \
      --platform manylinux2014_x86_64 \
      --python-version 3.11 \
      --only-binary=:all: \
      --implementation cp \
      --upgrade \
      -r "${SRC_DIR}/requirements.txt" \
      -t "$PKG_TMP/"
    cp "${SRC_DIR}/lambda_function.py" "$PKG_TMP/"
    cd "$PKG_TMP" && zip -qr "$ZIP_FILE" . && cd - > /dev/null
    local ZIP_KB=$(( $(wc -c < "$ZIP_FILE") / 1024 ))
    rm -rf "$PKG_TMP"
    log_info "  Zip: $ZIP_FILE  (${ZIP_KB} KB with deps)"
  else
    cd "$SRC_DIR" && zip -q "$ZIP_FILE" lambda_function.py && cd - > /dev/null
    log_info "  Zip: $ZIP_FILE  (no deps)"
  fi

  # ── Create or update ──────────────────────────────────────────────────────────
  if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" \
       2>/dev/null | grep -q '"FunctionName"'; then
    log_info "  Updating existing function..."
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
      --environment "file://${ENV_JSON_FILE}" \
      --region "$REGION" > /dev/null
    aws lambda wait function-updated \
      --function-name "$FUNC_NAME" --region "$REGION"
    log_ok "  $FUNC_NAME — updated."
  else
    log_info "  Creating new function (role: $ROLE_ARN)..."
    aws lambda create-function \
      --function-name "$FUNC_NAME" \
      --runtime "$RUNTIME" \
      --role "$ROLE_ARN" \
      --handler "lambda_function.lambda_handler" \
      --zip-file "fileb://$ZIP_FILE" \
      --timeout 30 \
      --memory-size 256 \
      --tracing-config Mode=Active \
      --environment "file://${ENV_JSON_FILE}" \
      --description "${PREFIX} OTA — ${FUNC_NAME}" \
      --region "$REGION" > /dev/null
    aws lambda wait function-active \
      --function-name "$FUNC_NAME" --region "$REGION"
    log_ok "  $FUNC_NAME — created."
  fi

  # ── CloudWatch log group with 30-day retention ────────────────────────────────
  local LOG_GROUP="/aws/lambda/${FUNC_NAME}"
  aws logs create-log-group \
    --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days 30 \
    --region "$REGION"
  log_info "  Log group: $LOG_GROUP  (30-day retention)"

  rm -f "$ZIP_FILE"
}

# ── Admin Lambdas ─────────────────────────────────────────────────────────────
log_section "Admin Lambdas"
deploy_lambda "upload_url"          "$ADMIN_ROLE_ARN"
deploy_lambda "package_activate"    "$ADMIN_ROLE_ARN"
deploy_lambda "artifact_processor"  "$ADMIN_ROLE_ARN"
deploy_lambda "package_register"    "$ADMIN_ROLE_ARN"
deploy_lambda "compatibility_check" "$ADMIN_ROLE_ARN"
deploy_lambda "job_create"          "$ADMIN_ROLE_ARN"
deploy_lambda "status_handler"      "$ADMIN_ROLE_ARN"
deploy_lambda "device_register"     "$ADMIN_ROLE_ARN"

# ── User Lambdas ──────────────────────────────────────────────────────────────
log_section "User Lambdas"
deploy_lambda "user_check_updates"     "$USER_ROLE_ARN"
deploy_lambda "user_consent"           "$USER_ROLE_ARN"
deploy_lambda "user_update_status"     "$USER_ROLE_ARN"
deploy_lambda "user_get_download_link" "$USER_ROLE_ARN"

# ── IoT invoke permissions ────────────────────────────────────────────────────
log_section "IoT Invoke Permissions"
for SUFFIX in status_handler device_register; do
  FUNC="${PREFIX}_ota_${SUFFIX}"
  STMT_ID="iot-rule-invoke-${FUNC}"
  aws lambda remove-permission \
    --function-name "$FUNC" \
    --statement-id "$STMT_ID" \
    --region "$REGION" 2>/dev/null || true
  aws lambda add-permission \
    --function-name "$FUNC" \
    --statement-id "$STMT_ID" \
    --action "lambda:InvokeFunction" \
    --principal "iot.amazonaws.com" \
    --source-account "$ACCOUNT_ID" \
    --region "$REGION" > /dev/null
  log_ok "IoT → $FUNC"
done

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -f "$ENV_JSON_FILE"

log_phase_done
log_ok "All 12 Lambda functions deployed successfully."
