#!/bin/bash
# Phase 15 — Create CloudWatch alarms for all OTA Lambda functions
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
SNS_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:digilux-ota-alerts"

# All OTA Lambda functions
LAMBDAS=(
  digilux_ota_package_register
  digilux_ota_compatibility_check
  digilux_ota_job_create
  digilux_ota_status_handler
  digilux_ota_device_register
  digilux_ota_user_check_updates
  digilux_ota_user_consent
  digilux_ota_user_update_status
  digilux_ota_user_get_download_link
)

# Lambda timeout is 30s — alarm at 80% = 24000ms
DURATION_THRESHOLD_MS=24000

# Period: 60s; evaluate 1 data point
PERIOD=60
EVAL_PERIODS=1

create_alarm() {
  local NAME="$1"
  local METRIC="$2"
  local THRESHOLD="$3"
  local STAT="$4"        # "Sum", "Average", etc. — or "extN" for p-stat
  local DESC="$5"
  local FUNC="$6"

  local STAT_ARGS
  if [[ "$STAT" == p* ]]; then
    STAT_ARGS="--extended-statistic $STAT"
  else
    STAT_ARGS="--statistic $STAT"
  fi

  # shellcheck disable=SC2086
  aws cloudwatch put-metric-alarm \
    --alarm-name        "$NAME" \
    --alarm-description "$DESC" \
    --namespace         "AWS/Lambda" \
    --metric-name       "$METRIC" \
    --dimensions        Name=FunctionName,Value="$FUNC" \
    $STAT_ARGS \
    --period            "$PERIOD" \
    --evaluation-periods "$EVAL_PERIODS" \
    --threshold         "$THRESHOLD" \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions     "$SNS_ARN" \
    --ok-actions        "$SNS_ARN" \
    --region            "$REGION" > /dev/null
}

echo "SNS topic: $SNS_ARN"
echo ""

for FUNC in "${LAMBDAS[@]}"; do
  echo "==> $FUNC"

  # Errors — alert on any error
  create_alarm \
    "${FUNC}-errors" \
    "Errors" \
    1 \
    "Sum" \
    "OTA Lambda errors: ${FUNC}" \
    "$FUNC"
  echo "    errors alarm set"

  # Throttles — alert on any throttle
  create_alarm \
    "${FUNC}-throttles" \
    "Throttles" \
    1 \
    "Sum" \
    "OTA Lambda throttles: ${FUNC}" \
    "$FUNC"
  echo "    throttles alarm set"

  # Duration — alert if p99 exceeds 80% of 30s timeout
  create_alarm \
    "${FUNC}-duration" \
    "Duration" \
    "$DURATION_THRESHOLD_MS" \
    "p99" \
    "OTA Lambda duration p99 > 24s: ${FUNC}" \
    "$FUNC"
  echo "    duration alarm set"
done

echo ""
echo "All CloudWatch alarms created. Alerts → $SNS_ARN"
