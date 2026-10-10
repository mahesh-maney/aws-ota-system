#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# honeywell_discovery.sh — Read-only AWS environment discovery script
#
# Purpose:
#   Discovers all existing AWS resources on the Honeywell environment that are
#   needed before running the OTA zero-touch deployment (deploy.sh).
#   This script makes NO changes — it only reads.
#
# Usage:
#   chmod +x honeywell_discovery.sh
#   ./honeywell_discovery.sh                        # auto-detect region
#   ./honeywell_discovery.sh --region us-east-1     # specify region
#   ./honeywell_discovery.sh --prefix honeywell     # filter by name prefix
#   ./honeywell_discovery.sh --output report.txt    # save report to file
#
# Prerequisites:
#   - AWS CLI installed and configured with Honeywell credentials
#   - Read-only permissions on: apigateway, cognito-idp, iot, iam,
#     dynamodb, lambda, s3, secretsmanager, ses, kms
#
# Output:
#   Prints a structured report and a ready-to-fill deploy.config block.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail          # -e removed: we handle errors per-command with || true
export AWS_PAGER="" PAGER=cat

# ── Parse arguments ───────────────────────────────────────────────────────────
REGION=""
PREFIX=""
OUTPUT_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)  REGION="$2";      shift 2 ;;
    --prefix)  PREFIX="$2";      shift 2 ;;
    --output)  OUTPUT_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD='\033[1m'; CYAN='\033[0;36m'; GREEN='\033[0;32m'
  YELLOW='\033[0;33m'; RED='\033[0;31m'; RESET='\033[0m'
else
  BOLD=''; CYAN=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi

# ── Tee to file if requested ──────────────────────────────────────────────────
if [[ -n "$OUTPUT_FILE" ]]; then
  exec > >(tee "$OUTPUT_FILE") 2>&1
  echo "Output will also be saved to: $OUTPUT_FILE"
  echo ""
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
section() { echo ""; echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════${RESET}"; echo -e "${BOLD}${CYAN}  $1${RESET}"; echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════${RESET}"; }
ok()      { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
info()    { echo -e "      $1"; }
found()   { echo -e "  ${GREEN}FOUND${RESET}  $1"; }
missing() { echo -e "  ${RED}NONE${RESET}   $1"; }

# Accumulator for deploy.config output
CONFIG_LINES=()
cfg() { CONFIG_LINES+=("$1"); }

# ─────────────────────────────────────────────────────────────────────────────
# 0. PREREQUISITES CHECK
# ─────────────────────────────────────────────────────────────────────────────
section "0. Prerequisites Check"

PREREQ_FAIL=false

check_cmd() {
  local CMD="$1" REQUIRED="$2" NOTE="${3:-}"
  if command -v "$CMD" &>/dev/null; then
    local VER
    VER=$("$CMD" --version 2>&1 | head -1 | sed 's/^[^0-9]*//' | cut -d' ' -f1 | tr -d '\n' || echo "?")
    ok "$(printf '%-10s' "$CMD")  $VER"
  else
    if [[ "$REQUIRED" == "required" ]]; then
      echo -e "  ${RED}MISSING${RESET}  $(printf '%-10s' "$CMD")  REQUIRED — $NOTE"
      PREREQ_FAIL=true
    else
      warn "$(printf '%-10s' "$CMD")  not found  (optional — $NOTE)"
    fi
  fi
}

check_cmd aws      required "install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
check_cmd python3  required "install: https://www.python.org/downloads/"
check_cmd pip      required "comes with python3 — try: python3 -m ensurepip"
check_cmd zip      required "macOS: brew install zip  |  Linux: apt install zip"
check_cmd openssl  required "macOS: brew install openssl  |  Linux: apt install openssl"
check_cmd node     optional "only needed for admin UI deploy — install: https://nodejs.org"
check_cmd npm      optional "only needed for admin UI deploy — comes with node"

if [[ "$PREREQ_FAIL" == "true" ]]; then
  echo ""
  echo -e "  ${RED}One or more required tools are missing.${RESET}"
  echo "  Install them before running deploy.sh."
  echo "  You can still continue with this discovery script."
  echo ""
fi

# ── Verify AWS credentials ────────────────────────────────────────────────────
section "AWS Identity"
if ! IDENTITY=$(aws sts get-caller-identity --output json 2>&1); then
  echo -e "${RED}ERROR: AWS credentials not configured or expired.${RESET}"
  echo "  Run: aws configure  OR  set AWS_PROFILE / AWS_ACCESS_KEY_ID"
  exit 1
fi
ACCOUNT_ID=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
CALLER_ARN=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
ok "Account  : $ACCOUNT_ID"
ok "Identity : $CALLER_ARN"
cfg "ACCOUNT_ID=$ACCOUNT_ID"

# ── Auto-detect region ────────────────────────────────────────────────────────
if [[ -z "$REGION" ]]; then
  REGION=$(aws configure get region 2>/dev/null || echo "")
  if [[ -z "$REGION" ]]; then
    REGION="${AWS_DEFAULT_REGION:-us-east-1}"
    warn "Region not set — defaulting to $REGION. Use --region to override."
  else
    ok "Region   : $REGION (from AWS config)"
  fi
else
  ok "Region   : $REGION (from --region flag)"
fi
export REGION  # FIX: export so Python subprocesses can read via os.environ
export PREFIX="${PREFIX:-}"
cfg "REGION=$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# 1. API GATEWAY
# ─────────────────────────────────────────────────────────────────────────────
section "1. API Gateway (REST APIs)"

APIS=$(aws apigateway get-rest-apis --region "$REGION" --output json 2>/dev/null || echo '{"items":[]}')
API_COUNT=$(echo "$APIS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo "0")

if [[ "$API_COUNT" -eq 0 ]]; then
  missing "No REST APIs found in $REGION"
  cfg "API_GATEWAY_ID=YOUR_API_GATEWAY_ID"
  cfg "API_GATEWAY_STAGE=YOUR_STAGE_NAME"
else
  echo "$APIS" | python3 -c "
import sys, json, subprocess, os

data  = json.load(sys.stdin)
items = data.get('items', [])
region = os.environ.get('REGION','')

for api in items:
    api_id   = api.get('id','')
    api_name = api.get('name','')
    print(f'  API: {api_name}  (id={api_id})')

    # List stages
    try:
        result = subprocess.run(
            ['aws','apigateway','get-stages','--rest-api-id',api_id,'--region',region,'--output','json'],
            capture_output=True, text=True, timeout=15
        )
        stages = json.loads(result.stdout).get('item', [])
        for s in stages:
            stage_name = s.get('stageName','')
            print(f'      Stage: {stage_name}')
    except Exception as e:
        print(f'      (could not fetch stages: {e})')

    # List existing /ota routes to detect conflicts
    try:
        result = subprocess.run(
            ['aws','apigateway','get-resources','--rest-api-id',api_id,'--region',region,'--output','json'],
            capture_output=True, text=True, timeout=20
        )
        resources = json.loads(result.stdout).get('items', [])
        ota_routes = [r.get('path','') for r in resources if 'ota' in r.get('path','').lower()]
        if ota_routes:
            print(f'      WARNING: Existing /ota routes (may conflict):')
            for r in ota_routes:
                print(f'         {r}')
        else:
            print(f'      OK: No existing /ota routes — safe to add OTA endpoints')
    except Exception as e:
        print(f'      (could not fetch resources: {e})')
    print()
" 2>/dev/null || warn "Could not enumerate API resources (check IAM permissions)"
  warn "Fill in API_GATEWAY_ID and API_GATEWAY_STAGE in deploy.config from the list above."
  cfg "API_GATEWAY_ID=# FILL FROM ABOVE"
  cfg "API_GATEWAY_STAGE=# FILL FROM ABOVE"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. COGNITO USER POOLS
# ─────────────────────────────────────────────────────────────────────────────
section "2. Cognito User Pools"

POOLS=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" --output json 2>/dev/null || echo '{"UserPools":[]}')
POOL_COUNT=$(echo "$POOLS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('UserPools',[])))" 2>/dev/null || echo "0")

if [[ "$POOL_COUNT" -eq 0 ]]; then
  missing "No Cognito User Pools found"
  cfg "ADMIN_COGNITO_AUTHORIZER_ID=YOUR_ADMIN_AUTHORIZER_ID"
  cfg "USER_COGNITO_AUTHORIZER_ID=YOUR_USER_AUTHORIZER_ID"
  cfg "USER_COGNITO_POOL_ID=YOUR_USER_POOL_ID"
else
  echo "$POOLS" | python3 -c "
import sys, json, subprocess, os
data  = json.load(sys.stdin)
pools = data.get('UserPools', [])
region = os.environ.get('REGION','')

for pool in pools:
    pool_id   = pool.get('Id','')
    pool_name = pool.get('Name','')
    print(f'  Pool: {pool_name}')
    print(f'    Pool ID : {pool_id}')

    # Get clients for this pool
    try:
        result = subprocess.run(
            ['aws','cognito-idp','list-user-pool-clients',
             '--user-pool-id', pool_id,
             '--max-results','10',
             '--region', region,
             '--output','json'],
            capture_output=True, text=True, timeout=15
        )
        clients = json.loads(result.stdout).get('UserPoolClients', [])
        for c in clients:
            cid   = c.get('ClientId','')
            cname = c.get('ClientName','')
            print(f'    Client: {cname}  (id={cid})')
    except Exception as e:
        print(f'    (could not fetch clients: {e})')
    print()
" 2>/dev/null || warn "Could not enumerate Cognito pools"

  warn "Identify which pool is ADMIN (internal staff) and which is USER (device owners)."
  warn "For each, get the Cognito Authorizer ID from API Gateway → Authorizers."
  cfg "ADMIN_COGNITO_AUTHORIZER_ID=# FILL: API Gateway → Authorizers → admin pool authorizer ID"
  cfg "USER_COGNITO_AUTHORIZER_ID=# FILL: API Gateway → Authorizers → user pool authorizer ID"
  cfg "USER_COGNITO_POOL_ID=# FILL FROM ABOVE (user-facing pool, not admin)"
  cfg "ADMIN_UI_COGNITO_CLIENT=# FILL: client ID from admin pool"
fi

# List API Gateway Cognito authorizers
echo ""
echo "  Cognito Authorizers attached to API Gateways:"
_auth_raw=$(aws apigateway get-rest-apis --region "$REGION" --output json 2>/dev/null || echo '{"items":[]}')
echo "$_auth_raw" | python3 -c "
import sys, json, subprocess, os
region = os.environ.get('REGION','')
data = json.load(sys.stdin)
for api in data.get('items',[]):
    api_id = api['id']
    try:
        result = subprocess.run(
            ['aws','apigateway','get-authorizers','--rest-api-id',api_id,'--region',region,'--output','json'],
            capture_output=True, text=True, timeout=15
        )
        auths = json.loads(result.stdout).get('items', [])
        for a in auths:
            print(f'    API={api[\"name\"]}  Authorizer={a.get(\"name\")}  id={a.get(\"id\")}  type={a.get(\"type\")}')
    except: pass
" 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# 3. IOT THING GROUPS
# ─────────────────────────────────────────────────────────────────────────────
section "3. IoT Core — Thing Groups"

GROUPS=$(aws iot list-thing-groups --region "$REGION" --output json 2>/dev/null || echo '{"thingGroups":[]}')
GROUP_COUNT=$(echo "$GROUPS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('thingGroups',[])))" 2>/dev/null || echo "0")

if [[ "$GROUP_COUNT" -eq 0 ]]; then
  missing "No IoT Thing Groups found"
  cfg "IOT_ROOT_GROUP=DIGILUX"
  cfg "IOT_PRODUCTION_GROUP=PRODUCTION"
  cfg "IOT_GATEWAYS_GROUP=GATEWAYS"
  cfg "IOT_TOUCH_PANELS_GROUP=TOUCH-PANELS"
else
  ok "Found $GROUP_COUNT Thing Group(s):"
  echo "$GROUPS" | python3 -c "
import sys, json, subprocess, os
region = os.environ.get('REGION','')
data   = json.load(sys.stdin)
groups = data.get('thingGroups', [])

for g in groups:
    name = g.get('groupName','')
    try:
        result = subprocess.run(
            ['aws','iot','describe-thing-group','--thing-group-name',name,'--region',region,'--output','json'],
            capture_output=True, text=True, timeout=15
        )
        detail = json.loads(result.stdout)
        parent = detail.get('thingGroupProperties',{}).get('parentGroupName','(root)')
        count_r = subprocess.run(
            ['aws','iot','list-things-in-thing-group','--thing-group-name',name,'--region',region,'--output','json'],
            capture_output=True, text=True, timeout=15
        )
        things = json.loads(count_r.stdout).get('things', [])
        print(f'    {name:30s}  parent={parent:20s}  devices={len(things)}')
    except Exception as e:
        print(f'    {name:30s}  (error: {e})')
" 2>/dev/null || warn "Could not enumerate IoT Thing Groups"
  warn "Set IOT_ROOT_GROUP / IOT_PRODUCTION_GROUP / IOT_GATEWAYS_GROUP in deploy.config to match hierarchy above."
  cfg "IOT_ROOT_GROUP=# FILL FROM ABOVE — top-level group name"
  cfg "IOT_PRODUCTION_GROUP=# FILL FROM ABOVE — production sub-group"
  cfg "IOT_GATEWAYS_GROUP=# FILL FROM ABOVE — gateways leaf group (devices land here)"
  cfg "IOT_TOUCH_PANELS_GROUP=# FILL FROM ABOVE — touch panels leaf group (or leave blank)"
fi

# IoT endpoint
IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION" --query endpointAddress --output text 2>/dev/null || echo "")
if [[ -n "$IOT_ENDPOINT" ]]; then
  ok "IoT endpoint: $IOT_ENDPOINT"
  cfg "IOT_ENDPOINT=$IOT_ENDPOINT"
else
  warn "Could not retrieve IoT endpoint (check iot:DescribeEndpoint permission)"
  cfg "IOT_ENDPOINT=# FILL: run: aws iot describe-endpoint --endpoint-type iot:Data-ATS --region $REGION"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. IAM PERMISSION BOUNDARIES
# ─────────────────────────────────────────────────────────────────────────────
section "4. IAM — Permission Boundaries & SCPs"

# FIX: check IAM user boundary (only if caller is a user, not a role)
if echo "$CALLER_ARN" | grep -q ":user/"; then
  _user_raw=$(aws iam get-user --output json 2>/dev/null || echo '{}')
  echo "$_user_raw" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    pb = d.get('User',{}).get('PermissionsBoundary',{})
    if pb:
        print(f'  WARNING: User has permission boundary: {pb.get(\"PermissionsBoundaryArn\")}')
    else:
        print('  OK: No permission boundary on current user')
except: pass
" 2>/dev/null || true
fi

# FIX: check assumed-role boundary only if caller is actually a role
if echo "$CALLER_ARN" | grep -q "assumed-role"; then
  ROLE_NAME=$(echo "$CALLER_ARN" | python3 -c "
import sys
p = sys.stdin.read().strip()
print(p.split('assumed-role/')[-1].split('/')[0] if 'assumed-role' in p else '')
" 2>/dev/null || echo "")
  if [[ -n "$ROLE_NAME" ]]; then
    _role_raw=$(aws iam get-role --role-name "$ROLE_NAME" --output json 2>/dev/null || echo '{}')
    echo "$_role_raw" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    pb = d.get('Role',{}).get('PermissionsBoundary',{})
    if pb:
        print(f'  WARNING: Assumed role has permission boundary: {pb.get(\"PermissionsBoundaryArn\")}')
        print('     You may not be able to create IAM roles without attaching the same boundary.')
    else:
        print('  OK: No permission boundary on assumed role')
except: pass
" 2>/dev/null || true
  fi
fi

# Check SCPs (requires org:ListPoliciesForTarget — may fail without org perms)
echo "  Checking AWS Organizations SCPs (requires org access)..."
ORG_RAW=$(aws organizations describe-organization --output json 2>/dev/null || echo '{}')
ORG_ID=$(echo "$ORG_RAW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Organization',{}).get('Id',''))" 2>/dev/null || echo "")
if [[ -n "$ORG_ID" ]]; then
  ok "Organization ID: $ORG_ID"
  ACCOUNT_POLICIES=$(aws organizations list-policies-for-target \
    --target-id "$ACCOUNT_ID" \
    --filter SERVICE_CONTROL_POLICY \
    --region us-east-1 \
    --output json 2>/dev/null || echo '{"Policies":[]}')
  SCP_COUNT=$(echo "$ACCOUNT_POLICIES" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('Policies',[])))" 2>/dev/null || echo "0")
  if [[ "$SCP_COUNT" -gt 0 ]]; then
    warn "SCPs attached to this account ($SCP_COUNT policy/policies):"
    echo "$ACCOUNT_POLICIES" | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('Policies',[]):
    print(f'    {p.get(\"Name\")}  (id={p.get(\"Id\")})')
" 2>/dev/null || true
    warn "Review SCPs before running deploy.sh — they may block IAM role creation, KMS, or IoT actions."
  else
    ok "No SCPs attached to this account"
  fi
else
  info "(No Organizations access — SCPs could not be checked. Ask Honeywell's AWS admin.)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5. EXISTING DYNAMODB TABLES (conflict check)
# ─────────────────────────────────────────────────────────────────────────────
section "5. DynamoDB — Existing Tables (conflict check)"

OTA_KEYWORDS=("_ota_packages" "_ota_jobs" "_ota_deployments" "_ota_user_consents" "_ota_beta_users" "_ota_compatibility")
DEVICE_KEYWORDS=("_device_data" "_device_inventory")

# FIX: use || echo '{"TableNames":[]}' so python3 never gets empty input
_ddb_raw=$(aws dynamodb list-tables --region "$REGION" --output json 2>/dev/null || echo '{"TableNames":[]}')
ALL_TABLES=$(echo "$_ddb_raw" | python3 -c "import sys,json; [print(t) for t in json.load(sys.stdin).get('TableNames',[])]" 2>/dev/null || echo "")

echo "  Checking for existing device data table..."
if [[ -n "$ALL_TABLES" ]]; then
  while IFS= read -r T; do
    for DT in "${DEVICE_KEYWORDS[@]}"; do
      if [[ "$T" == *"$DT"* ]]; then
        found "$T  <- device data table (use this in deploy.config)"
        cfg "DEVICE_DATA_TABLE=$T"
      fi
    done
  done <<< "$ALL_TABLES"
fi

echo ""
echo "  Checking for existing OTA tables (would be skipped if found)..."
CONFLICT=false
if [[ -n "$ALL_TABLES" ]]; then
  while IFS= read -r T; do
    for OT in "${OTA_KEYWORDS[@]}"; do
      if [[ "$T" == *"$OT"* ]]; then
        ok "$T  (already exists — deploy.sh will skip creation)"
        CONFLICT=true
      fi
    done
  done <<< "$ALL_TABLES"
fi
if [[ "$CONFLICT" == "false" ]]; then
  info "No OTA tables found — deploy.sh will create them fresh."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6. EXISTING LAMBDA FUNCTIONS (conflict check)
# ─────────────────────────────────────────────────────────────────────────────
section "6. Lambda — Existing OTA Functions (conflict check)"

OTA_LAMBDA_KEYWORDS=(
  "ota_artifact_processor"
  "ota_package_activate"
  "ota_job_create"
  "ota_user_check_updates"
  "ota_user_consent"
  "ota_artifact_key"
  "ota_status_handler"
  "ota_package_register"
  "ota_device_register"
  "entitlement_check"
)

# FIX: || echo '{"Functions":[]}' so python3 never gets empty input
_lam_raw=$(aws lambda list-functions --region "$REGION" --output json 2>/dev/null || echo '{"Functions":[]}')
ALL_LAMBDAS=$(echo "$_lam_raw" | python3 -c "import sys,json; [print(f.get('FunctionName','')) for f in json.load(sys.stdin).get('Functions',[])]" 2>/dev/null || echo "")

FOUND_LAMBDAS=false
if [[ -n "$ALL_LAMBDAS" ]]; then
  while IFS= read -r LNAME; do
    for OL in "${OTA_LAMBDA_KEYWORDS[@]}"; do
      if [[ "$LNAME" == *"$OL"* ]]; then
        found "$LNAME  <- will be UPDATED (not created) by deploy.sh"
        FOUND_LAMBDAS=true
      fi
    done
  done <<< "$ALL_LAMBDAS"
fi
if [[ "$FOUND_LAMBDAS" == "false" ]]; then
  info "No existing OTA Lambda functions found — deploy.sh will create them."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7. S3 BUCKETS (conflict check)
# ─────────────────────────────────────────────────────────────────────────────
section "7. S3 — Existing Buckets (conflict check)"

# FIX: || echo '{"Buckets":[]}' so python3 never gets empty input
_s3_raw=$(aws s3api list-buckets --output json 2>/dev/null || echo '{"Buckets":[]}')
ALL_BUCKETS=$(echo "$_s3_raw" | python3 -c "import sys,json; [print(b.get('Name','')) for b in json.load(sys.stdin).get('Buckets',[])]" 2>/dev/null || echo "")

echo "  Checking for existing OTA-related buckets..."
OTA_BUCKET_FOUND=false
ADMIN_UI_BUCKET_FOUND=false
if [[ -n "$ALL_BUCKETS" ]]; then
  while IFS= read -r B; do
    if [[ "$B" == *"ota-artifact"* || "$B" == *"ota-artifacts"* ]]; then
      found "$B  <- artifact bucket (deploy.sh will reuse)"
      cfg "ARTIFACT_BUCKET=$B"
      OTA_BUCKET_FOUND=true
    fi
    if [[ "$B" == *"ota-admin"* || "$B" == *"admin-ui"* ]]; then
      found "$B  <- admin UI bucket"
      cfg "ADMIN_UI_BUCKET=$B"
      ADMIN_UI_BUCKET_FOUND=true
    fi
  done <<< "$ALL_BUCKETS"
fi
[[ "$OTA_BUCKET_FOUND"      == "false" ]] && info "No OTA artifact bucket found — deploy.sh will create one."
[[ "$ADMIN_UI_BUCKET_FOUND" == "false" ]] && info "No admin UI bucket found — leave ADMIN_UI_BUCKET blank to skip UI deploy."

# ─────────────────────────────────────────────────────────────────────────────
# 8. SECRETS MANAGER (conflict check)
# ─────────────────────────────────────────────────────────────────────────────
section "8. Secrets Manager — OTA Signing Key"

# FIX: || echo '{"SecretList":[]}' fallback
_sec_raw=$(aws secretsmanager list-secrets --region "$REGION" --output json 2>/dev/null || echo '{"SecretList":[]}')
SECRETS=$(echo "$_sec_raw" | python3 -c "import sys,json; [print(s.get('Name','')) for s in json.load(sys.stdin).get('SecretList',[])]" 2>/dev/null || echo "")

OTA_KEY_FOUND=false
if [[ -n "$SECRETS" ]]; then
  while IFS= read -r S; do
    if [[ "$S" == *"ota-signing"* ]]; then
      found "$S  <- signing key exists (deploy.sh phase 02 will skip generation)"
      cfg "SIGNING_SECRET=$S"
      OTA_KEY_FOUND=true
    fi
  done <<< "$SECRETS"
fi
[[ "$OTA_KEY_FOUND" == "false" ]] && info "No OTA signing key found — deploy.sh will generate a new ECDSA P-256 key."

# ─────────────────────────────────────────────────────────────────────────────
# 9. SES (email sender verification)
# ─────────────────────────────────────────────────────────────────────────────
section "9. SES — Verified Email Identities"

# FIX: || echo '{"Identities":[]}' fallback
_ses_raw=$(aws ses list-identities --region "$REGION" --output json 2>/dev/null || echo '{"Identities":[]}')
SES_IDENTITIES=$(echo "$_ses_raw" | python3 -c "import sys,json; [print(i) for i in json.load(sys.stdin).get('Identities',[])]" 2>/dev/null || echo "")

if [[ -z "$SES_IDENTITIES" ]]; then
  warn "No SES verified identities found."
  warn "The OTA system sends recall/decline emails. You need a verified sender."
  cfg "SES_SENDER_EMAIL=# FILL: a verified SES email address (e.g. noreply@honeywell.com)"
else
  ok "Verified SES identities:"
  while IFS= read -r I; do
    info "  $I"
  done <<< "$SES_IDENTITIES"
  cfg "SES_SENDER_EMAIL=# FILL FROM ABOVE"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 10. KMS KEYS
# ─────────────────────────────────────────────────────────────────────────────
section "10. KMS — Customer Managed Keys"

# FIX: || echo '{"Aliases":[]}' fallback
_kms_raw=$(aws kms list-aliases --region "$REGION" --output json 2>/dev/null || echo '{"Aliases":[]}')
KMS_KEYS=$(echo "$_kms_raw" | python3 -c "
import sys, json
aliases = json.load(sys.stdin).get('Aliases', [])
for a in aliases:
    name = a.get('AliasName','')
    if not name.startswith('alias/aws/'):  # skip AWS-managed keys
        print(f'  {name}  ->  {a.get(\"TargetKeyId\",\"\")}')
" 2>/dev/null || echo "")

if [[ -z "$KMS_KEYS" ]]; then
  info "No customer-managed KMS keys found."
  info "deploy.sh will use the default aws/secretsmanager key for Secrets Manager."
  cfg "# KMS_KEY_ID= (leave blank to use aws/secretsmanager default)"
else
  ok "Customer-managed KMS keys:"
  echo "$KMS_KEYS"
  OTA_KMS=$(echo "$KMS_KEYS" | grep -i "ota" || true)
  if [[ -n "$OTA_KMS" ]]; then
    warn "OTA-related KMS key found: $OTA_KMS"
    cfg "KMS_KEY_ID=# FILL: KMS key ID for OTA artifact encryption (from above)"
  else
    cfg "KMS_KEY_ID=# OPTIONAL: KMS key for envelope encryption (blank = auto-created)"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 11. GENERATE deploy.config BLOCK
# ─────────────────────────────────────────────────────────────────────────────
section "Generated deploy.config (fill in FILL items before running deploy.sh)"

echo ""
echo "────────────────────────────────────────────────────────────"
echo "# deploy.config — generated by honeywell_discovery.sh"
echo "# $(date)"
echo "# Account: $ACCOUNT_ID  Region: $REGION"
echo "────────────────────────────────────────────────────────────"
echo ""
echo "PREFIX=honeywell"
echo "REGION=$REGION"
echo "ACCOUNT_ID=$ACCOUNT_ID"
echo ""
echo "# API Gateway (from Section 1)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == API_GATEWAY* ]] && echo "$L"
done
echo ""
echo "# Cognito (from Section 2)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == *COGNITO* ]] && echo "$L"
done
echo ""
echo "# IoT Groups (from Section 3)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == IOT_* ]] && echo "$L"
done
echo "IOT_GATEWAY_MODELS=# FILL: comma-separated device model names e.g. DGW-100,DGW-200"
echo "IOT_TOUCH_PANEL_MODELS=# FILL: comma-separated touch panel models or leave blank"
echo ""
echo "# DynamoDB (from Section 5)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == DEVICE_DATA_TABLE* ]] && echo "$L"
done
echo ""
echo "# S3 (from Section 7)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == ARTIFACT_BUCKET* || "$L" == ADMIN_UI_BUCKET* ]] && echo "$L"
done
echo ""
echo "# Secrets (from Section 8)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == SIGNING_SECRET* ]] && echo "$L"
done
echo ""
echo "# KMS (from Section 10)"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == KMS_KEY_ID* ]] && echo "$L"
done
echo ""
echo "# Notifications"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == SES_SENDER_EMAIL* ]] && echo "$L"
done
echo "ALERT_EMAIL=# FILL: email to receive CloudWatch alarms"
echo ""
echo "# Admin UI"
for L in "${CONFIG_LINES[@]}"; do
  [[ "$L" == ADMIN_UI* ]] && echo "$L"
done
echo "ADMIN_UI_REPO_PATH=# FILL: absolute path to digilux-ota-admin-ui repo on this machine"
echo "BRAND_NAME=Honeywell OTA"
echo "LOGO_FILE=# FILL: path to Honeywell logo file (PNG/SVG) or leave blank"
echo ""
echo "# OTA job settings"
echo "IOT_JOB_TIMEOUT_MINUTES=1440"
echo "COOLING_OFF_DAYS=7"
echo "────────────────────────────────────────────────────────────"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 12. FINAL RISK SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
section "Risk Summary — Before Running deploy.sh"

echo ""
echo -e "  ${GREEN}SAFE to run as-is:${RESET}"
echo "    Phase 01 — S3 artifact bucket"
echo "    Phase 02 — ECDSA signing key"
echo "    Phase 04 — DynamoDB OTA tables"
echo "    Phase 07 — Lambda functions"
echo "    Phase 10 — CloudWatch log groups + alarms"
echo "    Phase 11 — S3 event notification"
echo ""
echo -e "  ${YELLOW}REVIEW before running:${RESET}"
echo "    Phase 03 — IoT Thing Groups (match to existing hierarchy above)"
echo "    Phase 05 — IAM roles (check Permission Boundaries + SCPs above)"
echo "    Phase 08 — IoT Rules (verify no name conflicts)"
echo "    Phase 12 — Production hardening (may tighten S3/IAM policies)"
echo ""
echo -e "  ${RED}COORDINATE with Honeywell team first:${RESET}"
echo "    Phase 09 — API Gateway routes (adds routes to existing API;"
echo "               check for /ota path conflicts printed in Section 1)"
echo ""
echo "  Recommended run order for a safe first deploy:"
echo "    ./deploy.sh --phase 01   # S3"
echo "    ./deploy.sh --phase 02   # Secrets"
echo "    ./deploy.sh --phase 04   # DynamoDB"
echo "    ./deploy.sh --phase 05   # IAM"
echo "    ./deploy.sh --phase 07   # Lambdas"
echo "    # PAUSE — verify Lambdas are healthy in AWS console"
echo "    ./deploy.sh --phase 08   # IoT Rules"
echo "    ./deploy.sh --phase 09   # API Gateway (after Honeywell confirms)"
echo "    ./deploy.sh --phase 03   # IoT Groups (after confirming hierarchy)"
echo "    ./deploy.sh --ui-only    # Admin UI (last)"
echo ""
echo -e "${BOLD}Discovery complete.${RESET}"
[[ -n "$OUTPUT_FILE" ]] && echo "Report saved to: $OUTPUT_FILE"
echo ""
