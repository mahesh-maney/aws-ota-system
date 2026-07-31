#!/bin/bash
# Phase 12 — Production hardening: SNS alerts, DLQs, log retention, S3 lifecycle, alarms
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ALERT_EMAIL="${ALERT_EMAIL:?Set ALERT_EMAIL env var before running}"

echo "════════════════════════════════════════════════════"
echo " Digilux OTA — Production Hardening"
echo "════════════════════════════════════════════════════"

# ── 1. SNS Alert Topic ────────────────────────────────────────────────────────
echo ""
echo "==> [1/5] SNS alert topic"

SNS_ARN=$(aws sns create-topic \
  --name "digilux-ota-alerts" \
  --region "$REGION" \
  --query 'TopicArn' --output text)
echo "    Topic: $SNS_ARN"

# Subscribe email (idempotent — won't duplicate if already subscribed)
EXISTING=$(aws sns list-subscriptions-by-topic \
  --topic-arn "$SNS_ARN" --region "$REGION" \
  --query "Subscriptions[?Endpoint=='${ALERT_EMAIL}'].SubscriptionArn" \
  --output text 2>/dev/null)

if [ -z "$EXISTING" ]; then
  aws sns subscribe \
    --topic-arn "$SNS_ARN" \
    --protocol email \
    --notification-endpoint "$ALERT_EMAIL" \
    --region "$REGION" > /dev/null
  echo "    Subscribed: $ALERT_EMAIL (confirmation email sent)"
else
  echo "    Already subscribed: $ALERT_EMAIL"
fi

# ── 2. SQS Dead Letter Queues ─────────────────────────────────────────────────
echo ""
echo "==> [2/5] SQS Dead Letter Queues"

for FUNC in digilux_ota_artifact_processor digilux_ota_status_handler; do
  QUEUE_NAME="${FUNC}-dlq"

  # Create SQS queue (idempotent)
  QUEUE_URL=$(aws sqs create-queue \
    --queue-name "$QUEUE_NAME" \
    --attributes '{"MessageRetentionPeriod":"1209600"}' \
    --region "$REGION" \
    --query 'QueueUrl' --output text)

  QUEUE_ARN=$(aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attribute-names QueueArn \
    --region "$REGION" \
    --query 'Attributes.QueueArn' --output text)

  # Attach DLQ to Lambda (MaximumRetryAttempts=2 before sending to DLQ)
  aws lambda put-function-event-invoke-config \
    --function-name "$FUNC" \
    --region "$REGION" \
    --maximum-retry-attempts 2 \
    --destination-config "{\"OnFailure\":{\"Destination\":\"${QUEUE_ARN}\"}}" > /dev/null

  echo "    $FUNC → DLQ: $QUEUE_NAME"
done

# ── 3. Log Retention ──────────────────────────────────────────────────────────
echo ""
echo "==> [3/5] Log retention (30 days)"

for FUNC in digilux_ota_upload_url digilux_ota_artifact_processor; do
  LOG_GROUP="/aws/lambda/$FUNC"
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days 30 \
    --region "$REGION"
  echo "    $LOG_GROUP → 30 days"
done

# ── 4. S3 Lifecycle — expire non-current versions after 90 days ───────────────
echo ""
echo "==> [4/5] S3 lifecycle for non-current versions"

aws s3api put-bucket-lifecycle-configuration \
  --bucket digilux-ota-artifacts \
  --region "$REGION" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "abort-incomplete-multipart",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
      },
      {
        "ID": "expire-noncurrent-versions",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 90},
        "NoncurrentVersionTransitions": [
          {
            "NoncurrentDays": 30,
            "StorageClass": "STANDARD_IA"
          }
        ]
      }
    ]
  }'
echo "    Non-current versions: move to STANDARD_IA after 30d, delete after 90d"

# ── 5. CloudWatch Alarms ──────────────────────────────────────────────────────
echo ""
echo "==> [5/5] CloudWatch alarms"

# Alarms for all 6 OTA Lambdas (update existing + add missing)
for FUNC in \
  digilux_ota_upload_url \
  digilux_ota_artifact_processor \
  digilux_ota_job_create \
  digilux_ota_status_handler \
  digilux_ota_compatibility_check \
  digilux_ota_device_register; do

  aws cloudwatch put-metric-alarm \
    --alarm-name "digilux-ota-errors-${FUNC}" \
    --alarm-description "Lambda errors in ${FUNC} — investigate immediately" \
    --metric-name Errors \
    --namespace AWS/Lambda \
    --statistic Sum \
    --period 300 \
    --threshold 3 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --evaluation-periods 1 \
    --dimensions Name=FunctionName,Value="$FUNC" \
    --treat-missing-data notBreaching \
    --alarm-actions "$SNS_ARN" \
    --ok-actions "$SNS_ARN" \
    --region "$REGION"
  echo "    Alarm: digilux-ota-errors-${FUNC}"
done

# DLQ depth alarm — alert if messages land in DLQ
for FUNC in digilux_ota_artifact_processor digilux_ota_status_handler; do
  QUEUE_URL=$(aws sqs get-queue-url --queue-name "${FUNC}-dlq" --region "$REGION" --query 'QueueUrl' --output text)
  QUEUE_NAME="${FUNC}-dlq"

  aws cloudwatch put-metric-alarm \
    --alarm-name "digilux-ota-dlq-${FUNC}" \
    --alarm-description "Messages in DLQ for ${FUNC} — async invocation failed after retries" \
    --metric-name ApproximateNumberOfMessagesVisible \
    --namespace AWS/SQS \
    --statistic Sum \
    --period 60 \
    --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --evaluation-periods 1 \
    --dimensions Name=QueueName,Value="$QUEUE_NAME" \
    --treat-missing-data notBreaching \
    --alarm-actions "$SNS_ARN" \
    --region "$REGION"
  echo "    Alarm: digilux-ota-dlq-${FUNC}"
done

# Update existing IoT rule error alarm to wire SNS
aws cloudwatch put-metric-alarm \
  --alarm-name "digilux-ota-rule-errors" \
  --alarm-description "OTA IoT rule failed to deliver messages to Lambda" \
  --metric-name TopicMatch \
  --namespace AWS/IoT \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions "$SNS_ARN" \
  --region "$REGION"
echo "    Alarm: digilux-ota-rule-errors (SNS wired)"

# Update CW dashboard to include all 6 Lambdas
aws cloudwatch put-dashboard \
  --dashboard-name "digilux-ota-fleet" \
  --region "$REGION" \
  --dashboard-body '{
  "widgets": [
    {
      "type": "metric",
      "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "OTA Lambda Errors (all functions)",
        "metrics": [
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_upload_url",          {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_artifact_processor",  {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_job_create",          {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_status_handler",      {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_compatibility_check", {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_device_register",     {"stat": "Sum", "period": 300}]
        ],
        "view": "timeSeries",
        "region": "ap-south-1",
        "period": 300
      }
    },
    {
      "type": "metric",
      "x": 12, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "OTA Lambda Invocations",
        "metrics": [
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_upload_url",          {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_artifact_processor",  {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_status_handler",      {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_device_register",     {"stat": "Sum", "period": 300}]
        ],
        "view": "timeSeries",
        "region": "ap-south-1",
        "period": 300
      }
    },
    {
      "type": "metric",
      "x": 0, "y": 6, "width": 12, "height": 6,
      "properties": {
        "title": "DLQ Depth (failed async invocations)",
        "metrics": [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "digilux_ota_artifact_processor-dlq", {"stat": "Sum", "period": 60}],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "digilux_ota_status_handler-dlq",     {"stat": "Sum", "period": 60}]
        ],
        "view": "timeSeries",
        "region": "ap-south-1",
        "period": 60
      }
    },
    {
      "type": "log",
      "x": 12, "y": 6, "width": 12, "height": 6,
      "properties": {
        "title": "Recent OTA Status Events",
        "query": "SOURCE \"/aws/lambda/digilux_ota_status_handler\" | fields @timestamp, @message | filter @message like /Job/ | sort @timestamp desc | limit 50",
        "region": "ap-south-1",
        "view": "table"
      }
    },
    {
      "type": "log",
      "x": 0, "y": 12, "width": 24, "height": 6,
      "properties": {
        "title": "OTA Errors and Failures",
        "query": "SOURCE \"/aws/lambda/digilux_ota_status_handler\" | SOURCE \"/aws/lambda/digilux_ota_job_create\" | SOURCE \"/aws/lambda/digilux_ota_upload_url\" | SOURCE \"/aws/lambda/digilux_ota_artifact_processor\" | fields @timestamp, @message | filter @message like /ERROR/ or @message like /FAILED/ | sort @timestamp desc | limit 50",
        "region": "ap-south-1",
        "view": "table"
      }
    }
  ]
}' > /dev/null
echo "    Dashboard: digilux-ota-fleet (updated with all Lambdas + DLQ widget)"

echo ""
echo "════════════════════════════════════════════════════"
echo " Production hardening complete."
echo ""
echo " SNS topic : $SNS_ARN"
echo " Alert email: $ALERT_EMAIL"
echo " NOTE: Check $ALERT_EMAIL inbox to confirm SNS subscription."
echo "════════════════════════════════════════════════════"
