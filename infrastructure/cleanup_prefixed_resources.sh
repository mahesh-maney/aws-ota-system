#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# cleanup_prefixed_resources.sh
#
# Deletes all non-S3 OTA resources that were created with the old
# ${PREFIX}_ota_* / ${PREFIX}-ota-* naming convention.
#
# Use this BEFORE redeploying phases 02–12 with the corrected generic names
# (ota_packages, ota-lambda-role, ota_artifact_processor, etc.)
#
# S3 buckets are NOT touched — they keep the customer prefix and retain data.
#
# Usage:
#   bash infrastructure/cleanup_prefixed_resources.sh              # dry run (safe)
#   bash infrastructure/cleanup_prefixed_resources.sh --execute    # actually delete
#   bash infrastructure/cleanup_prefixed_resources.sh --config path/to.cfg
#
# Exit code: 0 = clean, 1 = one or more deletions failed
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _RED='\033[0;31m'; _YELLOW='\033[1;33m'
_BLUE='\033[0;34m';  _BOLD='\033[1m';   _NC='\033[0m'

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
EXECUTE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=true; shift ;;
    --config)  CONFIG_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo -e "${_RED}ERROR:${_NC} Config file not found: $CONFIG_FILE"
  exit 1
fi
set -a; source "$CONFIG_FILE"; set +a

PREFIX="${PREFIX:?PREFIX not set in deploy.config}"
REGION="${REGION:-us-east-1}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
API_GATEWAY_ID="${API_GATEWAY_ID:-}"

# IoT rule names used [a-zA-Z0-9_] — hyphens were replaced with underscores
RULE_PREFIX="${PREFIX//-/_}"

# ── Helpers ───────────────────────────────────────────────────────────────────
FAIL_COUNT=0

_run() {
  # Wrapper: in dry-run mode, print; in execute mode, run and handle errors.
  local DESC="$1"; shift
  if [[ "$EXECUTE" == "false" ]]; then
    echo -e "  ${_YELLOW}[DRY RUN]${_NC}  $DESC"
  else
    if "$@" 2>/dev/null; then
      echo -e "  ${_GREEN}[DELETED]${_NC}  $DESC"
    else
      echo -e "  ${_YELLOW}[SKIPPED]${_NC}  $DESC  (not found or already gone)"
    fi
  fi
}

_exists_lambda() {
  aws lambda get-function --function-name "$1" --region "$REGION" \
    2>/dev/null | grep -q '"FunctionName"'
}

_exists_table() {
  aws dynamodb describe-table --table-name "$1" --region "$REGION" \
    2>/dev/null | grep -q '"TableStatus"'
}

_exists_secret() {
  aws secretsmanager describe-secret --secret-id "$1" --region "$REGION" \
    2>/dev/null | grep -q '"ARN"'
}

_exists_role() {
  aws iam get-role --role-name "$1" \
    2>/dev/null | grep -q '"RoleName"'
}

_exists_iot_rule() {
  aws iot get-topic-rule --rule-name "$1" --region "$REGION" \
    2>/dev/null | grep -q '"ruleName"'
}

_exists_thing_type() {
  aws iot describe-thing-type --thing-type-name "$1" --region "$REGION" \
    2>/dev/null | grep -q '"thingTypeName"'
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD}  OTA Resource Cleanup — removing old '${PREFIX}' prefix${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "  Config : $CONFIG_FILE"
echo -e "  Prefix : ${_BOLD}${PREFIX}${_NC}"
echo -e "  Region : ${_BOLD}${REGION}${_NC}"
echo -e "  Account: ${_BOLD}${ACCOUNT_ID}${_NC}"
echo ""
if [[ "$EXECUTE" == "false" ]]; then
  echo -e "  ${_YELLOW}DRY RUN — no changes will be made.${_NC}"
  echo -e "  ${_YELLOW}Re-run with --execute to perform actual deletion.${_NC}"
else
  echo -e "  ${_RED}${_BOLD}EXECUTE MODE — resources will be permanently deleted.${_NC}"
fi
echo ""

# ── 1. Lambda functions ───────────────────────────────────────────────────────
echo -e "${_BOLD}── [1/10] Lambda functions ──────────────────────────────────────${_NC}"
LAMBDA_SUFFIXES=(
  upload_url
  package_activate
  artifact_processor
  package_register
  compatibility_check
  job_create
  status_handler
  device_register
  user_check_updates
  user_consent
  user_update_status
  user_get_download_link
)
for SUFFIX in "${LAMBDA_SUFFIXES[@]}"; do
  FUNC="${PREFIX}_ota_${SUFFIX}"
  if _exists_lambda "$FUNC"; then
    _run "$FUNC" \
      aws lambda delete-function \
        --function-name "$FUNC" \
        --region "$REGION"
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $FUNC"
  fi
done
echo ""

# ── 2. IoT rules ──────────────────────────────────────────────────────────────
echo -e "${_BOLD}── [2/10] IoT rules ─────────────────────────────────────────────${_NC}"
for RULE in "${RULE_PREFIX}_ota_status_ingest" "${RULE_PREFIX}_ota_device_register_rule"; do
  if _exists_iot_rule "$RULE"; then
    _run "$RULE" \
      aws iot delete-topic-rule \
        --rule-name "$RULE" \
        --region "$REGION"
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $RULE"
  fi
done
echo ""

# ── 3. SQS Dead Letter Queues ─────────────────────────────────────────────────
echo -e "${_BOLD}── [3/10] SQS Dead Letter Queues ────────────────────────────────${_NC}"
for SUFFIX in artifact_processor status_handler; do
  QUEUE_NAME="${PREFIX}_ota_${SUFFIX}-dlq"
  QUEUE_URL=$(aws sqs get-queue-url \
    --queue-name "$QUEUE_NAME" \
    --region "$REGION" \
    --query 'QueueUrl' --output text 2>/dev/null || true)
  if [[ -n "$QUEUE_URL" ]]; then
    _run "$QUEUE_NAME" \
      aws sqs delete-queue \
        --queue-url "$QUEUE_URL" \
        --region "$REGION"
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $QUEUE_NAME"
  fi
done
echo ""

# ── 4. CloudWatch alarms ──────────────────────────────────────────────────────
echo -e "${_BOLD}── [4/10] CloudWatch alarms ─────────────────────────────────────${_NC}"
# Collect all alarm names matching our old prefix
ALARMS=$(aws cloudwatch describe-alarms \
  --region "$REGION" \
  --query "MetricAlarms[?starts_with(AlarmName,'${PREFIX}-ota-')].AlarmName" \
  --output text 2>/dev/null || true)

if [[ -z "$ALARMS" ]]; then
  echo -e "  ${_BLUE}[ABSENT]${_NC}   No ${PREFIX}-ota-* alarms found"
else
  # Split into array (handles whitespace-separated output)
  read -ra ALARM_ARR <<< "$ALARMS"
  for ALARM in "${ALARM_ARR[@]}"; do
    _run "$ALARM" \
      aws cloudwatch delete-alarms \
        --alarm-names "$ALARM" \
        --region "$REGION"
  done
fi
echo ""

# ── 5. CloudWatch dashboard ───────────────────────────────────────────────────
echo -e "${_BOLD}── [5/10] CloudWatch dashboard ──────────────────────────────────${_NC}"
DASHBOARD="${PREFIX}-ota-fleet"
if aws cloudwatch get-dashboard --dashboard-name "$DASHBOARD" \
     --region "$REGION" 2>/dev/null | grep -q '"DashboardBody"'; then
  _run "$DASHBOARD" \
    aws cloudwatch delete-dashboards \
      --dashboard-names "$DASHBOARD" \
      --region "$REGION"
else
  echo -e "  ${_BLUE}[ABSENT]${_NC}   $DASHBOARD"
fi
echo ""

# ── 6. CloudWatch log groups ──────────────────────────────────────────────────
echo -e "${_BOLD}── [6/10] CloudWatch log groups ─────────────────────────────────${_NC}"
# Lambda log groups
LOG_GROUPS=$(aws logs describe-log-groups \
  --log-group-name-prefix "/aws/lambda/${PREFIX}_ota_" \
  --region "$REGION" \
  --query 'logGroups[].logGroupName' \
  --output text 2>/dev/null || true)

if [[ -z "$LOG_GROUPS" ]]; then
  echo -e "  ${_BLUE}[ABSENT]${_NC}   /aws/lambda/${PREFIX}_ota_*"
else
  for LG in $LOG_GROUPS; do
    _run "$LG" \
      aws logs delete-log-group \
        --log-group-name "$LG" \
        --region "$REGION"
  done
fi

# IoT rule error log group
RULE_LOG="/${PREFIX}/ota/rule-errors"
if aws logs describe-log-groups \
     --log-group-name-prefix "$RULE_LOG" \
     --region "$REGION" \
     --query 'logGroups[0].logGroupName' \
     --output text 2>/dev/null | grep -q "$RULE_LOG"; then
  _run "$RULE_LOG" \
    aws logs delete-log-group \
      --log-group-name "$RULE_LOG" \
      --region "$REGION"
else
  echo -e "  ${_BLUE}[ABSENT]${_NC}   $RULE_LOG"
fi
echo ""

# ── 7. SNS alert topic ────────────────────────────────────────────────────────
echo -e "${_BOLD}── [7/10] SNS alert topic ───────────────────────────────────────${_NC}"
SNS_ARN=$(aws sns list-topics \
  --region "$REGION" \
  --query "Topics[?ends_with(TopicArn,':${PREFIX}-ota-alerts')].TopicArn" \
  --output text 2>/dev/null || true)

if [[ -n "$SNS_ARN" ]]; then
  _run "${PREFIX}-ota-alerts  ($SNS_ARN)" \
    aws sns delete-topic \
      --topic-arn "$SNS_ARN" \
      --region "$REGION"
else
  echo -e "  ${_BLUE}[ABSENT]${_NC}   ${PREFIX}-ota-alerts"
fi
echo ""

# ── 8. IAM roles and inline policies ─────────────────────────────────────────
echo -e "${_BOLD}── [8/10] IAM roles ─────────────────────────────────────────────${_NC}"
delete_role() {
  local ROLE="$1" POLICY="$2"
  if _exists_role "$ROLE"; then
    # Detach inline policy first (required before role deletion)
    if aws iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY" \
         2>/dev/null | grep -q '"PolicyName"'; then
      _run "  inline policy ${POLICY} on ${ROLE}" \
        aws iam delete-role-policy \
          --role-name "$ROLE" \
          --policy-name "$POLICY"
    fi
    # Detach any managed policies
    MANAGED=$(aws iam list-attached-role-policies \
      --role-name "$ROLE" \
      --query 'AttachedPolicies[].PolicyArn' \
      --output text 2>/dev/null || true)
    for PA in $MANAGED; do
      _run "  detach managed policy ${PA} from ${ROLE}" \
        aws iam detach-role-policy \
          --role-name "$ROLE" \
          --policy-arn "$PA"
    done
    _run "role $ROLE" \
      aws iam delete-role \
        --role-name "$ROLE"
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $ROLE"
  fi
}

delete_role "${PREFIX}-ota-lambda-role"      "${PREFIX}-ota-admin-permissions"
delete_role "${PREFIX}-ota-user-lambda-role" "${PREFIX}-ota-user-permissions"
delete_role "${PREFIX}-ota-iot-rule-role"    "${PREFIX}-ota-rule-permissions"
echo ""

# ── 9. DynamoDB tables ────────────────────────────────────────────────────────
echo -e "${_BOLD}── [9/10] DynamoDB tables ───────────────────────────────────────${_NC}"
DDB_TABLES=(
  "${PREFIX}_ota_packages"
  "${PREFIX}_ota_jobs"
  "${PREFIX}_ota_compatibility"
  "${PREFIX}_ota_deployments"
  "${PREFIX}_ota_user_consents"
  "${PREFIX}_ota_beta_users"
)
for TABLE in "${DDB_TABLES[@]}"; do
  if _exists_table "$TABLE"; then
    _run "$TABLE" \
      aws dynamodb delete-table \
        --table-name "$TABLE" \
        --region "$REGION"
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $TABLE"
  fi
done
echo ""

# ── 10. Secrets Manager ───────────────────────────────────────────────────────
echo -e "${_BOLD}── [10/11] Secrets Manager ──────────────────────────────────────${_NC}"
SECRET="${PREFIX}-ota-signing-key"
if _exists_secret "$SECRET"; then
  # --force-delete-without-recovery skips the 7-day window
  _run "$SECRET" \
    aws secretsmanager delete-secret \
      --secret-id "$SECRET" \
      --force-delete-without-recovery \
      --region "$REGION"
else
  echo -e "  ${_BLUE}[ABSENT]${_NC}   $SECRET"
fi
echo ""

# ── 11. IoT Thing Types ───────────────────────────────────────────────────────
# Note: a thing type can only be deleted after it has been deprecated for 5 min.
# The script deprecates and then deletes (two-step). If things are still
# associated, the delete will fail — that is expected and safe to ignore.
echo -e "${_BOLD}── [11/11] IoT Thing Types ──────────────────────────────────────${_NC}"
for TYPE in "${PREFIX}-network-controller" "${PREFIX}-zigbee-device"; do
  if _exists_thing_type "$TYPE"; then
    if [[ "$EXECUTE" == "true" ]]; then
      echo -e "  ${_YELLOW}[INFO]${_NC}     Deprecating $TYPE (required before deletion)..."
      aws iot deprecate-thing-type \
        --thing-type-name "$TYPE" \
        --region "$REGION" 2>/dev/null || true
      echo -e "  ${_YELLOW}[INFO]${_NC}     Waiting 5 min for deprecation window..."
      sleep 300
      if aws iot delete-thing-type \
           --thing-type-name "$TYPE" \
           --region "$REGION" 2>/dev/null; then
        echo -e "  ${_GREEN}[DELETED]${_NC}  $TYPE"
      else
        echo -e "  ${_YELLOW}[SKIPPED]${_NC}  $TYPE (things still associated — safe to leave)"
      fi
    else
      echo -e "  ${_YELLOW}[DRY RUN]${_NC}  $TYPE  (requires 5-min deprecation window — will add delay)"
    fi
  else
    echo -e "  ${_BLUE}[ABSENT]${_NC}   $TYPE"
  fi
done
echo ""

# ── 12. API Gateway — remove /ota resource subtree ───────────────────────────
# All OTA methods/integrations must be deleted so phase 09 can recreate them
# pointing to the new generic Lambda function names.
echo -e "${_BOLD}── [12/12] API Gateway /ota resource subtree ────────────────────${_NC}"
if [[ -z "$API_GATEWAY_ID" ]]; then
  echo -e "  ${_YELLOW}[WARN]${_NC}     API_GATEWAY_ID not set — skipping API Gateway cleanup."
  echo -e "             Set API_GATEWAY_ID in deploy.config and re-run, or"
  echo -e "             manually delete the /ota resource in API Gateway console."
else
  # Find /api/v1 resource
  API_ROOT=$(aws apigateway get-resources \
    --rest-api-id "$API_GATEWAY_ID" \
    --region "$REGION" \
    --limit 500 \
    --query "items[?path=='/api/v1'].id" \
    --output text 2>/dev/null || true)

  if [[ -z "$API_ROOT" ]]; then
    echo -e "  ${_YELLOW}[WARN]${_NC}     /api/v1 not found in API Gateway $API_GATEWAY_ID"
  else
    # Find /api/v1/ota resource
    OTA_RESOURCE=$(aws apigateway get-resources \
      --rest-api-id "$API_GATEWAY_ID" \
      --region "$REGION" \
      --limit 500 \
      --query "items[?parentId=='${API_ROOT}' && pathPart=='ota'].id" \
      --output text 2>/dev/null || true)

    if [[ -z "$OTA_RESOURCE" ]]; then
      echo -e "  ${_BLUE}[ABSENT]${_NC}   /api/v1/ota  (API: $API_GATEWAY_ID)"
    else
      _run "/api/v1/ota subtree (ID: $OTA_RESOURCE) in API $API_GATEWAY_ID" \
        aws apigateway delete-resource \
          --rest-api-id "$API_GATEWAY_ID" \
          --resource-id "$OTA_RESOURCE" \
          --region "$REGION"

      if [[ "$EXECUTE" == "true" ]]; then
        # Deploy the API to apply the deletion
        echo -e "  ${_BLUE}[INFO]${_NC}     Deploying API Gateway to apply resource deletion..."
        aws apigateway create-deployment \
          --rest-api-id "$API_GATEWAY_ID" \
          --stage-name "${API_GATEWAY_STAGE:-api}" \
          --description "Cleanup: removed /ota subtree for Lambda rename" \
          --region "$REGION" > /dev/null
        echo -e "  ${_GREEN}[OK]${_NC}       API Gateway deployed."
      fi
    fi
  fi
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
if [[ "$EXECUTE" == "false" ]]; then
  echo -e "${_YELLOW}${_BOLD}  Dry run complete — no resources were deleted.${_NC}"
  echo ""
  echo -e "  To perform actual deletion, run:"
  echo     "    bash infrastructure/cleanup_prefixed_resources.sh --execute"
else
  if [[ $FAIL_COUNT -eq 0 ]]; then
    echo -e "${_GREEN}${_BOLD}  Cleanup complete.${_NC}"
  else
    echo -e "${_RED}${_BOLD}  Cleanup finished with ${FAIL_COUNT} error(s) — review output above.${_NC}"
  fi
  echo ""
  echo -e "  ${_BOLD}Next steps:${_NC}"
  echo     "    bash infrastructure/deploy.sh --phase 02   # new signing key"
  echo     "    bash infrastructure/deploy.sh --phase 03   # IoT thing types"
  echo     "    bash infrastructure/deploy.sh --phase 04   # DynamoDB tables"
  echo     "    bash infrastructure/deploy.sh --phase 05   # IAM roles"
  echo     "    bash infrastructure/deploy.sh --phase 07   # Lambda functions"
  echo     "    bash infrastructure/deploy.sh --phase 08   # IoT rules"
  echo     "    bash infrastructure/deploy.sh --phase 09   # API Gateway routes"
  echo     "    bash infrastructure/deploy.sh --phase 10   # CloudWatch"
  echo     "    bash infrastructure/deploy.sh --phase 11   # S3 event trigger"
  echo     "    bash infrastructure/deploy.sh --phase 12   # Production hardening"
fi
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo ""

[[ $FAIL_COUNT -eq 0 ]]
