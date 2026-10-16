#!/bin/bash
# Phase 15 — CloudWatch alarms for all 18 OTA Lambda functions
set -euo pipefail
export AWS_PAGER="" PAGER=cat
source "$(dirname "$0")/_lib.sh"

log_section "Phase 15: CloudWatch Alarms"

REGION="${REGION:-ap-south-1}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
PREFIX="${PREFIX:-digilux}"

SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-ota-alerts}"
SNS_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"

log_info "Region    : $REGION"
log_info "SNS topic : $SNS_ARN"

# All 18 OTA Lambda functions (generic names — no customer prefix)
ADMIN_LAMBDAS=(
  ota_upload_url
  ota_upload_complete
  ota_package_status
  ota_package_activate
  ota_artifact_processor
  ota_artifact_key
  ota_package_register
  ota_compatibility_check
  ota_beta_users
  ota_job_create
  ota_job_sync
  ota_key_server
  ota_status_handler
  ota_device_register
)

USER_LAMBDAS=(
  ota_user_check_updates
  ota_user_consent
  ota_user_update_status
  ota_user_get_download_link
)

ALL_LAMBDAS=("${ADMIN_LAMBDAS[@]}" "${USER_LAMBDAS[@]}")

# artifact_processor has a 300s timeout — alarm at 80% = 240000ms
# All others have 30s timeout — alarm at 80% = 24000ms
DURATION_DEFAULT_MS=24000
DURATION_ARTIFACT_MS=240000

# Period: 60s; evaluate 1 data point
PERIOD=60
EVAL_PERIODS=1

create_alarm() {
  local NAME="$1"
  local METRIC="$2"
  local THRESHOLD="$3"
  local STAT="$4"
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

for FUNC in "${ALL_LAMBDAS[@]}"; do
  log_step "$FUNC"

  DURATION_MS=$DURATION_DEFAULT_MS
  [[ "$FUNC" == "ota_artifact_processor" ]] && DURATION_MS=$DURATION_ARTIFACT_MS

  create_alarm \
    "${FUNC}-errors" "Errors" 1 "Sum" \
    "OTA Lambda errors: ${FUNC}" "$FUNC"
  log_info "  errors alarm set"

  create_alarm \
    "${FUNC}-throttles" "Throttles" 1 "Sum" \
    "OTA Lambda throttles: ${FUNC}" "$FUNC"
  log_info "  throttles alarm set"

  create_alarm \
    "${FUNC}-duration" "Duration" "$DURATION_MS" "p99" \
    "OTA Lambda duration p99 high: ${FUNC}" "$FUNC"
  log_info "  duration alarm set (threshold: ${DURATION_MS}ms)"

  log_ok "  $FUNC — 3 alarms created"
done

log_phase_done
log_ok "All ${#ALL_LAMBDAS[@]} Lambda functions alarmed.  Alerts → $SNS_ARN"
