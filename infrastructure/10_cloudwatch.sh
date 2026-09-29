#!/bin/bash
# Phase 10 — CloudWatch log groups and dashboard
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"

ALL_FUNCS=(
  "${PREFIX}_ota_upload_url"
  "${PREFIX}_ota_package_activate"
  "${PREFIX}_ota_artifact_processor"
  "${PREFIX}_ota_package_register"
  "${PREFIX}_ota_compatibility_check"
  "${PREFIX}_ota_job_create"
  "${PREFIX}_ota_status_handler"
  "${PREFIX}_ota_device_register"
  "${PREFIX}_ota_user_check_updates"
  "${PREFIX}_ota_user_consent"
  "${PREFIX}_ota_user_update_status"
  "${PREFIX}_ota_user_get_download_link"
)

echo "==> CloudWatch log groups (30-day retention)"
for FUNC in "${ALL_FUNCS[@]}"; do
  LOG_GROUP="/aws/lambda/${FUNC}"
  aws logs create-log-group \
    --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days 30 \
    --region "$REGION"
  echo "    $LOG_GROUP"
done

# Build dashboard JSON with dynamic PREFIX
DASHBOARD_BODY=$(cat <<ENDDASH
{
  "widgets": [
    {
      "type": "metric",
      "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "OTA Lambda Errors",
        "metrics": [
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_job_create",        {"stat":"Sum","period":300}],
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_artifact_processor",{"stat":"Sum","period":300}],
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_status_handler",    {"stat":"Sum","period":300}],
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_device_register",   {"stat":"Sum","period":300}],
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_user_consent",      {"stat":"Sum","period":300}],
          ["AWS/Lambda","Errors","FunctionName","${PREFIX}_ota_user_check_updates",{"stat":"Sum","period":300}]
        ],
        "view": "timeSeries",
        "region": "${REGION}",
        "period": 300
      }
    },
    {
      "type": "metric",
      "x": 12, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "OTA Lambda Invocations",
        "metrics": [
          ["AWS/Lambda","Invocations","FunctionName","${PREFIX}_ota_status_handler",    {"stat":"Sum","period":300}],
          ["AWS/Lambda","Invocations","FunctionName","${PREFIX}_ota_device_register",   {"stat":"Sum","period":300}],
          ["AWS/Lambda","Invocations","FunctionName","${PREFIX}_ota_user_check_updates",{"stat":"Sum","period":300}],
          ["AWS/Lambda","Invocations","FunctionName","${PREFIX}_ota_user_consent",      {"stat":"Sum","period":300}]
        ],
        "view": "timeSeries",
        "region": "${REGION}",
        "period": 300
      }
    },
    {
      "type": "log",
      "x": 0, "y": 6, "width": 24, "height": 6,
      "properties": {
        "title": "Recent OTA Status Events",
        "query": "SOURCE \"/aws/lambda/${PREFIX}_ota_status_handler\" | fields @timestamp, @message | filter @message like /Job/ | sort @timestamp desc | limit 50",
        "region": "${REGION}",
        "view": "table"
      }
    },
    {
      "type": "log",
      "x": 0, "y": 12, "width": 24, "height": 6,
      "properties": {
        "title": "OTA Errors",
        "query": "SOURCE \"/aws/lambda/${PREFIX}_ota_job_create\" | SOURCE \"/aws/lambda/${PREFIX}_ota_status_handler\" | SOURCE \"/aws/lambda/${PREFIX}_ota_artifact_processor\" | fields @timestamp, @message | filter @message like /ERROR/ or @message like /FAILED/ | sort @timestamp desc | limit 50",
        "region": "${REGION}",
        "view": "table"
      }
    }
  ]
}
ENDDASH
)

echo ""
echo "==> CloudWatch Dashboard: ${PREFIX}-ota-fleet"
aws cloudwatch put-dashboard \
  --dashboard-name "${PREFIX}-ota-fleet" \
  --region "$REGION" \
  --dashboard-body "$DASHBOARD_BODY"
echo "    Dashboard created."

echo ""
echo "CloudWatch setup complete."
