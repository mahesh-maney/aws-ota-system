#!/bin/bash
# Phase 7 — Deploy all OTA Lambda functions (admin + user)
set -euo pipefail
export AWS_PAGER="" PAGER=cat
source "$(dirname "$0")/_lib.sh"

log_section "Phase 7: Deploy Lambda Functions"

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
FUNC_PREFIX="${FUNC_PREFIX:-ota_}"   # Function name prefix. Use "digilux_ota_" to update the live deployment.
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

ADMIN_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME:-ota-lambda-role}"
USER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${USER_LAMBDA_ROLE_NAME:-ota-user-lambda-role}"

ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"
SIGNING_SECRET="${SIGNING_SECRET:-ota-signing-key}"
PACKAGES_TABLE="${PACKAGES_TABLE:-ota_packages}"
OTA_JOBS_TABLE="${OTA_JOBS_TABLE:-ota_jobs}"
COMPAT_TABLE="${COMPAT_TABLE:-ota_compatibility}"
DEPLOYMENTS_TABLE="${DEPLOYMENTS_TABLE:-ota_deployments}"
CONSENTS_TABLE="${CONSENTS_TABLE:-ota_user_consents}"
BETA_USERS_TABLE="${BETA_USERS_TABLE:-ota_beta_users}"
DEVICE_DATA_TABLE="${DEVICE_DATA_TABLE:-digilux_device_data}"
USER_COGNITO_POOL_ID="${USER_COGNITO_POOL_ID:-}"
SES_SENDER="${SES_SENDER_EMAIL:-noreply@example.com}"
ADMIN_EMAILS="${ADMIN_EMAILS:-}"   # comma-separated; e.g. ops@example.com,cto@example.com

IOT_ROOT_GROUP="${IOT_ROOT_GROUP:-DIGILUX}"
IOT_PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-PRODUCTION}"
# When IOT_PRODUCTION_GROUP is blank (e.g. Honeywell 2-level hierarchy),
# the root group IS the production group — fall back to root.
EFFECTIVE_PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-$IOT_ROOT_GROUP}"
IOT_GATEWAYS_GROUP="${IOT_GATEWAYS_GROUP:-GATEWAYS}"
IOT_TOUCH_PANELS_GROUP="${IOT_TOUCH_PANELS_GROUP:-TOUCH-PANELS}"
IOT_GATEWAY_MODELS="${IOT_GATEWAY_MODELS:-DGW-100,DGW-200}"
IOT_TOUCH_PANEL_MODELS="${IOT_TOUCH_PANEL_MODELS:-TP-100,TP-200}"
CLOUDFRONT_DOMAIN="${CLOUDFRONT_DOMAIN:-}"
CLOUDFRONT_KEY_PAIR_ID="${CLOUDFRONT_KEY_PAIR_ID:-}"
CLOUDFRONT_KEY_SECRET="${CLOUDFRONT_KEY_SECRET_NAME:-ota-cloudfront-key}"
PRESIGN_EXPIRY_TIER1_MAX_MB="${PRESIGN_EXPIRY_TIER1_MAX_MB:-50}"
PRESIGN_EXPIRY_TIER1_SEC="${PRESIGN_EXPIRY_TIER1_SEC:-3600}"
PRESIGN_EXPIRY_TIER2_MAX_MB="${PRESIGN_EXPIRY_TIER2_MAX_MB:-200}"
PRESIGN_EXPIRY_TIER2_SEC="${PRESIGN_EXPIRY_TIER2_SEC:-21600}"
PRESIGN_EXPIRY_TIER3_MAX_MB="${PRESIGN_EXPIRY_TIER3_MAX_MB:-500}"
PRESIGN_EXPIRY_TIER3_SEC="${PRESIGN_EXPIRY_TIER3_SEC:-86400}"
PRESIGN_EXPIRY_TIER4_SEC="${PRESIGN_EXPIRY_TIER4_SEC:-172800}"
IOT_JOB_TIMEOUT_MINUTES="${IOT_JOB_TIMEOUT_MINUTES:-1440}"
CANARY_MAX="${CANARY_MAX:-5}"
KMS_KEY_ID="${KMS_KEY_ID:-}"   # KMS key ID/alias for artifact envelope encryption
STALE_JOB_DAYS="${STALE_JOB_DAYS:-7}"
KEY_REQUESTS_TABLE="${KEY_REQUESTS_TABLE:-ota_key_requests}"

LAMBDA_DIR="$(cd "$(dirname "$0")/06_lambdas" && pwd)"
RUNTIME="python3.11"

# Short git SHA for version description (falls back to timestamp if not in a repo)
GIT_SHA="$(git -C "$(dirname "$0")" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"

log_info "Prefix          : $PREFIX"
log_info "Func prefix     : $FUNC_PREFIX"
log_info "Region          : $REGION"
log_info "Account         : $ACCOUNT_ID"
log_info "Admin role      : $ADMIN_ROLE_ARN"
log_info "User role       : $USER_ROLE_ARN"
log_info "Artifact bucket : $ARTIFACT_BUCKET"
log_info "Lambda dir      : $LAMBDA_DIR"

require_cmd python3 pip zip

# ── PyArmor obfuscation setup ─────────────────────────────────────────────────
# Set OBFUSCATE=true to encrypt all Lambda source before packaging.
# Uses --platform linux.x86_64.11 so obfuscation works from any OS (Mac/Win/Linux).
OBFUSCATE="${OBFUSCATE:-false}"
PYARMOR_CMD=""
if [[ "$OBFUSCATE" == "true" ]]; then
  log_info "Obfuscation enabled — installing PyArmor under Python 3.11..."
  # Must use python3.11 so the generated runtime .so matches Lambda's Python 3.11
  if ! command -v python3.11 &>/dev/null; then
    log_warn "python3.11 not found — obfuscation requires Python 3.11. Install it and retry."
    exit 1
  fi
  python3.11 -m pip install -q pyarmor
  # Resolve the pyarmor executable — try sys.prefix, user base, and PATH
  _PYARMOR_CANDIDATES=(
    "$(python3.11 -c 'import sys; print(sys.prefix)')/bin/pyarmor"
    "$(python3.11 -c 'import site; print(site.getuserbase())')/bin/pyarmor"
    "$(python3.11 -m pip show pyarmor 2>/dev/null | grep ^Location | awk '{print $2}')/../../../bin/pyarmor"
  )
  PYARMOR_CMD=""
  for _c in "${_PYARMOR_CANDIDATES[@]}"; do
    _c="$(realpath "$_c" 2>/dev/null || echo "$_c")"
    if [[ -x "$_c" ]]; then
      PYARMOR_CMD="$_c"
      break
    fi
  done
  # Last resort: check PATH
  if [[ -z "$PYARMOR_CMD" ]] && command -v pyarmor &>/dev/null; then
    PYARMOR_CMD="$(command -v pyarmor)"
  fi
  if [[ -z "$PYARMOR_CMD" ]]; then
    log_warn "pyarmor executable not found. Try: python3.11 -m pip install --user pyarmor"
    exit 1
  fi
  log_ok "$($PYARMOR_CMD --version 2>&1 | head -1) ready  (target: linux.x86_64, cp311)"
fi

# ── KMS key — create if not supplied ──────────────────────────────────────────
# artifact_processor uses KMS envelope encryption for per-artifact AES keys.
# If KMS_KEY_ID is blank, create a new CMK + alias/digilux-ota-data-key now.
KMS_ALIAS="alias/${PREFIX}-ota-data-key"
if [[ -z "$KMS_KEY_ID" ]]; then
  # Check if the alias already exists
  _existing_kms=$(aws kms describe-key --key-id "$KMS_ALIAS" \
    --region "$REGION" --query "KeyMetadata.KeyId" --output text 2>/dev/null || true)
  if [[ -n "$_existing_kms" ]]; then
    KMS_KEY_ID="$KMS_ALIAS"
    log_ok "KMS key already exists: $KMS_ALIAS → $_existing_kms"
  else
    log_step "Creating KMS key for artifact envelope encryption"
    _new_key_id=$(aws kms create-key \
      --description "OTA artifact data key encryption — ${PREFIX}" \
      --region "$REGION" \
      --query "KeyMetadata.KeyId" --output text)
    aws kms create-alias \
      --alias-name "$KMS_ALIAS" \
      --target-key-id "$_new_key_id" \
      --region "$REGION"
    KMS_KEY_ID="$KMS_ALIAS"
    log_ok "KMS key created: $_new_key_id  alias: $KMS_ALIAS"
  fi
else
  # KMS_KEY_ID was explicitly set in deploy.config.
  # Check if the alias/key exists in the target account — create it if not.
  _existing_kms=$(aws kms describe-key --key-id "$KMS_KEY_ID" \
    --region "$REGION" --query "KeyMetadata.KeyId" --output text 2>/dev/null || true)
  if [[ -n "$_existing_kms" ]]; then
    log_ok "KMS key (from config): $KMS_KEY_ID → $_existing_kms"
  else
    log_step "KMS key '$KMS_KEY_ID' not found in this account — creating it"
    _new_key_id=$(aws kms create-key \
      --description "OTA artifact data key encryption — ${PREFIX}" \
      --region "$REGION" \
      --query "KeyMetadata.KeyId" --output text)
    # KMS_KEY_ID may be a bare name (digilux-ota-data-key) or full alias (alias/...)
    _alias_name="$KMS_KEY_ID"
    [[ "$_alias_name" != alias/* ]] && _alias_name="alias/${_alias_name}"
    aws kms create-alias \
      --alias-name "$_alias_name" \
      --target-key-id "$_new_key_id" \
      --region "$REGION"
    KMS_KEY_ID="$_alias_name"
    log_ok "KMS key created: $_new_key_id  alias: $_alias_name"
  fi
fi

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
        'KMS_KEY_ID': '${KMS_KEY_ID}',
        'SIGNING_SECRET': '${SIGNING_SECRET}',
        'CLOUDFRONT_DOMAIN': '${CLOUDFRONT_DOMAIN}',
        'CLOUDFRONT_KEY_PAIR_ID': '${CLOUDFRONT_KEY_PAIR_ID}',
        'CLOUDFRONT_PRIVATE_KEY_SECRET': '${CLOUDFRONT_KEY_SECRET}',
        'COGNITO_POOL_ID': '${USER_COGNITO_POOL_ID}',
        'COGNITO_USER_POOL_ID': '${USER_COGNITO_POOL_ID}',
        'SES_SENDER': '${SES_SENDER}',
        'ADMIN_EMAILS': '${ADMIN_EMAILS}',
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
        'PRODUCTION_GROUP': '${EFFECTIVE_PRODUCTION_GROUP}',
        'GATEWAYS_GROUP': '${IOT_GATEWAYS_GROUP}',
        'TOUCH_PANELS_GROUP': '${IOT_TOUCH_PANELS_GROUP}',
        'GATEWAY_MODELS': '${IOT_GATEWAY_MODELS}',
        'TOUCH_PANEL_MODELS': '${IOT_TOUCH_PANEL_MODELS}',
        'DEVICE_DATA_USER_INDEX': 'userId-index',
        'CONSENTS_USER_INDEX': 'userId-deviceId-index',
        'CONSENTS_JOB_INDEX': 'jobId-index',
        'CONSENTS_DEPLOYMENT_INDEX': 'deploymentId-index',
        'IOT_ENDPOINT': '${IOT_ENDPOINT:-}',
        'STALE_JOB_DAYS': '${STALE_JOB_DAYS}',
        'KEY_REQUESTS_TABLE': '${KEY_REQUESTS_TABLE}',
        'LOG_LEVEL': 'INFO',
    }
}
print(json.dumps(env))
" > "$ENV_JSON_FILE"
log_ok "Environment JSON written to $ENV_JSON_FILE  ($(wc -c < "$ENV_JSON_FILE") bytes)"

# ── _obfuscate_lambda ─────────────────────────────────────────────────────────
# Obfuscates all .py files from SRC_DIR into DEST_DIR using PyArmor.
# Generates a linux.x86_64 Python 3.11 compatible runtime — works from any OS.
_obfuscate_lambda() {
  local SRC_DIR="$1"
  local DEST_DIR="$2"
  local OBF_TMP="/tmp/_pyarmor_obf_$$"
  rm -rf "$OBF_TMP" && mkdir -p "$OBF_TMP"

  # Collect all .py source files (exclude __pycache__)
  local PY_FILES=()
  for f in "$SRC_DIR"/*.py; do
    [[ -f "$f" ]] && PY_FILES+=("$f")
  done

  if [[ ${#PY_FILES[@]} -eq 0 ]]; then
    log_warn "  No .py files found in $SRC_DIR — skipping obfuscation."
    return
  fi

  $PYARMOR_CMD gen \
    --platform linux.x86_64 \
    --output "$OBF_TMP" \
    "${PY_FILES[@]}" 2>/dev/null

  # Copy obfuscated .py files into destination
  for f in "$OBF_TMP"/*.py; do
    [[ -f "$f" ]] && cp "$f" "$DEST_DIR/"
  done
  # Copy the PyArmor runtime package (required at Lambda runtime)
  if [[ -d "$OBF_TMP/pyarmor_runtime_000000" ]]; then
    cp -r "$OBF_TMP/pyarmor_runtime_000000" "$DEST_DIR/"
    log_info "  pyarmor_runtime_000000 bundled."
  fi

  rm -rf "$OBF_TMP"
}

# ── deploy_lambda ──────────────────────────────────────────────────────────────
# Usage: deploy_lambda <src_suffix> <role_arn> [timeout_sec] [memory_mb] [tmp_mb]
#   src_suffix: the part after "digilux_ota_" in the source directory name
#   Deployed function name: ota_<src_suffix>
deploy_lambda() {
  local SRC_SUFFIX="$1"
  local ROLE_ARN="$2"
  local TIMEOUT="${3:-30}"
  local MEMORY="${4:-256}"
  local TMP_MB="${5:-512}"
  local SRC_DIR_NAME="digilux_ota_${SRC_SUFFIX}"
  local FUNC_NAME="${FUNC_PREFIX}${SRC_SUFFIX}"
  local SRC_DIR="${LAMBDA_DIR}/${SRC_DIR_NAME}"
  local ZIP_FILE="/tmp/${FUNC_NAME}.zip"

  log_step "$FUNC_NAME"

  if [[ ! -d "$SRC_DIR" ]]; then
    log_warn "Source directory not found: $SRC_DIR — skipping."
    return
  fi

  # ── Build zip ────────────────────────────────────────────────────────────────
  local PKG_TMP="/tmp/${FUNC_NAME}_pkg"
  rm -rf "$PKG_TMP" && mkdir -p "$PKG_TMP"

  # Step 1: Install pip dependencies (linux/x86_64 wheels) if needed
  if [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    log_info "  Installing dependencies for $FUNC_NAME (linux/x86_64)..."
    pip install -q \
      --platform manylinux2014_x86_64 \
      --python-version 3.11 \
      --only-binary=:all: \
      --implementation cp \
      --upgrade \
      -r "${SRC_DIR}/requirements.txt" \
      -t "$PKG_TMP/"
  fi

  # Step 2: Copy or obfuscate Python source files
  if [[ "$OBFUSCATE" == "true" ]]; then
    log_info "  Obfuscating $FUNC_NAME source (linux.x86_64)..."
    _obfuscate_lambda "$SRC_DIR" "$PKG_TMP"
  else
    shopt -s nullglob
    for f in "$SRC_DIR"/*.py; do cp "$f" "$PKG_TMP/"; done
    shopt -u nullglob
  fi

  # Step 2b: Auto-sync master _notif.py if this Lambda imports it
  local MASTER_NOTIF="${LAMBDA_DIR}/_notif.py"
  if [[ -f "$MASTER_NOTIF" ]] && grep -q "import _notif" "${SRC_DIR}/lambda_function.py" 2>/dev/null; then
    cp "$MASTER_NOTIF" "$PKG_TMP/_notif.py"
    log_info "  _notif.py synced from master"
  fi

  # Step 3: Copy non-Python sidecars (JSON configs, etc.)
  shopt -s nullglob
  for f in "$SRC_DIR"/*.json; do [[ -f "$f" ]] && cp "$f" "$PKG_TMP/"; done
  shopt -u nullglob

  # Step 4: Zip everything and report size
  cd "$PKG_TMP" && zip -qr "$ZIP_FILE" . && cd - > /dev/null
  local ZIP_KB=$(( $(wc -c < "$ZIP_FILE") / 1024 ))
  rm -rf "$PKG_TMP"
  local OBFUSCATED_TAG=""
  [[ "$OBFUSCATE" == "true" ]] && OBFUSCATED_TAG=" [obfuscated]"
  if [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    log_info "  Zip: $ZIP_FILE  (${ZIP_KB} KB with deps${OBFUSCATED_TAG})"
  else
    log_info "  Zip: $ZIP_FILE  (${ZIP_KB} KB${OBFUSCATED_TAG})"
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
      --timeout "$TIMEOUT" \
      --memory-size "$MEMORY" \
      --ephemeral-storage "{\"Size\":${TMP_MB}}" \
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
      --timeout "$TIMEOUT" \
      --memory-size "$MEMORY" \
      --ephemeral-storage "{\"Size\":${TMP_MB}}" \
      --tracing-config Mode=Active \
      --environment "file://${ENV_JSON_FILE}" \
      --description "OTA — ${FUNC_NAME}" \
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

  # ── Publish an immutable version snapshot ────────────────────────────────────
  local VERSION
  VERSION=$(aws lambda publish-version \
    --function-name "$FUNC_NAME" \
    --description "git:${GIT_SHA} deployed:$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --region "$REGION" \
    --query 'Version' --output text)
  log_info "  Published version: $VERSION  (git:${GIT_SHA})"

  # ── Create or update 'prod' alias ────────────────────────────────────────────
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

  rm -f "$ZIP_FILE"
}

# ── Admin Lambdas ─────────────────────────────────────────────────────────────
log_section "Admin Lambdas"
deploy_lambda "upload_url"          "$ADMIN_ROLE_ARN"
deploy_lambda "upload_complete"     "$ADMIN_ROLE_ARN"
deploy_lambda "package_status"      "$ADMIN_ROLE_ARN"
deploy_lambda "package_activate"    "$ADMIN_ROLE_ARN"
# artifact_processor handles large firmware files — 300s timeout, 2048MB memory/tmp
deploy_lambda "artifact_processor"  "$ADMIN_ROLE_ARN" 300 2048 2048

deploy_lambda "artifact_key"        "$ADMIN_ROLE_ARN"
deploy_lambda "beta_users"          "$ADMIN_ROLE_ARN"
deploy_lambda "job_create"          "$ADMIN_ROLE_ARN"
deploy_lambda "job_sync"            "$ADMIN_ROLE_ARN"
deploy_lambda "key_server"          "$ADMIN_ROLE_ARN"
deploy_lambda "status_handler"      "$ADMIN_ROLE_ARN"
deploy_lambda "device_register"     "$ADMIN_ROLE_ARN"

# ── User Lambdas ──────────────────────────────────────────────────────────────
log_section "User Lambdas"
deploy_lambda "user_check_updates"     "$USER_ROLE_ARN"
deploy_lambda "user_consent"           "$USER_ROLE_ARN"
deploy_lambda "user_update_status"     "$USER_ROLE_ARN"

# ── IoT invoke permissions ────────────────────────────────────────────────────
# Grant IoT rules permission to invoke both $LATEST and the 'prod' alias.
log_section "IoT Invoke Permissions"
for SUFFIX in status_handler device_register artifact_key; do
  FUNC="${FUNC_PREFIX}${SUFFIX}"
  # Permission on the unqualified function (covers $LATEST)
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
  # Permission on the 'prod' alias
  ALIAS_STMT_ID="iot-rule-invoke-${FUNC}-prod"
  aws lambda remove-permission \
    --function-name "$FUNC" \
    --qualifier prod \
    --statement-id "$ALIAS_STMT_ID" \
    --region "$REGION" 2>/dev/null || true
  aws lambda add-permission \
    --function-name "$FUNC" \
    --qualifier prod \
    --statement-id "$ALIAS_STMT_ID" \
    --action "lambda:InvokeFunction" \
    --principal "iot.amazonaws.com" \
    --source-account "$ACCOUNT_ID" \
    --region "$REGION" > /dev/null
  log_ok "IoT → ${FUNC}:prod"
done

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -f "$ENV_JSON_FILE"

log_phase_done
log_ok "All 18 Lambda functions deployed successfully."
