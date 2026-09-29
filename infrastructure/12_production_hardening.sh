#!/bin/bash
# Phase 12 — Production hardening: SNS alerts, DLQs, S3 lifecycle, CloudWatch alarms
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ALERT_EMAIL="${ALERT_EMAIL:?ALERT_EMAIL not set}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"

echo "════════════════════════════════════════════════════"
echo " OTA Production Hardening  (prefix: $PREFIX)"
echo "════════════════════════════════════════════════════"

# ── SNS Alert Topic ───────────────────────────────────────────────────────────
echo ""
echo "==> [1/4] SNS alert topic"
SNS_ARN=$(aws sns create-topic \
  --name "${PREFIX}-ota-alerts" \
  --region "$REGION" \
  --query 'TopicArn' --output text)
echo "    Topic: $SNS_ARN"

EXISTING=$(aws sns list-subscriptions-by-topic \
  --topic-arn "$SNS_ARN" --region "$REGION" \
  --query "Subscriptions[?Endpoint=='${ALERT_EMAIL}'].SubscriptionArn" \
  --output text 2>/dev/null)

if [[ -z "$EXISTING" ]]; then
  aws sns subscribe \
    --topic-arn "$SNS_ARN" \
    --protocol email \
    --notification-endpoint "$ALERT_EMAIL" \
    --region "$REGION" > /dev/null
  echo "    Subscribed: $ALERT_EMAIL (check inbox to confirm)"
else
  echo "    Already subscribed: $ALERT_EMAIL"
fi

# ── SQS Dead Letter Queues ────────────────────────────────────────────────────
echo ""
echo "==> [2/4] SQS Dead Letter Queues"
for SUFFIX in artifact_processor status_handler; do
  FUNC="${PREFIX}_ota_${SUFFIX}"
  QUEUE_NAME="${FUNC}-dlq"

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

  aws lambda put-function-event-invoke-config \
    --function-name "$FUNC" \
    --region "$REGION" \
    --maximum-retry-attempts 2 \
    --destination-config "{\"OnFailure\":{\"Destination\":\"${QUEUE_ARN}\"}}" > /dev/null

  echo "    $FUNC → DLQ: $QUEUE_NAME"
done

# ── S3 Lifecycle ──────────────────────────────────────────────────────────────
echo ""
echo "==> [3/4] S3 lifecycle rules"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$ARTIFACT_BUCKET" \
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
          {"NoncurrentDays": 30, "StorageClass": "STANDARD_IA"}
        ]
      }
    ]
  }'
echo "    Non-current versions: IA after 30d, deleted after 90d"

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
echo ""
echo "==> [4/4] CloudWatch alarms"
for SUFFIX in \
  upload_url artifact_processor job_create status_handler \
  device_register user_check_updates user_consent; do

  FUNC="${PREFIX}_ota_${SUFFIX}"
  aws cloudwatch put-metric-alarm \
    --alarm-name "${PREFIX}-ota-errors-${FUNC}" \
    --alarm-description "Lambda errors in ${FUNC}" \
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
    --ok-actions    "$SNS_ARN" \
    --region "$REGION"
  echo "    Alarm: ${PREFIX}-ota-errors-${FUNC}"
done

# DLQ depth alarms
for SUFFIX in artifact_processor status_handler; do
  QUEUE_NAME="${PREFIX}_ota_${SUFFIX}-dlq"
  aws cloudwatch put-metric-alarm \
    --alarm-name "${PREFIX}-ota-dlq-${SUFFIX}" \
    --alarm-description "Messages in DLQ for ${PREFIX}_ota_${SUFFIX}" \
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
  echo "    Alarm: ${PREFIX}-ota-dlq-${SUFFIX}"
done

echo ""
echo "════════════════════════════════════════════════════"
echo " Production hardening complete."
echo " SNS topic : $SNS_ARN"
echo " NOTE: Confirm SNS subscription in $ALERT_EMAIL inbox."
echo "════════════════════════════════════════════════════"
