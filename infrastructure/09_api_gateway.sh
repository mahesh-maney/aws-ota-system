#!/bin/bash
# Phase 9 — API Gateway routes for OTA admin endpoints
# Adds routes to existing digilux gateway: ds6nxf8ac5 / stage: smarthome
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
API_ID="ds6nxf8ac5"
STAGE="smarthome"
AUTHORIZER_ID="ddhsbg"

UPLOAD_URL_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:digilux_ota_upload_url"
COMPAT_CHECK_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:digilux_ota_compatibility_check"
JOB_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:digilux_ota_job_create"

# ── Helpers ───────────────────────────────────────────────────────────────────

get_or_create_resource() {
  local PARENT_ID="$1"
  local PATH_PART="$2"
  local EXISTING
  EXISTING=$(aws apigateway get-resources \
    --rest-api-id "$API_ID" --region "$REGION" \
    --query "items[?parentId=='$PARENT_ID' && pathPart=='$PATH_PART'].id" \
    --output text)
  if [ -n "$EXISTING" ]; then
    echo "$EXISTING"
  else
    aws apigateway create-resource \
      --rest-api-id "$API_ID" --parent-id "$PARENT_ID" \
      --path-part "$PATH_PART" --region "$REGION" \
      --query "id" --output text
  fi
}

add_method() {
  local RESOURCE_ID="$1"
  local HTTP_METHOD="$2"
  local LAMBDA_ARN="$3"

  aws apigateway put-method \
    --rest-api-id "$API_ID" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --authorization-type "COGNITO_USER_POOLS" \
    --authorizer-id "$AUTHORIZER_ID" \
    --region "$REGION" 2>/dev/null || true

  aws apigateway put-integration \
    --rest-api-id "$API_ID" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
    --region "$REGION" > /dev/null

  # Lambda invoke permission — idempotent
  local STMT_ID="apigw-ota-${RESOURCE_ID}-${HTTP_METHOD}"
  aws lambda remove-permission \
    --function-name "$LAMBDA_ARN" --statement-id "$STMT_ID" \
    --region "$REGION" 2>/dev/null || true
  aws lambda add-permission \
    --function-name "$LAMBDA_ARN" --statement-id "$STMT_ID" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/*" \
    --region "$REGION" > /dev/null

  echo "    ${HTTP_METHOD} wired to $(echo "$LAMBDA_ARN" | awk -F: '{print $NF}')"
}

add_cors() {
  local RESOURCE_ID="$1"
  aws apigateway put-method \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --authorization-type NONE \
    --region "$REGION" 2>/dev/null || true

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
      "method.response.header.Access-Control-Allow-Methods": "'"'"'GET,POST,OPTIONS'"'"'",
      "method.response.header.Access-Control-Allow-Origin": "'"'"'*'"'"'"
    }' --region "$REGION" 2>/dev/null || true
}

# ── Find /api/v1 root ─────────────────────────────────────────────────────────
API_ROOT=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query "items[?path=='/api/v1'].id" --output text)

if [ -z "$API_ROOT" ]; then
  echo "ERROR: /api/v1 not found in API $API_ID"; exit 1
fi
echo "==> /api/v1 resource ID: $API_ROOT"

# ── /api/v1/ota ───────────────────────────────────────────────────────────────
echo ""
echo "==> /api/v1/ota"
OTA_RES=$(get_or_create_resource "$API_ROOT" "ota")

# ── /api/v1/ota/packages — GET list ──────────────────────────────────────────
echo "==> /api/v1/ota/packages"
PKGS_RES=$(get_or_create_resource "$OTA_RES" "packages")
add_method "$PKGS_RES" "GET" "$UPLOAD_URL_ARN"
add_cors   "$PKGS_RES"

# ── /api/v1/ota/packages/upload-artefact — POST get pre-signed PUT URL ─────────────
echo "==> /api/v1/ota/packages/upload-artefact"
UPLOAD_RES=$(get_or_create_resource "$PKGS_RES" "upload-artefact")
add_method "$UPLOAD_RES" "POST" "$UPLOAD_URL_ARN"
add_cors   "$UPLOAD_RES"

# ── /api/v1/ota/deployments — POST create, GET list ──────────────────────────
echo "==> /api/v1/ota/deployments"
DEPLOY_RES=$(get_or_create_resource "$OTA_RES" "deployments")
add_method "$DEPLOY_RES" "POST" "$JOB_ARN"
add_method "$DEPLOY_RES" "GET"  "$JOB_ARN"
add_cors   "$DEPLOY_RES"

# ── /api/v1/ota/deployments/{jobId} — GET status ─────────────────────────────
echo "==> /api/v1/ota/deployments/{jobId}"
DEPLOY_JOB_RES=$(get_or_create_resource "$DEPLOY_RES" "{jobId}")
add_method "$DEPLOY_JOB_RES" "GET" "$JOB_ARN"
add_cors   "$DEPLOY_JOB_RES"

# ── /api/v1/ota/deployments/{jobId}/abort — POST ─────────────────────────────
echo "==> /api/v1/ota/deployments/{jobId}/abort"
ABORT_RES=$(get_or_create_resource "$DEPLOY_JOB_RES" "abort")
add_method "$ABORT_RES" "POST" "$JOB_ARN"
add_cors   "$ABORT_RES"

# ── /api/v1/controllers/{deviceId}/updates/available — GET ───────────────────
echo "==> /api/v1/controllers/{deviceId}/updates/available"
CTRL_RES=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query "items[?pathPart=='controllers'].id" --output text)
[ -z "$CTRL_RES" ] && CTRL_RES=$(get_or_create_resource "$API_ROOT" "controllers")
CTRL_ID_RES=$(get_or_create_resource "$CTRL_RES"    "{deviceId}")
UPDATES_RES=$(get_or_create_resource "$CTRL_ID_RES" "updates")
AVAIL_RES=$(get_or_create_resource   "$UPDATES_RES" "available")
add_method "$AVAIL_RES" "GET" "$COMPAT_CHECK_ARN"
add_cors   "$AVAIL_RES"

# ── Deploy to stage ───────────────────────────────────────────────────────────
echo ""
echo "==> Deploying to stage: $STAGE"
aws apigateway create-deployment \
  --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --description "OTA endpoints v2 — cleaner upload flow" \
  --region "$REGION" > /dev/null

BASE_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE}"
echo ""
echo "API Gateway deployed."
echo "Base URL: $BASE_URL"
echo ""
echo "Endpoints:"
echo "  GET  /api/v1/ota/packages                              List all packages"
echo "  POST /api/v1/ota/packages/upload-artefact                   Get pre-signed upload URL"
echo "  POST /api/v1/ota/deployments                           Create deployment"
echo "  GET  /api/v1/ota/deployments                           List deployments"
echo "  GET  /api/v1/ota/deployments/{jobId}                   Job status + progress"
echo "  POST /api/v1/ota/deployments/{jobId}/abort             Abort deployment"
echo "  GET  /api/v1/controllers/{deviceId}/updates/available  Check device updates"
