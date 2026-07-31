#!/bin/bash
# Phase 8 — IoT Rules for OTA status and device registration
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
RULE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/digilux-ota-iot-rule-role"

STATUS_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:digilux_ota_status_handler"
REGISTER_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:digilux_ota_device_register"

create_or_replace_rule() {
  local RULE_NAME="$1"
  local PAYLOAD="$2"

  if aws iot get-topic-rule --rule-name "$RULE_NAME" --region "$REGION" 2>/dev/null; then
    echo "    Replacing existing rule: $RULE_NAME"
    aws iot replace-topic-rule \
      --rule-name "$RULE_NAME" \
      --topic-rule-payload "$PAYLOAD" \
      --region "$REGION"
  else
    echo "    Creating new rule: $RULE_NAME"
    aws iot create-topic-rule \
      --rule-name "$RULE_NAME" \
      --topic-rule-payload "$PAYLOAD" \
      --region "$REGION"
  fi
}

# ── digilux_ota_status_ingest ─────────────────────────────────────────────────
# Captures device OTA status updates → invokes status handler Lambda
echo "==> Rule: digilux_ota_status_ingest"
create_or_replace_rule "digilux_ota_status_ingest" "{
  \"sql\": \"SELECT *, topic(3) AS deviceId FROM 'iot/device/+/ota/status'\",
  \"description\": \"Capture OTA job status updates from controllers\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": {
      \"functionArn\": \"$STATUS_LAMBDA_ARN\"
    }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"/digilux/ota/rule-errors\",
      \"roleArn\": \"$RULE_ROLE_ARN\"
    }
  }
}"

# ── digilux_ota_device_register ───────────────────────────────────────────────
# Triggered when OTA agent starts on a controller → registers in inventory
echo "==> Rule: digilux_ota_device_register"
create_or_replace_rule "digilux_ota_device_register" "{
  \"sql\": \"SELECT *, topic(3) AS deviceIdFromTopic FROM 'iot/device/+/ota/register'\",
  \"description\": \"Register controller in OTA device inventory on agent startup\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": {
      \"functionArn\": \"$REGISTER_LAMBDA_ARN\"
    }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"/digilux/ota/rule-errors\",
      \"roleArn\": \"$RULE_ROLE_ARN\"
    }
  }
}"

# ── Create error log group ────────────────────────────────────────────────────
echo "==> Creating CloudWatch log group for rule errors"
aws logs create-log-group \
  --log-group-name "/digilux/ota/rule-errors" \
  --region "$REGION" 2>/dev/null || true

aws logs put-retention-policy \
  --log-group-name "/digilux/ota/rule-errors" \
  --retention-in-days 30 \
  --region "$REGION"

echo ""
echo "IoT Rules deployed."
