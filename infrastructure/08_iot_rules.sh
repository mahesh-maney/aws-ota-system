#!/bin/bash
# Phase 8 — IoT Rules for OTA status updates and device registration
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

RULE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IOT_RULE_ROLE_NAME:-ota-iot-rule-role}"
STATUS_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:ota_status_handler"
REGISTER_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:ota_device_register"
JOB_SYNC_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:ota_job_sync"

LOG_GROUP="/ota/rule-errors"

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

# ── Enable IoT Core job lifecycle events ──────────────────────────────────────
# Required for the ota_job_lifecycle_sync rule to receive $aws/events/job/+/+ messages.
# Without this the rule exists but fires on nothing — jobs stay stuck in DynamoDB.
echo "==> Enabling IoT Core job lifecycle events"
aws iot update-event-configurations \
  --event-configurations '{
    "JOB":           {"Enabled": true},
    "JOB_EXECUTION": {"Enabled": true}
  }' \
  --region "$REGION"
echo "    Job and job-execution lifecycle events enabled."

# ── Status ingest rule ────────────────────────────────────────────────────────
echo "==> IoT Rule: ota_status_ingest"
create_or_replace_rule "ota_status_ingest" "{
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
echo "==> IoT Rule: ota_device_register_rule"
create_or_replace_rule "ota_device_register_rule" "{
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

# ── IoT job lifecycle rule → ota_job_sync ─────────────────────────────────────
# Fires when a job is completed/cancelled/deleted directly in IoT Core.
# Requires job lifecycle events to be enabled in IoT Core settings.
echo "==> IoT Rule: ota_job_lifecycle_sync"
create_or_replace_rule "ota_job_lifecycle_sync" "{
  \"sql\": \"SELECT topic(4) AS jobId, topic(5) AS eventType FROM '\$aws/events/job/+/+'\",
  \"description\": \"Sync ota_jobs DynamoDB when IoT Core job state changes\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": { \"functionArn\": \"${JOB_SYNC_LAMBDA_ARN}\" }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"${LOG_GROUP}\",
      \"roleArn\": \"${RULE_ROLE_ARN}\"
    }
  }
}"

# Lambda invoke permission for the IoT lifecycle rule
aws lambda remove-permission \
  --function-name "ota_job_sync" \
  --statement-id "ota_job_sync-iot-lifecycle" \
  --region "$REGION" 2>/dev/null || true
aws lambda add-permission \
  --function-name "ota_job_sync" \
  --statement-id "ota_job_sync-iot-lifecycle" \
  --action "lambda:InvokeFunction" \
  --principal "iot.amazonaws.com" \
  --source-arn "arn:aws:iot:${REGION}:${ACCOUNT_ID}:rule/ota_job_lifecycle_sync" \
  --region "$REGION" > /dev/null
echo "    Lambda invoke permission granted to IoT rule"

# ── IoT rule role: allow invoking job_sync Lambda ─────────────────────────────
aws iam put-role-policy \
  --role-name "${IOT_RULE_ROLE_NAME:-ota-iot-rule-role}" \
  --policy-name "ota-rule-job-sync" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": \"${JOB_SYNC_LAMBDA_ARN}\"
    }]
  }" 2>/dev/null || true

# ── MQTT handshake rule → ota_artifact_key ────────────────────────────────────
# Device sends {"jobId":"..."} to iot/device/{thingName}/ota/handshake/request.
# Lambda decrypts the artifact data key and publishes it back on the response topic.
ARTIFACT_KEY_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:ota_artifact_key"
echo "==> IoT Rule: ota_artifact_key_request"
create_or_replace_rule "ota_artifact_key_request" "{
  \"sql\": \"SELECT *, topic(3) AS thingName FROM 'iot/device/+/ota/handshake/request'\",
  \"description\": \"MQTT key delivery — forward handshake requests to ota_artifact_key Lambda\",
  \"ruleDisabled\": false,
  \"awsIotSqlVersion\": \"2016-03-23\",
  \"actions\": [{
    \"lambda\": { \"functionArn\": \"${ARTIFACT_KEY_LAMBDA_ARN}\" }
  }],
  \"errorAction\": {
    \"cloudwatchLogs\": {
      \"logGroupName\": \"${LOG_GROUP}\",
      \"roleArn\": \"${RULE_ROLE_ARN}\"
    }
  }
}"

aws lambda remove-permission \
  --function-name "ota_artifact_key" \
  --statement-id "ota_artifact_key-iot-handshake" \
  --region "$REGION" 2>/dev/null || true
aws lambda add-permission \
  --function-name "ota_artifact_key" \
  --statement-id "ota_artifact_key-iot-handshake" \
  --action "lambda:InvokeFunction" \
  --principal "iot.amazonaws.com" \
  --source-arn "arn:aws:iot:${REGION}:${ACCOUNT_ID}:rule/ota_artifact_key_request" \
  --region "$REGION" > /dev/null
echo "    Lambda invoke permission granted to IoT handshake rule"

aws iam put-role-policy \
  --role-name "${IOT_RULE_ROLE_NAME:-ota-iot-rule-role}" \
  --policy-name "ota-rule-artifact-key" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": \"${ARTIFACT_KEY_LAMBDA_ARN}\"
    }]
  }" 2>/dev/null || true

# ── EventBridge scheduled rule → ota_job_sync (daily staleness check) ─────────
echo "==> EventBridge: ota-job-sync-daily"
RULE_ARN=$(aws events put-rule \
  --name "ota-job-sync-daily" \
  --schedule-expression "rate(1 day)" \
  --description "Daily staleness check for OTA jobs — triggers ota_job_sync" \
  --state ENABLED \
  --region "$REGION" \
  --query RuleArn --output text)
echo "    Rule ARN: $RULE_ARN"

aws events put-targets \
  --rule "ota-job-sync-daily" \
  --region "$REGION" \
  --targets "[{
    \"Id\": \"ota-job-sync-target\",
    \"Arn\": \"${JOB_SYNC_LAMBDA_ARN}\"
  }]" > /dev/null

# Lambda invoke permission for EventBridge
aws lambda remove-permission \
  --function-name "ota_job_sync" \
  --statement-id "ota_job_sync-eventbridge-daily" \
  --region "$REGION" 2>/dev/null || true
aws lambda add-permission \
  --function-name "ota_job_sync" \
  --statement-id "ota_job_sync-eventbridge-daily" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "$RULE_ARN" \
  --region "$REGION" > /dev/null
echo "    Lambda invoke permission granted to EventBridge rule"

echo ""
echo "IoT Rules deployed."
