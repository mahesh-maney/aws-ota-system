#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 13_user_ota_setup.sh
# User-initiated OTA update infrastructure
# Creates: DynamoDB consent table, IAM role, 3 Lambda functions, API GW routes
# Run after: 09_api_gateway.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
API_ID="ds6nxf8ac5"          # existing API Gateway ID
STAGE="smarthome"
AUTHORIZER_ID="fp1yfy"       # existing Cognito authorizer

# Load configurable values from ota.env
ENV_FILE="$(dirname "$0")/ota.config"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck source=infrastructure/ota.env
  source "${ENV_FILE}"
  echo "Loaded config from ${ENV_FILE}"
else
  echo "WARNING: ${ENV_FILE} not found — using Lambda defaults"
fi

LAMBDA_ROLE_NAME="digilux-ota-user-lambda-role"
CONSENTS_TABLE="digilux_ota_user_consents"

LAMBDA_NAMES=(
  "digilux_ota_user_check_updates"
  "digilux_ota_user_consent"
  "digilux_ota_user_update_status"
  "digilux_ota_user_get_download_link"
)

LAMBDA_ENV_COMMON="REGION=${REGION},ACCOUNT_ID=${ACCOUNT_ID},\
DEVICE_DATA_TABLE=digilux_device_data,\
PACKAGES_TABLE=digilux_ota_packages,\
OTA_JOBS_TABLE=digilux_ota_jobs,\
CONSENTS_TABLE=${CONSENTS_TABLE},\
ARTIFACT_BUCKET=digilux-ota-artifacts,\
RATE_LIMIT_MINUTES=5,\
DEVICE_DATA_USER_INDEX=userId-index,\
CONSENTS_USER_INDEX=userId-deviceId-index,\
CONSENTS_JOB_INDEX=jobId-index,\
PRESIGN_EXPIRY_TIER1_MAX_MB=${PRESIGN_EXPIRY_TIER1_MAX_MB:-50},\
PRESIGN_EXPIRY_TIER1_SEC=${PRESIGN_EXPIRY_TIER1_SEC:-3600},\
PRESIGN_EXPIRY_TIER2_MAX_MB=${PRESIGN_EXPIRY_TIER2_MAX_MB:-200},\
PRESIGN_EXPIRY_TIER2_SEC=${PRESIGN_EXPIRY_TIER2_SEC:-21600},\
PRESIGN_EXPIRY_TIER3_MAX_MB=${PRESIGN_EXPIRY_TIER3_MAX_MB:-500},\
PRESIGN_EXPIRY_TIER3_SEC=${PRESIGN_EXPIRY_TIER3_SEC:-86400},\
PRESIGN_EXPIRY_TIER4_SEC=${PRESIGN_EXPIRY_TIER4_SEC:-172800},\
IOT_JOB_TIMEOUT_MINUTES=${IOT_JOB_TIMEOUT_MINUTES:-1440},\
CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-},\
CLOUDFRONT_KEY_PAIR_ID=${CLOUDFRONT_KEY_PAIR_ID:-},\
CLOUDFRONT_PRIVATE_KEY_SECRET=${CLOUDFRONT_PRIVATE_KEY_SECRET:-digilux-ota-cloudfront-key},\
ENTITLEMENT_FUNCTION=digilux_entitlement_check,\
LOG_LEVEL=INFO"

echo "========================================================"
echo "  Digilux User OTA Setup"
echo "  Account: ${ACCOUNT_ID}  Region: ${REGION}"
echo "========================================================"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — DynamoDB: digilux_ota_user_consents
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 1 — Creating DynamoDB table: ${CONSENTS_TABLE}"

TABLE_EXISTS=$(aws dynamodb list-tables --region "${REGION}" \
  --query "TableNames[?@=='${CONSENTS_TABLE}']" --output text)

if [[ -z "${TABLE_EXISTS}" ]]; then
  aws dynamodb create-table \
    --region "${REGION}" \
    --table-name "${CONSENTS_TABLE}" \
    --attribute-definitions \
        AttributeName=consentId,AttributeType=S \
        AttributeName=userId,AttributeType=S \
        AttributeName=deviceId,AttributeType=S \
        AttributeName=jobId,AttributeType=S \
    --key-schema AttributeName=consentId,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --global-secondary-indexes \
      "[
        {
          \"IndexName\": \"userId-deviceId-index\",
          \"KeySchema\": [
            {\"AttributeName\":\"userId\",\"KeyType\":\"HASH\"},
            {\"AttributeName\":\"deviceId\",\"KeyType\":\"RANGE\"}
          ],
          \"Projection\": {\"ProjectionType\":\"ALL\"}
        },
        {
          \"IndexName\": \"jobId-index\",
          \"KeySchema\": [
            {\"AttributeName\":\"jobId\",\"KeyType\":\"HASH\"}
          ],
          \"Projection\": {\"ProjectionType\":\"ALL\"}
        }
      ]" \
    --no-cli-pager

  echo "Waiting for table to become ACTIVE..."
  aws dynamodb wait table-exists --table-name "${CONSENTS_TABLE}" --region "${REGION}"

  # TTL on consentedAt — 24h retention
  aws dynamodb update-time-to-live \
    --region "${REGION}" \
    --table-name "${CONSENTS_TABLE}" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" \
    --no-cli-pager

  echo "Table created: ${CONSENTS_TABLE}"
else
  echo "Table already exists: ${CONSENTS_TABLE} — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — IAM Role: digilux-ota-user-lambda-role
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 2 — Creating IAM role: ${LAMBDA_ROLE_NAME}"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

ROLE_EXISTS=$(aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" \
  --query 'Role.RoleName' --output text 2>/dev/null || true)

if [[ -z "${ROLE_EXISTS}" ]]; then
  aws iam create-role \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --assume-role-policy-document "${TRUST_POLICY}" \
    --no-cli-pager
  echo "IAM role created: ${LAMBDA_ROLE_NAME}"
else
  echo "IAM role already exists: ${LAMBDA_ROLE_NAME} — updating policy"
fi

POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/digilux_ota_user_*:*"
    },
    {
      "Sid": "DeviceDataReadWrite",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_device_data",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_device_data/index/*"
      ]
    },
    {
      "Sid": "PackagesRead",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:Query"
      ],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_ota_packages"
    },
    {
      "Sid": "OtaJobsReadWrite",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_ota_jobs",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_ota_jobs/index/*"
      ]
    },
    {
      "Sid": "ConsentsReadWrite",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${CONSENTS_TABLE}",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${CONSENTS_TABLE}/index/*"
      ]
    },
    {
      "Sid": "IotJobs",
      "Effect": "Allow",
      "Action": [
        "iot:CreateJob",
        "iot:DescribeJob",
        "iot:TagResource"
      ],
      "Resource": [
        "arn:aws:iot:${REGION}:${ACCOUNT_ID}:job/digilux-ota-*",
        "arn:aws:iot:${REGION}:${ACCOUNT_ID}:thing/*"
      ]
    },
    {
      "Sid": "IotPublish",
      "Effect": "Allow",
      "Action": [
        "iot:Publish"
      ],
      "Resource": "arn:aws:iot:${REGION}:${ACCOUNT_ID}:topic/iot/device/*/ota"
    },
    {
      "Sid": "S3Presign",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::digilux-ota-artifacts/*"
    },
    {
      "Sid": "CloudFrontKey",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:digilux-ota-cloudfront-key*"
    },
    {
      "Sid": "XRay",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords"
      ],
      "Resource": "*"
    }
  ]
}
EOF
)

aws iam put-role-policy \
  --role-name "${LAMBDA_ROLE_NAME}" \
  --policy-name "${LAMBDA_ROLE_NAME}-policy" \
  --policy-document "${POLICY_DOC}" \
  --no-cli-pager

echo "IAM policy attached to ${LAMBDA_ROLE_NAME}"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
echo "Waiting 10s for IAM role propagation..."
sleep 10

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Lambda Functions
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 3 — Deploying Lambda functions"

LAMBDA_DIR="$(dirname "$0")/06_lambdas"

for FUNC_NAME in "${LAMBDA_NAMES[@]}"; do
  SRC_DIR="${LAMBDA_DIR}/${FUNC_NAME}"

  echo ""
  echo "  Packaging ${FUNC_NAME}..."
  ZIP_FILE="/tmp/${FUNC_NAME}.zip"
  PKG_TMP="/tmp/${FUNC_NAME}_pkg"
  rm -rf "${PKG_TMP}" && mkdir -p "${PKG_TMP}"
  cp "${SRC_DIR}/lambda_function.py" "${PKG_TMP}/"
  # Install dependencies if requirements.txt exists
  # Use --platform linux/x86_64 so native binaries match Lambda's runtime
  if [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    echo "    Installing dependencies from requirements.txt (linux/x86_64)..."
    pip install -q -r "${SRC_DIR}/requirements.txt" \
      -t "${PKG_TMP}/" \
      --platform manylinux2014_x86_64 \
      --python-version 3.11 \
      --only-binary=:all: \
      --upgrade
  fi
  cd "${PKG_TMP}"
  zip -qr "${ZIP_FILE}" .
  cd - > /dev/null
  rm -rf "${PKG_TMP}"

  EXISTING=$(aws lambda get-function --function-name "${FUNC_NAME}" \
    --region "${REGION}" --query 'Configuration.FunctionName' \
    --output text 2>/dev/null || true)

  if [[ -z "${EXISTING}" ]]; then
    echo "  Creating Lambda: ${FUNC_NAME}"
    aws lambda create-function \
      --region "${REGION}" \
      --function-name "${FUNC_NAME}" \
      --runtime python3.11 \
      --role "${ROLE_ARN}" \
      --handler lambda_function.lambda_handler \
      --zip-file "fileb://${ZIP_FILE}" \
      --timeout 30 \
      --memory-size 256 \
      --tracing-config Mode=Active \
      --environment "Variables={${LAMBDA_ENV_COMMON}}" \
      --no-cli-pager
    echo "  Waiting for function to become active..."
    aws lambda wait function-active --function-name "${FUNC_NAME}" --region "${REGION}"
  else
    echo "  Updating Lambda: ${FUNC_NAME}"
    aws lambda update-function-code \
      --region "${REGION}" \
      --function-name "${FUNC_NAME}" \
      --zip-file "fileb://${ZIP_FILE}" \
      --no-cli-pager
    aws lambda wait function-updated --function-name "${FUNC_NAME}" --region "${REGION}"

    aws lambda update-function-configuration \
      --region "${REGION}" \
      --function-name "${FUNC_NAME}" \
      --environment "Variables={${LAMBDA_ENV_COMMON}}" \
      --no-cli-pager
    aws lambda wait function-updated --function-name "${FUNC_NAME}" --region "${REGION}"
  fi

  echo "  Deployed: ${FUNC_NAME}"
done

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — CloudWatch Log Groups (30-day retention)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 4 — Setting log retention"

for FUNC_NAME in "${LAMBDA_NAMES[@]}"; do
  LOG_GROUP="/aws/lambda/${FUNC_NAME}"
  aws logs create-log-group --log-group-name "${LOG_GROUP}" \
    --region "${REGION}" --no-cli-pager 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "${LOG_GROUP}" \
    --retention-in-days 30 \
    --region "${REGION}" --no-cli-pager
  echo "  Log retention set: ${LOG_GROUP}"
done

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — API Gateway Routes
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 5 — Adding API Gateway routes"

# Helper: create or get resource by path part under a parent
get_or_create_resource() {
  local parent_id="$1"
  local path_part="$2"

  local existing
  existing=$(aws apigateway get-resources --rest-api-id "${API_ID}" \
    --region "${REGION}" \
    --query "items[?parentId=='${parent_id}' && pathPart=='${path_part}'].id" \
    --output text 2>/dev/null)

  if [[ -n "${existing}" ]]; then
    echo "${existing}"
  else
    aws apigateway create-resource \
      --rest-api-id "${API_ID}" \
      --region "${REGION}" \
      --parent-id "${parent_id}" \
      --path-part "${path_part}" \
      --query 'id' --output text --no-cli-pager
  fi
}

# Helper: add Lambda-proxy method (skip if already exists)
add_method() {
  local resource_id="$1"
  local http_method="$2"
  local func_name="$3"
  local auth="$4"   # "COGNITO_USER_POOLS" or "NONE"

  aws apigateway get-method \
    --rest-api-id "${API_ID}" \
    --resource-id "${resource_id}" \
    --http-method "${http_method}" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null 2>&1 && {
      echo "  Method ${http_method} ${resource_id} already exists — skipping"
      return 0
  }

  local auth_args=()
  if [[ "${auth}" == "COGNITO_USER_POOLS" ]]; then
    auth_args=(--authorizer-id "${AUTHORIZER_ID}" --authorization-type COGNITO_USER_POOLS)
  else
    auth_args=(--authorization-type NONE)
  fi

  aws apigateway put-method \
    --rest-api-id "${API_ID}" \
    --resource-id "${resource_id}" \
    --http-method "${http_method}" \
    "${auth_args[@]}" \
    --region "${REGION}" \
    --no-cli-pager

  LAMBDA_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/\
arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${func_name}/invocations"

  aws apigateway put-integration \
    --rest-api-id "${API_ID}" \
    --resource-id "${resource_id}" \
    --http-method "${http_method}" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "${LAMBDA_URI}" \
    --region "${REGION}" \
    --no-cli-pager

  # Lambda invoke permission
  local stmt_id="${func_name}-apigw-$(echo "${resource_id}" | tr -dc 'a-zA-Z0-9')"
  aws lambda add-permission \
    --function-name "${func_name}" \
    --statement-id "${stmt_id}" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${http_method}/*" \
    --region "${REGION}" \
    --no-cli-pager 2>/dev/null || true

  echo "  Added: ${http_method} → ${func_name}"
}

# Locate /api/v1/ota resource (already exists from admin setup)
OTA_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "${API_ID}" --region "${REGION}" \
  --query "items[?path=='/api/v1/ota'].id" --output text)

if [[ -z "${OTA_RESOURCE_ID}" ]]; then
  echo "ERROR: /api/v1/ota resource not found. Run 09_api_gateway.sh first."
  exit 1
fi
echo "  /api/v1/ota resource ID: ${OTA_RESOURCE_ID}"

# /api/v1/ota/my
MY_ID=$(get_or_create_resource "${OTA_RESOURCE_ID}" "my")
echo "  /api/v1/ota/my → ${MY_ID}"

# /api/v1/ota/my/updates  (parent resource for consent, download-link, status)
UPDATES_ID=$(get_or_create_resource "${MY_ID}" "updates")
echo "  /api/v1/ota/my/updates → ${UPDATES_ID}"

# NOTE: GET /api/v1/ota/my/updates was renamed to GET /api/v1/ota/device/available-updates
# That route is managed by the device resource block below — do NOT add GET here.

# POST /api/v1/ota/my/updates/consent  → digilux_ota_user_consent
CONSENT_ID=$(get_or_create_resource "${UPDATES_ID}" "consent")
echo "  /api/v1/ota/my/updates/consent → ${CONSENT_ID}"
add_method "${CONSENT_ID}" "POST" "digilux_ota_user_consent" "COGNITO_USER_POOLS"

# /api/v1/ota/my/updates/{jobId}
JOB_PARAM_ID=$(get_or_create_resource "${UPDATES_ID}" "{jobId}")
echo "  /api/v1/ota/my/updates/{jobId} → ${JOB_PARAM_ID}"

# /api/v1/ota/my/updates/{jobId}/status
STATUS_ID=$(get_or_create_resource "${JOB_PARAM_ID}" "status")
echo "  /api/v1/ota/my/updates/{jobId}/status → ${STATUS_ID}"

# GET /api/v1/ota/my/updates/{jobId}/status  → digilux_ota_user_update_status
add_method "${STATUS_ID}" "GET" "digilux_ota_user_update_status" "COGNITO_USER_POOLS"

# POST /api/v1/ota/my/updates/download-link  → digilux_ota_user_get_download_link
DOWNLOAD_LINK_ID=$(get_or_create_resource "${UPDATES_ID}" "download-link")
echo "  /api/v1/ota/my/updates/download-link → ${DOWNLOAD_LINK_ID}"
add_method "${DOWNLOAD_LINK_ID}" "POST" "digilux_ota_user_get_download_link" "COGNITO_USER_POOLS"

# /api/v1/ota/device  (already created for available-updates)
DEVICE_ID=$(get_or_create_resource "${OTA_RESOURCE_ID}" "device")
echo "  /api/v1/ota/device → ${DEVICE_ID}"

# GET /api/v1/ota/device/available-updates  → digilux_ota_user_check_updates
AVAIL_ID=$(get_or_create_resource "${DEVICE_ID}" "available-updates")
echo "  /api/v1/ota/device/available-updates → ${AVAIL_ID}"
add_method "${AVAIL_ID}" "GET" "digilux_ota_user_check_updates" "COGNITO_USER_POOLS"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Deploy API stage
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "STEP 6 — Deploying API Gateway stage: ${STAGE}"
aws apigateway create-deployment \
  --rest-api-id "${API_ID}" \
  --stage-name "${STAGE}" \
  --description "Deploy user OTA endpoints" \
  --region "${REGION}" \
  --no-cli-pager
echo "  Stage deployed: ${STAGE}"

echo ""
echo "========================================================"
echo "  User OTA setup complete."
echo ""
echo "  New endpoints:"
echo "  GET  /api/v1/ota/device/available-updates"
echo "  POST /api/v1/ota/my/updates/consent"
echo "  POST /api/v1/ota/my/updates/download-link"
echo "  GET  /api/v1/ota/my/updates/{jobId}/status"
echo ""
echo "  New DynamoDB table: ${CONSENTS_TABLE}"
echo "  New IAM role:       ${LAMBDA_ROLE_NAME}"
echo "========================================================"
