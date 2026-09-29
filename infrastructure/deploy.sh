#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Zero-touch OTA deployment orchestrator
#
# Usage:
#   ./deploy.sh                        # runs all phases
#   ./deploy.sh --phase 04             # run only phase 04_dynamodb.sh
#   ./deploy.sh --from 07              # run from phase 07 onwards
#   ./deploy.sh --config path/to.cfg   # use a different config file
#   ./deploy.sh --ui-only              # deploy admin UI only
#
# Prerequisites:
#   1. Copy deploy.config.template → deploy.config and fill in all values.
#   2. AWS CLI configured (aws configure or environment variables).
#   3. Sufficient IAM permissions (see docs/DEPLOY_PERMISSIONS.md).
#   4. For admin UI: Node.js + npm installed.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER=""
export PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse arguments ───────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
ONLY_PHASE=""
FROM_PHASE=""
UI_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)   CONFIG_FILE="$2"; shift 2 ;;
    --phase)    ONLY_PHASE="$2"; shift 2 ;;
    --from)     FROM_PHASE="$2"; shift 2 ;;
    --ui-only)  UI_ONLY=true; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo ""
  echo "ERROR: Configuration file not found: $CONFIG_FILE"
  echo ""
  echo "  1. Copy the template:   cp deploy.config.template deploy.config"
  echo "  2. Fill in all values in deploy.config"
  echo "  3. Re-run: ./deploy.sh"
  echo ""
  exit 1
fi

set -a
# shellcheck source=deploy.config
source "$CONFIG_FILE"
set +a

# ── Validate required vars ────────────────────────────────────────────────────
_require() {
  local var="$1" label="$2"
  if [[ -z "${!var:-}" || "${!var}" == YOUR_* ]]; then
    echo "ERROR: $label is not set in $CONFIG_FILE  (variable: $var)"
    MISSING=true
  fi
}

MISSING=false
_require PREFIX              "Deployment prefix"
_require REGION              "AWS region"
_require API_GATEWAY_ID      "API Gateway ID"
_require API_GATEWAY_STAGE   "API Gateway stage"
_require ADMIN_COGNITO_AUTHORIZER_ID "Admin Cognito authorizer ID"
_require USER_COGNITO_AUTHORIZER_ID  "User Cognito authorizer ID"
_require USER_COGNITO_POOL_ID        "User Cognito pool ID"
_require DEVICE_DATA_TABLE   "Device data DynamoDB table"
_require ALERT_EMAIL         "Alert email"
_require SES_SENDER_EMAIL    "SES sender email"

if [[ "$MISSING" == "true" ]]; then
  echo ""
  echo "Please fill in all required values in: $CONFIG_FILE"
  exit 1
fi

# ── Prerequisite commands ─────────────────────────────────────────────────────
echo "==> Checking prerequisites..."
MISSING_CMDS=()
for CMD in aws openssl python3 pip zip; do
  command -v "$CMD" &>/dev/null || MISSING_CMDS+=("$CMD")
done
if [[ ${#MISSING_CMDS[@]} -gt 0 ]]; then
  echo "ERROR: Missing required commands: ${MISSING_CMDS[*]}"
  echo "       Install them before running deploy.sh"
  exit 1
fi
echo "    aws, openssl, python3, pip, zip — all present."

# ── Verify AWS credentials ────────────────────────────────────────────────────
echo "==> Verifying AWS credentials..."
if ! CALLER=$(aws sts get-caller-identity --output json 2>&1); then
  echo "ERROR: AWS credentials not configured or expired."
  echo "       Run 'aws configure' or set AWS_PROFILE / AWS_ACCESS_KEY_ID."
  echo "       AWS error: $CALLER"
  exit 1
fi
CALLER_ACCOUNT=$(echo "$CALLER" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
CALLER_ARN=$(echo "$CALLER"     | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
echo "    Account : $CALLER_ACCOUNT"
echo "    Identity: $CALLER_ARN"
if [[ -n "${ACCOUNT_ID:-}" && "$CALLER_ACCOUNT" != "$ACCOUNT_ID" ]]; then
  echo "ERROR: Logged-in account ($CALLER_ACCOUNT) does not match ACCOUNT_ID in config ($ACCOUNT_ID)."
  echo "       Check your AWS_PROFILE or credentials."
  exit 1
fi

# ── Derive defaults for optional vars ────────────────────────────────────────
export PREFIX REGION
export ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

# Resource names derived from PREFIX
export ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"
export SIGNING_SECRET="${SIGNING_SECRET:-${PREFIX}-ota-signing-key}"

# Lambda role names
export LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-${PREFIX}-ota-lambda-role}"
export USER_LAMBDA_ROLE_NAME="${USER_LAMBDA_ROLE_NAME:-${PREFIX}-ota-user-lambda-role}"
export IOT_RULE_ROLE_NAME="${IOT_RULE_ROLE_NAME:-${PREFIX}-ota-iot-rule-role}"

# DynamoDB table names
export PACKAGES_TABLE="${PACKAGES_TABLE:-${PREFIX}_ota_packages}"
export OTA_JOBS_TABLE="${OTA_JOBS_TABLE:-${PREFIX}_ota_jobs}"
export COMPAT_TABLE="${COMPAT_TABLE:-${PREFIX}_ota_compatibility}"
export DEPLOYMENTS_TABLE="${DEPLOYMENTS_TABLE:-${PREFIX}_ota_deployments}"
export CONSENTS_TABLE="${CONSENTS_TABLE:-${PREFIX}_ota_user_consents}"
export BETA_USERS_TABLE="${BETA_USERS_TABLE:-${PREFIX}_ota_beta_users}"

# API Gateway
export API_GATEWAY_ID API_GATEWAY_STAGE
export ADMIN_COGNITO_AUTHORIZER_ID USER_COGNITO_AUTHORIZER_ID

# Cognito
export USER_COGNITO_POOL_ID DEVICE_DATA_TABLE

# Notifications
export ALERT_EMAIL SES_SENDER_EMAIL

# Admin UI
export ADMIN_UI_BUCKET="${ADMIN_UI_BUCKET:-}"
export ADMIN_UI_API_BASE="${ADMIN_UI_API_BASE:-https://${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${API_GATEWAY_STAGE}/api/v1}"
export ADMIN_UI_COGNITO_URL="${ADMIN_UI_COGNITO_URL:-https://cognito-idp.${REGION}.amazonaws.com/}"
export ADMIN_UI_COGNITO_CLIENT="${ADMIN_UI_COGNITO_CLIENT:-}"
export LOGO_FILE="${LOGO_FILE:-}"
export BRAND_NAME="${BRAND_NAME:-OTA Admin}"
export ADMIN_UI_REPO_PATH="${ADMIN_UI_REPO_PATH:-}"

# IoT groups
export IOT_ROOT_GROUP="${IOT_ROOT_GROUP:-DIGILUX}"
export IOT_PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-PRODUCTION}"
export IOT_GATEWAYS_GROUP="${IOT_GATEWAYS_GROUP:-GATEWAYS}"
export IOT_TOUCH_PANELS_GROUP="${IOT_TOUCH_PANELS_GROUP:-TOUCH-PANELS}"
export IOT_GATEWAY_MODELS="${IOT_GATEWAY_MODELS:-DGW-100,DGW-200}"
export IOT_TOUCH_PANEL_MODELS="${IOT_TOUCH_PANEL_MODELS:-TP-100,TP-200}"

# CloudFront
export CLOUDFRONT_DOMAIN="${CLOUDFRONT_DOMAIN:-}"
export CLOUDFRONT_KEY_PAIR_ID="${CLOUDFRONT_KEY_PAIR_ID:-}"
export CLOUDFRONT_KEY_SECRET_NAME="${CLOUDFRONT_KEY_SECRET_NAME:-${PREFIX}-ota-cloudfront-key}"

# Presign tiers
export PRESIGN_EXPIRY_TIER1_MAX_MB="${PRESIGN_EXPIRY_TIER1_MAX_MB:-50}"
export PRESIGN_EXPIRY_TIER1_SEC="${PRESIGN_EXPIRY_TIER1_SEC:-3600}"
export PRESIGN_EXPIRY_TIER2_MAX_MB="${PRESIGN_EXPIRY_TIER2_MAX_MB:-200}"
export PRESIGN_EXPIRY_TIER2_SEC="${PRESIGN_EXPIRY_TIER2_SEC:-21600}"
export PRESIGN_EXPIRY_TIER3_MAX_MB="${PRESIGN_EXPIRY_TIER3_MAX_MB:-500}"
export PRESIGN_EXPIRY_TIER3_SEC="${PRESIGN_EXPIRY_TIER3_SEC:-86400}"
export PRESIGN_EXPIRY_TIER4_SEC="${PRESIGN_EXPIRY_TIER4_SEC:-172800}"

# Misc
export CANARY_MAX="${CANARY_MAX:-5}"
export IOT_JOB_TIMEOUT_MINUTES="${IOT_JOB_TIMEOUT_MINUTES:-1440}"

# ── Print summary ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  OTA Zero-Touch Deployment"
echo "════════════════════════════════════════════════════════════════"
echo "  Prefix         : $PREFIX"
echo "  Region         : $REGION"
echo "  Account        : $ACCOUNT_ID"
echo "  API Gateway    : $API_GATEWAY_ID  (stage: $API_GATEWAY_STAGE)"
echo "  Artifact bucket: $ARTIFACT_BUCKET"
echo "  Alert email    : $ALERT_EMAIL"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ── UI-only shortcut ──────────────────────────────────────────────────────────
if [[ "$UI_ONLY" == "true" ]]; then
  bash "$DIR/deploy_admin_ui.sh"
  exit 0
fi

# ── Preflight validation ──────────────────────────────────────────────────────
# Runs before any phase touches AWS. Exits if any check fails.
echo "==> Running preflight checks..."
DEPLOY_CONFIG="$CONFIG_FILE" bash "$DIR/preflight.sh"
echo ""

# ── Log file setup ────────────────────────────────────────────────────────────
DEPLOY_LOG="/tmp/${PREFIX}_ota_deploy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$DEPLOY_LOG") 2>&1
echo "Deploy log: $DEPLOY_LOG"
echo ""

# ── Phase runner ──────────────────────────────────────────────────────────────
PHASES=(
  "01_s3.sh"
  "02_secrets.sh"
  "03_iot_setup.sh"
  "04_dynamodb.sh"
  "05_iam_roles.sh"
  "07_deploy_lambdas.sh"
  "08_iot_rules.sh"
  "09_api_gateway.sh"
  "10_cloudwatch.sh"
  "11_s3_events.sh"
  "12_production_hardening.sh"
)

PHASE_TIMES=()

run_phase() {
  local script="$1"
  local t_start t_end elapsed
  t_start=$(date +%s)
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo "  $(date '+%H:%M:%S')  Phase: $script"
  echo "════════════════════════════════════════════════════════════════"
  chmod +x "$DIR/$script"
  bash "$DIR/$script"
  t_end=$(date +%s)
  elapsed=$(( t_end - t_start ))
  PHASE_TIMES+=("${script}: ${elapsed}s")
  echo "  $(date '+%H:%M:%S')  $script complete (${elapsed}s)"
  echo ""

  # IAM changes take up to 30s to propagate globally.
  # Without this wait, Lambda creation immediately after role creation
  # fails with "cannot be assumed" or "role does not exist" errors.
  if [[ "$script" == "05_iam_roles.sh" ]]; then
    echo "  Waiting 20s for IAM role propagation before deploying Lambdas..."
    sleep 20
  fi
}

STARTED=false
for PHASE in "${PHASES[@]}"; do
  # --phase: run only this one phase
  if [[ -n "$ONLY_PHASE" ]]; then
    [[ "$PHASE" == ${ONLY_PHASE}* ]] && run_phase "$PHASE"
    continue
  fi

  # --from: skip phases before the given prefix
  if [[ -n "$FROM_PHASE" ]]; then
    [[ "$PHASE" == ${FROM_PHASE}* ]] && STARTED=true
    [[ "$STARTED" == "false" ]] && continue
  fi

  run_phase "$PHASE"
done

# ── Admin UI ──────────────────────────────────────────────────────────────────
if [[ -n "$ADMIN_UI_BUCKET" ]]; then
  echo "────────────────────────────────────────────────────────────────"
  echo "  Phase: deploy_admin_ui.sh"
  echo "────────────────────────────────────────────────────────────────"
  bash "$DIR/deploy_admin_ui.sh"
  echo ""
fi

# ── Post-deploy instructions ──────────────────────────────────────────────────
SIGNING_KEY_OUTPUT=$(aws secretsmanager get-secret-value \
  --secret-id "$SIGNING_SECRET" \
  --query SecretString --output text \
  --region "$REGION" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('publicKey','(not yet generated)'))" 2>/dev/null \
  || echo "(run phase 02 first)")

echo "════════════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo ""
echo "  Phase timings:"
for T in "${PHASE_TIMES[@]:-}"; do echo "    $T"; done
echo ""
echo "  Full log: $DEPLOY_LOG"
echo ""
echo "  Base API URL:"
echo "    https://${API_GATEWAY_ID}.execute-api.${REGION}.amazonaws.com/${API_GATEWAY_STAGE}"
echo ""
echo "  OTA signing public key (embed in each controller at"
echo "  /etc/digilux/ota-signing.pub):"
echo ""
echo "  $SIGNING_KEY_OUTPUT"
echo ""
echo "  Next steps:"
echo "    1. Confirm SNS alert subscription in $ALERT_EMAIL inbox."
echo "    2. Distribute the public key to controllers."
echo "    3. Deploy the OTA agent to controllers."
echo "    4. Open the admin UI and upload a test package."
echo "════════════════════════════════════════════════════════════════"
