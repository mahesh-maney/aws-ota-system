#!/bin/bash
# Phase 8 — IoT Rules for OTA status updates and device registration
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

RULE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IOT_RULE_ROLE_NAME:-${PREFIX}-ota-iot-rule-role}"
STATUS_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}_ota_status_handler"
REGISTER_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}_ota_device_register"

LOG_GROUP="/${PREFIX}/ota/rule-errors"

# ── Create error log group ────────────────────────────────────────────────────
echo "==> CloudWatch log group for rule errors: $LOG_GROUP"
aws logs create-log-group \
  --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
aws logs put-retention-policy \
  --log-group-name "$LOG_GROUP" \
  --retention-in-days 30 \
  --region "$REGION"

create_or_replace_rule() {
  local RULE_NAME="$1"
  local PAYLOAD="$2"
  if aws iot get-topic-rule --rule-name "$RULE_NAME" --region "$REGION" 2>/dev/null | grep -q '"ruleName"'; then
    echo "    Replacing: $RULE_NAME"
    aws iot replace-topic-rule \
      --rule-name "$RULE_NAME" \
      --topic-rule-payload "$PAYLOAD" \
      --region "$REGION"
  else
    echo "    Creating: $RULE_NAME"
    aws iot create-topic-rule \
      --rule-name "$RULE_NAME" \
      --topic-rule-payload "$PAYLOAD" \
      --region "$REGION"
  fi
}

# ── Status ingest rule ────────────────────────────────────────────────────────
echo "==> IoT Rule: ${PREFIX}_ota_status_ingest"
create_or_replace_rule "${PREFIX}_ota_status_ingest" "{
  \"sql\": \"SELECT *, topic(3) AS deviceId FROM 'iot/device/+/ota/status'\",
  \"description\": \"Capture OTA job status updates from controllers\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": { \"functionArn\": \"${STATUS_LAMBDA_ARN}\" }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"${LOG_GROUP}\",
      \"roleArn\": \"${RULE_ROLE_ARN}\"
    }
  }
}"

# ── Device register rule ──────────────────────────────────────────────────────
echo "==> IoT Rule: ${PREFIX}_ota_device_register_rule"
create_or_replace_rule "${PREFIX}_ota_device_register_rule" "{
  \"sql\": \"SELECT *, topic(3) AS deviceIdFromTopic FROM 'iot/device/+/ota/register'\",
  \"description\": \"Register controller in OTA inventory on agent startup\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": { \"functionArn\": \"${REGISTER_LAMBDA_ARN}\" }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"${LOG_GROUP}\",
      \"roleArn\": \"${RULE_ROLE_ARN}\"
    }
  }
}"

echo ""
echo "IoT Rules deployed."
