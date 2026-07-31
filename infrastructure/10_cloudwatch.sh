#!/bin/bash
# Phase 10 — CloudWatch log groups, alarms, and dashboard
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
SNS_TOPIC_ARN=""   # Optional: set to SNS ARN for alert notifications

echo "==> Creating Lambda log groups"
for FUNC in \
  digilux_ota_package_register \
  digilux_ota_compatibility_check \
  digilux_ota_job_create \
  digilux_ota_status_handler \
  digilux_ota_device_register; do
  aws logs create-log-group \
    --log-group-name "/aws/lambda/$FUNC" \
    --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "/aws/lambda/$FUNC" \
    --retention-in-days 30 \
    --region "$REGION"
  echo "    /aws/lambda/$FUNC (30d retention)"
done

echo "==> Creating CloudWatch Dashboard: digilux-ota-fleet"
aws cloudwatch put-dashboard \
  --dashboard-name "digilux-ota-fleet" \
  --region "$REGION" \
  --dashboard-body '{
  "widgets": [
    {
      "type": "metric",
      "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "OTA Lambda Errors",
        "metrics": [
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_job_create", {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_status_handler", {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Errors", "FunctionName", "digilux_ota_device_register", {"stat": "Sum", "period": 300}]
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
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_status_handler", {"stat": "Sum", "period": 300}],
          ["AWS/Lambda", "Invocations", "FunctionName", "digilux_ota_device_register", {"stat": "Sum", "period": 300}]
        ],
        "view": "timeSeries",
        "region": "ap-south-1",
        "period": 300
      }
    },
    {
      "type": "log",
      "x": 0, "y": 6, "width": 24, "height": 6,
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
        "query": "SOURCE \"/aws/lambda/digilux_ota_status_handler\" | SOURCE \"/aws/lambda/digilux_ota_job_create\" | fields @timestamp, @message | filter @message like /ERROR/ or @message like /FAILED/ | sort @timestamp desc | limit 50",
        "region": "ap-south-1",
        "view": "table"
      }
    }
  ]
}'
echo "    Dashboard created: digilux-ota-fleet"

echo "==> Creating CloudWatch Alarms"

# Alarm: OTA Lambda errors spike
aws cloudwatch put-metric-alarm \
  --alarm-name "digilux-ota-lambda-errors" \
  --alarm-description "OTA Lambda function errors — investigate immediately" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --threshold 3 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --dimensions Name=FunctionName,Value=digilux_ota_status_handler \
  --treat-missing-data notBreaching \
  --region "$REGION" \
  ${SNS_TOPIC_ARN:+--alarm-actions "$SNS_TOPIC_ARN"}
echo "    Alarm: digilux-ota-lambda-errors"

# Alarm: IoT rule errors
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
  --region "$REGION" \
  ${SNS_TOPIC_ARN:+--alarm-actions "$SNS_TOPIC_ARN"} || true
echo "    Alarm: digilux-ota-rule-errors"

echo ""
echo "CloudWatch setup complete."
echo "View dashboard: https://ap-south-1.console.aws.amazon.com/cloudwatch/home?region=ap-south-1#dashboards:name=digilux-ota-fleet"
