#!/bin/bash
# Phase 7 — Package and deploy all OTA Lambda functions
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/digilux-ota-lambda-role"
RUNTIME="python3.11"
LAMBDA_DIR="$(cd "$(dirname "$0")/06_lambdas" && pwd)"

# Load configurable values from ota.config
ENV_FILE="$(dirname "$0")/ota.config"
if [[ -f "${ENV_FILE}" ]]; then
  source "${ENV_FILE}"
  echo "Loaded config from ${ENV_FILE}"
else
  echo "WARNING: ${ENV_FILE} not found — using Lambda defaults"
fi

COMMON_ENV="Variables={
  REGION=$REGION,
  ACCOUNT_ID=$ACCOUNT_ID,
  DEVICE_DATA_TABLE=digilux_device_data,
  PACKAGES_TABLE=digilux_ota_packages,
  OTA_JOBS_TABLE=digilux_ota_jobs,
  COMPAT_TABLE=digilux_ota_compatibility,
  ARTIFACT_BUCKET=digilux-ota-artifacts,
  SIGNING_SECRET=digilux-ota-signing-key,
  PRESIGN_EXPIRY_TIER1_MAX_MB=${PRESIGN_EXPIRY_TIER1_MAX_MB:-50},
  PRESIGN_EXPIRY_TIER1_SEC=${PRESIGN_EXPIRY_TIER1_SEC:-3600},
  PRESIGN_EXPIRY_TIER2_MAX_MB=${PRESIGN_EXPIRY_TIER2_MAX_MB:-200},
  PRESIGN_EXPIRY_TIER2_SEC=${PRESIGN_EXPIRY_TIER2_SEC:-21600},
  PRESIGN_EXPIRY_TIER3_MAX_MB=${PRESIGN_EXPIRY_TIER3_MAX_MB:-500},
  PRESIGN_EXPIRY_TIER3_SEC=${PRESIGN_EXPIRY_TIER3_SEC:-86400},
  PRESIGN_EXPIRY_TIER4_SEC=${PRESIGN_EXPIRY_TIER4_SEC:-172800},
  IOT_JOB_TIMEOUT_MINUTES=${IOT_JOB_TIMEOUT_MINUTES:-1440},
  CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-},
  CLOUDFRONT_KEY_PAIR_ID=${CLOUDFRONT_KEY_PAIR_ID:-},
  CLOUDFRONT_PRIVATE_KEY_SECRET=${CLOUDFRONT_PRIVATE_KEY_SECRET:-digilux-ota-cloudfront-key},
  COGNITO_POOL_ID=ap-south-1_h1o8s7257,
  CANARY_GROUP=DGX-Canary,
  CANARY_MAX=5
}"

deploy_lambda() {
  local NAME="$1"
  local HANDLER="$2"
  local SRC_DIR="$LAMBDA_DIR/$NAME"
  local ZIP_FILE="/tmp/${NAME}.zip"

  echo ""
  echo "==> Deploying: $NAME"

  # Install dependencies if requirements.txt exists
  if [ -f "$SRC_DIR/requirements.txt" ]; then
    echo "    Installing dependencies (Linux x86_64 platform)..."
    PKG_DIR="$SRC_DIR/package"
    rm -rf "$PKG_DIR" && mkdir -p "$PKG_DIR"
    # Use --platform to get Linux-compatible binaries (needed for C-extension packages)
    pip install -q \
      --platform manylinux2014_x86_64 \
      --python-version 3.11 \
      --only-binary=:all: \
      --implementation cp \
      -r "$SRC_DIR/requirements.txt" \
      -t "$PKG_DIR/" --upgrade
    cp "$SRC_DIR/lambda_function.py" "$PKG_DIR/"
    cd "$PKG_DIR" && zip -qr "$ZIP_FILE" . && cd - > /dev/null
  else
    cd "$SRC_DIR" && zip -q "$ZIP_FILE" lambda_function.py && cd - > /dev/null
  fi

  if aws lambda get-function --function-name "$NAME" --region "$REGION" 2>/dev/null; then
    echo "    Updating existing function..."
    aws lambda update-function-code \
      --function-name "$NAME" \
      --zip-file "fileb://$ZIP_FILE" \
      --region "$REGION" > /dev/null
    aws lambda wait function-updated --function-name "$NAME" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$NAME" \
      --runtime "$RUNTIME" \
      --handler "$HANDLER" \
      --timeout 30 \
      --memory-size 256 \
      --environment "$COMMON_ENV" \
      --region "$REGION" > /dev/null
  else
    echo "    Creating new function..."
    aws lambda create-function \
      --function-name "$NAME" \
      --runtime "$RUNTIME" \
      --role "$ROLE_ARN" \
      --handler "$HANDLER" \
      --zip-file "fileb://$ZIP_FILE" \
      --timeout 30 \
      --memory-size 256 \
      --environment "$COMMON_ENV" \
      --description "Digilux OTA — $NAME" \
      --region "$REGION" > /dev/null
    aws lambda wait function-active --function-name "$NAME" --region "$REGION"
  fi

  echo "    $NAME deployed."
  rm -f "$ZIP_FILE"
}

# The cryptography package needed by package_register is a layer dependency.
# Create a requirements.txt for it.
cat > "$LAMBDA_DIR/digilux_ota_package_register/requirements.txt" << 'EOF'
cryptography>=42.0.0
EOF

deploy_lambda "digilux_ota_package_register"  "lambda_function.lambda_handler"
deploy_lambda "digilux_ota_compatibility_check" "lambda_function.lambda_handler"
deploy_lambda "digilux_ota_job_create"          "lambda_function.lambda_handler"
deploy_lambda "digilux_ota_status_handler"      "lambda_function.lambda_handler"
deploy_lambda "digilux_ota_device_register"     "lambda_function.lambda_handler"

# Grant IoT permission to invoke the rule-triggered Lambdas
echo ""
echo "==> Adding IoT invoke permissions to rule-triggered Lambdas"
for FUNC in digilux_ota_status_handler digilux_ota_device_register; do
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
  echo "    Permission added: $FUNC"
done

echo ""
echo "All OTA Lambda functions deployed."
