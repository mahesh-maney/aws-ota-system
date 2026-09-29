#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# preflight.sh — Validate deploy.config against live AWS before any phase runs.
#
# Checks EVERY value in deploy.config points to a real, accessible resource.
# Makes zero changes to AWS.  Exits 0 if everything is OK, 1 if anything fails.
#
# Usage:
#   ./preflight.sh                     # uses deploy.config in same directory
#   ./preflight.sh --config foo.config
#   ./deploy.sh  (calls this automatically before any phase)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_FILE="${DEPLOY_CONFIG:-${DIR}/deploy.config}"
[[ "$#" -ge 2 && "$1" == "--config" ]] && CONFIG_FILE="$2"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: $CONFIG_FILE not found.  Copy deploy.config.template → deploy.config."
  exit 1
fi
set -a; source "$CONFIG_FILE"; set +a

ACCOUNT_ID="${ACCOUNT_ID:-}"

# ── Colour helpers ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  OK='\033[0;32m[  OK  ]\033[0m'
  FAIL='\033[0;31m[ FAIL ]\033[0m'
  WARN='\033[1;33m[ WARN ]\033[0m'
  SKIP='\033[0;34m[ SKIP ]\033[0m'
else
  OK='[  OK  ]'; FAIL='[ FAIL ]'; WARN='[ WARN ]'; SKIP='[ SKIP ]'
fi

ERRORS=0
WARNINGS=0

pass()  { echo -e "  ${OK}  $*"; }
fail()  { echo -e "  ${FAIL}  $*"; (( ERRORS++ )) || true; }
warn()  { echo -e "  ${WARN}  $*"; (( WARNINGS++ )) || true; }
skip()  { echo -e "  ${SKIP}  $*"; }

section() { echo ""; echo "── $* ──────────────────────────────────────────────────────"; }

# ── 1. Local tools ────────────────────────────────────────────────────────────
section "Local prerequisites"

for CMD in aws openssl python3 pip zip; do
  if command -v "$CMD" &>/dev/null; then
    pass "$CMD  ($(command -v "$CMD"))"
  else
    fail "$CMD not found — install it before running deploy.sh"
  fi
done

# npm only required if ADMIN_UI_BUCKET is set
if [[ -n "${ADMIN_UI_BUCKET:-}" ]]; then
  if command -v npm &>/dev/null; then
    pass "npm  ($(npm --version))"
  else
    fail "npm not found — required for admin UI build (ADMIN_UI_BUCKET is set)"
  fi
else
  skip "npm — not needed (ADMIN_UI_BUCKET is empty)"
fi

# ── 2. Deploy config values ───────────────────────────────────────────────────
section "deploy.config values"

check_set() {
  local VAR="$1" LABEL="$2"
  local VAL="${!VAR:-}"
  if [[ -z "$VAL" || "$VAL" == YOUR_* ]]; then
    fail "$LABEL ($VAR) is not set"
  else
    pass "$LABEL = $VAL"
  fi
}

check_set PREFIX              "Prefix"
check_set REGION              "Region"
check_set API_GATEWAY_ID      "API Gateway ID"
check_set API_GATEWAY_STAGE   "API Gateway stage"
check_set ADMIN_COGNITO_AUTHORIZER_ID "Admin Cognito authorizer"
check_set USER_COGNITO_AUTHORIZER_ID  "User Cognito authorizer"
check_set USER_COGNITO_POOL_ID        "User Cognito pool"
check_set DEVICE_DATA_TABLE   "Device data table"
check_set ALERT_EMAIL         "Alert email"
check_set SES_SENDER_EMAIL    "SES sender email"

# PREFIX must be lowercase alphanumeric + hyphens only
if [[ "${PREFIX:-}" =~ ^[a-z0-9-]+$ ]]; then
  pass "PREFIX format is valid"
else
  fail "PREFIX '${PREFIX:-}' contains invalid characters — use only lowercase letters, numbers, hyphens"
fi

# ── 3. AWS credentials ────────────────────────────────────────────────────────
section "AWS credentials"

CALLER_JSON=$(aws sts get-caller-identity --output json 2>&1) || {
  fail "AWS credentials not configured or expired: $CALLER_JSON"
  echo ""
  echo "Run 'aws configure' or set AWS_PROFILE before deploying."
  exit 1
}
DETECTED_ACCOUNT=$(echo "$CALLER_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
CALLER_ARN=$(echo "$CALLER_JSON"       | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
pass "AWS identity: $CALLER_ARN"
pass "Account     : $DETECTED_ACCOUNT"

if [[ -n "$ACCOUNT_ID" && "$ACCOUNT_ID" != "$DETECTED_ACCOUNT" ]]; then
  fail "ACCOUNT_ID in config ($ACCOUNT_ID) ≠ logged-in account ($DETECTED_ACCOUNT)"
fi

# ── 4. API Gateway ────────────────────────────────────────────────────────────
section "API Gateway"

if aws apigateway get-rest-api \
     --rest-api-id "${API_GATEWAY_ID}" \
     --region "${REGION}" 2>/dev/null | grep -q '"id"'; then
  API_NAME=$(aws apigateway get-rest-api \
    --rest-api-id "${API_GATEWAY_ID}" \
    --region "${REGION}" \
    --query 'name' --output text 2>/dev/null)
  pass "API Gateway $API_GATEWAY_ID exists  (name: $API_NAME)"
else
  fail "API Gateway ID '$API_GATEWAY_ID' not found in region $REGION — check API_GATEWAY_ID"
fi

# Check /api/v1 resource exists
API_V1=$(aws apigateway get-resources \
  --rest-api-id "$API_GATEWAY_ID" --region "$REGION" \
  --query "items[?path=='/api/v1'].id" --output text 2>/dev/null || true)
if [[ -n "$API_V1" ]]; then
  pass "/api/v1 resource exists  (ID: $API_V1)"
else
  fail "/api/v1 path does not exist in API $API_GATEWAY_ID — OTA routes need a /api/v1 parent"
fi

# Check admin authorizer
if aws apigateway get-authorizer \
     --rest-api-id "$API_GATEWAY_ID" \
     --authorizer-id "$ADMIN_COGNITO_AUTHORIZER_ID" \
     --region "$REGION" 2>/dev/null | grep -q '"id"'; then
  AUTH_NAME=$(aws apigateway get-authorizer \
    --rest-api-id "$API_GATEWAY_ID" \
    --authorizer-id "$ADMIN_COGNITO_AUTHORIZER_ID" \
    --region "$REGION" \
    --query 'name' --output text 2>/dev/null)
  pass "Admin authorizer $ADMIN_COGNITO_AUTHORIZER_ID exists  (name: $AUTH_NAME)"
else
  fail "Admin authorizer '$ADMIN_COGNITO_AUTHORIZER_ID' not found — check ADMIN_COGNITO_AUTHORIZER_ID"
fi

# Check user authorizer
if aws apigateway get-authorizer \
     --rest-api-id "$API_GATEWAY_ID" \
     --authorizer-id "$USER_COGNITO_AUTHORIZER_ID" \
     --region "$REGION" 2>/dev/null | grep -q '"id"'; then
  AUTH_NAME=$(aws apigateway get-authorizer \
    --rest-api-id "$API_GATEWAY_ID" \
    --authorizer-id "$USER_COGNITO_AUTHORIZER_ID" \
    --region "$REGION" \
    --query 'name' --output text 2>/dev/null)
  pass "User authorizer $USER_COGNITO_AUTHORIZER_ID exists  (name: $AUTH_NAME)"
else
  fail "User authorizer '$USER_COGNITO_AUTHORIZER_ID' not found — check USER_COGNITO_AUTHORIZER_ID"
fi

# ── 5. Cognito ────────────────────────────────────────────────────────────────
section "Cognito"

if aws cognito-idp describe-user-pool \
     --user-pool-id "$USER_COGNITO_POOL_ID" \
     --region "$REGION" 2>/dev/null | grep -q '"Id"'; then
  POOL_NAME=$(aws cognito-idp describe-user-pool \
    --user-pool-id "$USER_COGNITO_POOL_ID" \
    --region "$REGION" \
    --query 'UserPool.Name' --output text 2>/dev/null)
  pass "User pool $USER_COGNITO_POOL_ID exists  (name: $POOL_NAME)"
else
  fail "Cognito user pool '$USER_COGNITO_POOL_ID' not found — check USER_COGNITO_POOL_ID"
fi

# ── 6. DynamoDB — device data table ──────────────────────────────────────────
section "DynamoDB (existing tables)"

TABLE_DESC=$(aws dynamodb describe-table \
  --table-name "$DEVICE_DATA_TABLE" \
  --region "$REGION" 2>/dev/null || true)
if [[ -n "$TABLE_DESC" ]]; then
  TABLE_STATUS=$(echo "$TABLE_DESC" | python3 -c \
    "import sys,json; print(json.load(sys.stdin)['Table']['TableStatus'])" 2>/dev/null)
  pass "$DEVICE_DATA_TABLE exists  (status: $TABLE_STATUS)"

  # Verify userId-index GSI exists
  GSI=$(echo "$TABLE_DESC" | python3 -c \
    "import sys,json
t = json.load(sys.stdin)['Table']
gsis = [g['IndexName'] for g in t.get('GlobalSecondaryIndexes',[])]
print('userId-index' in gsis)" 2>/dev/null)
  if [[ "$GSI" == "True" ]]; then
    pass "$DEVICE_DATA_TABLE has userId-index GSI"
  else
    fail "$DEVICE_DATA_TABLE is missing userId-index GSI — check_updates Lambda won't work"
  fi
else
  fail "Device data table '$DEVICE_DATA_TABLE' not found — check DEVICE_DATA_TABLE"
fi

# ── 7. SES ────────────────────────────────────────────────────────────────────
section "SES (email notifications)"

SES_STATUS=$(aws ses get-identity-verification-attributes \
  --identities "$SES_SENDER_EMAIL" \
  --region "$REGION" \
  --query "VerificationAttributes.\"${SES_SENDER_EMAIL}\".VerificationStatus" \
  --output text 2>/dev/null || echo "UNKNOWN")

if [[ "$SES_STATUS" == "Success" ]]; then
  pass "SES sender $SES_SENDER_EMAIL is verified"
elif [[ "$SES_STATUS" == "Pending" ]]; then
  warn "SES sender $SES_SENDER_EMAIL verification is PENDING — confirm the email before deploying"
else
  warn "SES sender $SES_SENDER_EMAIL verification status: $SES_STATUS"
  warn "  Consent-decline emails will fail until this address is verified in SES."
  warn "  Run: aws ses verify-email-identity --email-address $SES_SENDER_EMAIL --region $REGION"
fi

# ── 8. Lambda source directories ──────────────────────────────────────────────
section "Lambda source code"

LAMBDA_DIR="$(cd "$DIR/06_lambdas" && pwd)"
REQUIRED_DIRS=(
  digilux_ota_upload_url
  digilux_ota_package_activate
  digilux_ota_artifact_processor
  digilux_ota_package_register
  digilux_ota_compatibility_check
  digilux_ota_job_create
  digilux_ota_status_handler
  digilux_ota_device_register
  digilux_ota_user_check_updates
  digilux_ota_user_consent
  digilux_ota_user_update_status
  digilux_ota_user_get_download_link
)

for D in "${REQUIRED_DIRS[@]}"; do
  SRC="$LAMBDA_DIR/$D/lambda_function.py"
  if [[ -f "$SRC" ]]; then
    pass "$D/lambda_function.py"
  else
    fail "$SRC not found — Lambda source missing"
  fi
done

# ── 9. Admin UI (only if ADMIN_UI_BUCKET is set) ──────────────────────────────
section "Admin UI"

if [[ -z "${ADMIN_UI_BUCKET:-}" ]]; then
  skip "ADMIN_UI_BUCKET not set — skipping UI checks"
else
  # S3 bucket
  if aws s3api head-bucket --bucket "$ADMIN_UI_BUCKET" --region "$REGION" 2>/dev/null; then
    pass "Admin UI bucket s3://$ADMIN_UI_BUCKET exists"
  else
    fail "Admin UI bucket '$ADMIN_UI_BUCKET' not found or not accessible"
  fi

  # Cognito client
  if [[ -z "${ADMIN_UI_COGNITO_CLIENT:-}" || "${ADMIN_UI_COGNITO_CLIENT}" == YOUR_* ]]; then
    fail "ADMIN_UI_COGNITO_CLIENT is not set — needed to build admin UI"
  else
    pass "ADMIN_UI_COGNITO_CLIENT = $ADMIN_UI_COGNITO_CLIENT"
  fi

  # Find admin UI repo
  REPO_CANDIDATES=(
    "${ADMIN_UI_REPO_PATH:-}"
    "$DIR/../../admin-web-interface"
    "$DIR/../../../admin-web-interface"
  )
  FOUND_REPO=""
  for C in "${REPO_CANDIDATES[@]}"; do
    [[ -z "$C" ]] && continue
    [[ -f "$C/package.json" ]] && { FOUND_REPO="$(cd "$C" && pwd)"; break; }
  done
  if [[ -n "$FOUND_REPO" ]]; then
    pass "Admin UI repo found: $FOUND_REPO"
  else
    fail "Admin UI repo not found — set ADMIN_UI_REPO_PATH in deploy.config"
  fi

  # Logo file
  if [[ -n "${LOGO_FILE:-}" ]]; then
    if [[ -f "$LOGO_FILE" ]]; then
      pass "Logo file: $LOGO_FILE"
    else
      fail "Logo file not found: $LOGO_FILE — check LOGO_FILE in deploy.config"
    fi
  else
    warn "LOGO_FILE not set — default placeholder logo will be used"
  fi
fi

# ── 10. IAM permissions spot-check ───────────────────────────────────────────
section "IAM permissions (spot check)"

# Test a representative set of actions using simulate-principal-policy
# This catches "denied by SCP" or missing policies on Honeywell's account.
CALLER_ARN_CLEAN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)
DETECTED_ACCOUNT_CLEAN=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)

check_permission() {
  local ACTION="$1" RESOURCE="$2"
  local RESULT
  RESULT=$(aws iam simulate-principal-policy \
    --policy-source-arn "$CALLER_ARN_CLEAN" \
    --action-names "$ACTION" \
    --resource-arns "$RESOURCE" \
    --region "$REGION" \
    --query 'EvaluationResults[0].EvalDecision' \
    --output text 2>/dev/null || echo "UNKNOWN")
  if [[ "$RESULT" == "allowed" ]]; then
    pass "$ACTION"
  elif [[ "$RESULT" == "UNKNOWN" ]]; then
    warn "$ACTION — could not simulate (check manually)"
  else
    fail "$ACTION — DENIED.  Your IAM user/role lacks this permission."
  fi
}

check_permission "dynamodb:CreateTable"       "arn:aws:dynamodb:${REGION}:${DETECTED_ACCOUNT_CLEAN}:table/*"
check_permission "lambda:CreateFunction"      "arn:aws:lambda:${REGION}:${DETECTED_ACCOUNT_CLEAN}:function:*"
check_permission "lambda:UpdateFunctionCode"  "arn:aws:lambda:${REGION}:${DETECTED_ACCOUNT_CLEAN}:function:*"
check_permission "iam:CreateRole"             "arn:aws:iam::${DETECTED_ACCOUNT_CLEAN}:role/*"
check_permission "iam:PutRolePolicy"          "arn:aws:iam::${DETECTED_ACCOUNT_CLEAN}:role/*"
check_permission "apigateway:PUT"             "arn:aws:apigateway:${REGION}::/restapis/*"
check_permission "iot:CreateThingGroup"       "*"
check_permission "s3:CreateBucket"            "arn:aws:s3:::*"
check_permission "secretsmanager:CreateSecret" "arn:aws:secretsmanager:${REGION}:${DETECTED_ACCOUNT_CLEAN}:secret:*"
check_permission "sns:CreateTopic"            "arn:aws:sns:${REGION}:${DETECTED_ACCOUNT_CLEAN}:*"
check_permission "cloudwatch:PutMetricAlarm"  "*"
check_permission "logs:CreateLogGroup"        "*"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
if [[ $ERRORS -gt 0 ]]; then
  echo -e "  \033[0;31mPREFLIGHT FAILED — $ERRORS error(s), $WARNINGS warning(s)\033[0m"
  echo ""
  echo "  Fix all FAIL items above before running ./deploy.sh"
  echo "════════════════════════════════════════════════════════════════"
  exit 1
elif [[ $WARNINGS -gt 0 ]]; then
  echo -e "  \033[1;33mPREFLIGHT PASSED with $WARNINGS warning(s)\033[0m"
  echo ""
  echo "  Review WARN items above.  Deploy may proceed but some features"
  echo "  (e.g. SES email notifications) may not work until resolved."
  echo "════════════════════════════════════════════════════════════════"
  exit 0
else
  echo -e "  \033[0;32mPREFLIGHT PASSED — all checks OK\033[0m"
  echo "  Safe to run: ./deploy.sh"
  echo "════════════════════════════════════════════════════════════════"
  exit 0
fi
