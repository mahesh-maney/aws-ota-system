#!/bin/bash
# Phase 9 — API Gateway routes for all OTA endpoints (admin + user)
# Safe to re-run — all resource/method creation is idempotent.
set -euo pipefail
export AWS_PAGER="" PAGER=cat
source "$(dirname "$0")/_lib.sh"

log_section "Phase 9: API Gateway Routes"

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

API_ID="${API_GATEWAY_ID:?API_GATEWAY_ID not set in deploy.config}"
STAGE="${API_GATEWAY_STAGE:-smarthome}"
ADMIN_AUTH="${ADMIN_COGNITO_AUTHORIZER_ID:?ADMIN_COGNITO_AUTHORIZER_ID not set}"
USER_AUTH="${USER_COGNITO_AUTHORIZER_ID:?USER_COGNITO_AUTHORIZER_ID not set}"

log_info "API Gateway ID  : $API_ID"
log_info "Stage           : $STAGE"
log_info "Admin authorizer: $ADMIN_AUTH"
log_info "User authorizer : $USER_AUTH"
log_info "Region          : $REGION"

# ── Helpers ───────────────────────────────────────────────────────────────────

get_or_create_resource() {
  local PARENT_ID="$1" PATH_PART="$2"
  local EXISTING
  EXISTING=$(aws apigateway get-resources \
    --rest-api-id "$API_ID" --region "$REGION" \
    --query "items[?parentId=='${PARENT_ID}' && pathPart=='${PATH_PART}'].id" \
    --output text 2>/dev/null)
  if [[ -n "$EXISTING" ]]; then
    echo "$EXISTING"
  else
    log_info "    Creating resource: /${PATH_PART}"
    aws apigateway create-resource \
      --rest-api-id "$API_ID" \
      --parent-id "$PARENT_ID" \
      --path-part "$PATH_PART" \
      --region "$REGION" \
      --query "id" --output text
  fi
}

# Add a Lambda-proxy method.  Skip if method already exists (idempotent).
add_method() {
  local RESOURCE_ID="$1" HTTP_METHOD="$2" FUNC_NAME="$3" AUTHORIZER_ID="$4"

  # Check if method already exists — use if/then to avoid set -e trap on the check
  local EXISTS
  if aws apigateway get-method \
       --rest-api-id "$API_ID" \
       --resource-id "$RESOURCE_ID" \
       --http-method "$HTTP_METHOD" \
       --region "$REGION" 2>/dev/null | grep -q '"httpMethod"'; then
    log_skip "    $HTTP_METHOD already exists on $RESOURCE_ID"
    return 0
  fi

  local LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNC_NAME}"

  aws apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --authorization-type "COGNITO_USER_POOLS" \
    --authorizer-id "$AUTHORIZER_ID" \
    --region "$REGION" > /dev/null

  aws apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
    --region "$REGION" > /dev/null

  # Lambda invoke permission — remove first (idempotent), then add
  local STMT_ID="${FUNC_NAME}-apigw-${RESOURCE_ID}-${HTTP_METHOD}"
  aws lambda remove-permission \
    --function-name "$FUNC_NAME" \
    --statement-id "$STMT_ID" \
    --region "$REGION" 2>/dev/null || true
  aws lambda add-permission \
    --function-name "$FUNC_NAME" \
    --statement-id "$STMT_ID" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/*" \
    --region "$REGION" > /dev/null

  log_ok "    $HTTP_METHOD → $FUNC_NAME"
}

# Add OPTIONS (CORS preflight).  Skip if already exists.
# NOTE: uses if/then (NOT && return) to avoid set -e trap on the existence check.
add_cors() {
  local RESOURCE_ID="$1"

  # Idempotency check — always use if/then, never bare '&&' with set -euo pipefail
  if aws apigateway get-method \
       --rest-api-id "$API_ID" \
       --resource-id "$RESOURCE_ID" \
       --http-method OPTIONS \
       --region "$REGION" 2>/dev/null | grep -q '"httpMethod"'; then
    log_skip "    OPTIONS already exists on $RESOURCE_ID"
    return 0
  fi

  aws apigateway put-method \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --authorization-type NONE \
    --region "$REGION" > /dev/null

  aws apigateway put-integration \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --type MOCK \
    --request-templates '{"application/json":"{\"statusCode\":200}"}' \
    --region "$REGION" > /dev/null

  aws apigateway put-method-response \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{
      "method.response.header.Access-Control-Allow-Headers": false,
      "method.response.header.Access-Control-Allow-Methods": false,
      "method.response.header.Access-Control-Allow-Origin": false
    }' --region "$REGION" 2>/dev/null || true

  aws apigateway put-integration-response \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{
      "method.response.header.Access-Control-Allow-Headers": "'"'"'Content-Type,Authorization'"'"'",
      "method.response.header.Access-Control-Allow-Methods": "'"'"'GET,POST,PATCH,OPTIONS'"'"'",
      "method.response.header.Access-Control-Allow-Origin":  "'"'"'*'"'"'"
    }' --region "$REGION" 2>/dev/null || true

  log_ok "    OPTIONS (CORS) added to $RESOURCE_ID"
}

# Lambda function names (PREFIX-aware)
U_UPLOAD="${PREFIX}_ota_upload_url"
U_ACTIVATE="${PREFIX}_ota_package_activate"
U_JOB="${PREFIX}_ota_job_create"
U_CHECK="${PREFIX}_ota_user_check_updates"
U_CONSENT="${PREFIX}_ota_user_consent"
U_STATUS="${PREFIX}_ota_user_update_status"
U_DOWNLOAD="${PREFIX}_ota_user_get_download_link"

# ── Resolve /api/v1 root ──────────────────────────────────────────────────────
log_step "Resolving /api/v1 resource"
API_ROOT=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query "items[?path=='/api/v1'].id" --output text)

if [[ -z "$API_ROOT" ]]; then
  log_error "/api/v1 not found in API Gateway $API_ID"
  log_error "The base /api/v1 path must already exist before OTA routes can be added."
  log_error "Check API_GATEWAY_ID in deploy.config."
  exit 1
fi
log_ok "/api/v1 resource ID: $API_ROOT"

# ── /api/v1/ota ───────────────────────────────────────────────────────────────
log_step "/api/v1/ota"
OTA=$(get_or_create_resource "$API_ROOT" "ota")

# ── /api/v1/ota/packages ──────────────────────────────────────────────────────
log_step "/api/v1/ota/packages"
PKGS=$(get_or_create_resource "$OTA" "packages")
add_method "$PKGS" "GET" "$U_UPLOAD" "$ADMIN_AUTH"
add_cors   "$PKGS"

log_step "/api/v1/ota/packages/upload-artefact"
UPLOAD=$(get_or_create_resource "$PKGS" "upload-artefact")
add_method "$UPLOAD" "POST" "$U_UPLOAD" "$ADMIN_AUTH"
add_cors   "$UPLOAD"

log_step "/api/v1/ota/packages/{packageName}/{version}/activate"
PKG_NAME=$(get_or_create_resource "$PKGS"     "{packageName}")
PKG_VER=$(get_or_create_resource  "$PKG_NAME" "{version}")
PKG_ACT=$(get_or_create_resource  "$PKG_VER"  "activate")
add_method "$PKG_ACT" "PATCH" "$U_ACTIVATE" "$ADMIN_AUTH"
add_cors   "$PKG_ACT"

# ── /api/v1/ota/deployments ───────────────────────────────────────────────────
log_step "/api/v1/ota/deployments"
DEPLOY=$(get_or_create_resource "$OTA" "deployments")
add_method "$DEPLOY" "POST" "$U_JOB" "$ADMIN_AUTH"
add_method "$DEPLOY" "GET"  "$U_JOB" "$ADMIN_AUTH"
add_cors   "$DEPLOY"

log_step "/api/v1/ota/deployments/{deploymentId}"
DEPLOY_ID=$(get_or_create_resource "$DEPLOY" "{deploymentId}")
add_method "$DEPLOY_ID" "GET" "$U_JOB" "$ADMIN_AUTH"
add_cors   "$DEPLOY_ID"

log_step "/api/v1/ota/deployments/{deploymentId}/abort"
DEPLOY_ABORT=$(get_or_create_resource "$DEPLOY_ID" "abort")
add_method "$DEPLOY_ABORT" "POST" "$U_JOB" "$ADMIN_AUTH"
add_cors   "$DEPLOY_ABORT"

# ── /api/v1/ota/device/available-updates ─────────────────────────────────────
log_step "/api/v1/ota/device/available-updates"
OTA_DEVICE=$(get_or_create_resource "$OTA" "device")
AVAIL=$(get_or_create_resource "$OTA_DEVICE" "available-updates")
add_method "$AVAIL" "GET" "$U_CHECK" "$USER_AUTH"
add_cors   "$AVAIL"

# ── /api/v1/ota/my/updates/* ─────────────────────────────────────────────────
log_step "/api/v1/ota/my/updates/*"
MY=$(get_or_create_resource "$OTA" "my")
UPDATES=$(get_or_create_resource "$MY" "updates")

CONSENT_RES=$(get_or_create_resource "$UPDATES" "consent")
add_method "$CONSENT_RES" "POST" "$U_CONSENT" "$USER_AUTH"
add_cors   "$CONSENT_RES"

DOWNLOAD_RES=$(get_or_create_resource "$UPDATES" "download-link")
add_method "$DOWNLOAD_RES" "POST" "$U_DOWNLOAD" "$USER_AUTH"
add_cors   "$DOWNLOAD_RES"

JOB_PARAM=$(get_or_create_resource "$UPDATES" "{jobId}")
STATUS_RES=$(get_or_create_resource "$JOB_PARAM" "status")
add_method "$STATUS_RES" "GET" "$U_STATUS" "$USER_AUTH"
add_cors   "$STATUS_RES"

# ── CORS on auth error responses ──────────────────────────────────────────────
# Before making any change, we read and log the existing gateway response config.
# This gives you a permanent record in the deploy log of exactly what was there
# before we touched it. If you ever need to revert, the values are right here.
log_step "Gateway Responses (CORS on 401/403/5xx) — auditing existing config first"

for RESP_TYPE in DEFAULT_4XX DEFAULT_5XX UNAUTHORIZED ACCESS_DENIED EXPIRED_TOKEN; do
  EXISTING_RESP=$(aws apigateway get-gateway-response \
    --rest-api-id "$API_ID" \
    --response-type "$RESP_TYPE" \
    --region "$REGION" 2>/dev/null || echo '{}')

  python3 - << PYEOF
import json

resp = json.loads('''${EXISTING_RESP}''')
resp_type = "${RESP_TYPE}"
params = resp.get("responseParameters", {})
templates = resp.get("responseTemplates", {})
status = resp.get("statusCode", "(default)")
is_default = resp.get("defaultResponse", True)

print(f"  [{resp_type}]")

if is_default and not params and not templates:
    print(f"    Status    : AWS default (no customisation) — safe to add CORS headers")
else:
    print(f"    Status    : CUSTOMISED — existing values logged below")
    print(f"    statusCode: {status}")
    if params:
        print(f"    responseParameters (BEFORE our change):")
        for k, v in params.items():
            print(f"      {k} = {v}")
            # Flag specifically if CORS origin is already set to something
            if "Allow-Origin" in k:
                if v.strip("'") != "*":
                    print(f"      *** CHANGE FLAGGED: Allow-Origin was '{v}' → will become '*'")
                    print(f"      *** If this matters, restore with:")
                    print(f"      ***   aws apigateway put-gateway-response --rest-api-id {resp_type} \\")
                    print(f"      ***     --response-type {resp_type} --response-parameters '{{\"{k}\": \"{v}\"}}' --region \$REGION")
                else:
                    print(f"      (already '*' — no effective change)")
    if templates:
        print(f"    responseTemplates (PRESERVED — we do not touch these):")
        for k, v in templates.items():
            print(f"      {k}: {v}")
PYEOF

  # Now apply our CORS headers
  aws apigateway put-gateway-response \
    --rest-api-id "$API_ID" \
    --response-type "$RESP_TYPE" \
    --response-parameters '{
      "gatewayresponse.header.Access-Control-Allow-Origin":  "'"'"'*'"'"'",
      "gatewayresponse.header.Access-Control-Allow-Headers": "'"'"'Content-Type,Authorization'"'"'"
    }' \
    --region "$REGION" > /dev/null
  log_ok "  $RESP_TYPE — CORS headers applied"
done

# ── Deploy to stage ───────────────────────────────────────────────────────────
log_step "Deploying stage: $STAGE"
aws apigateway create-deployment \
  --rest-api-id "$API_ID" \
  --stage-name "$STAGE" \
  --description "OTA endpoints — ${PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region "$REGION" > /dev/null
log_ok "Stage deployed: $STAGE"

BASE_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE}"

log_phase_done
echo ""
log_ok "API Gateway configured.  Base URL: $BASE_URL"
echo ""
echo "  Admin endpoints:"
echo "    GET   ${BASE_URL}/api/v1/ota/packages"
echo "    POST  ${BASE_URL}/api/v1/ota/packages/upload-artefact"
echo "    PATCH ${BASE_URL}/api/v1/ota/packages/{packageName}/{version}/activate"
echo "    POST  ${BASE_URL}/api/v1/ota/deployments"
echo "    GET   ${BASE_URL}/api/v1/ota/deployments"
echo "    GET   ${BASE_URL}/api/v1/ota/deployments/{deploymentId}"
echo "    POST  ${BASE_URL}/api/v1/ota/deployments/{deploymentId}/abort"
echo "  Device/User endpoints:"
echo "    GET   ${BASE_URL}/api/v1/ota/device/available-updates"
echo "    POST  ${BASE_URL}/api/v1/ota/my/updates/consent"
echo "    POST  ${BASE_URL}/api/v1/ota/my/updates/download-link"
echo "    GET   ${BASE_URL}/api/v1/ota/my/updates/{jobId}/status"
