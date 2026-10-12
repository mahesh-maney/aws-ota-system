#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# preflight_permissions_check.sh
#
# Verifies that the current AWS principal has all permissions required to run:
#   1. cleanup_prefixed_resources.sh  (delete old PREFIX-named resources)
#   2. deploy.sh --from 02            (phases 02–12 full redeploy)
#   3. deploy_provisioning_validation.sh
#
# Each check uses the safest available API call for that service.  A call that
# returns a non-permission error (ResourceNotFound, NoSuchEntity, etc.) is
# counted as PASS — the user has service access; the resource just doesn't
# exist yet.  Only AccessDenied / "not authorized" responses count as FAIL.
#
# On failure, generates  preflight_missing_permissions.json  in the same
# directory and prints the exact CLI command to create + attach the policy.
#
# Usage:
#   bash infrastructure/preflight_permissions_check.sh
#   bash infrastructure/preflight_permissions_check.sh --config path/to/deploy.config
#
# Exit code: 0 = all checks passed, 1 = one or more checks failed
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _RED='\033[0;31m'; _YELLOW='\033[1;33m'
_BLUE='\033[0;34m';  _BOLD='\033[1m';   _NC='\033[0m'

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo -e "${_RED}ERROR:${_NC} Config file not found: $CONFIG_FILE"
  echo "       Copy deploy.config.template → deploy.config and fill in values."
  exit 1
fi
set -a; source "$CONFIG_FILE"; set +a

PREFIX="${PREFIX:?PREFIX not set in deploy.config}"
REGION="${REGION:-us-east-1}"
API_GATEWAY_ID="${API_GATEWAY_ID:-}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"

# ── Identity ──────────────────────────────────────────────────────────────────
echo -e "\n${_BOLD}${_BLUE}── Identity ─────────────────────────────────────────────────────────────${_NC}"

IDENTITY=$(aws sts get-caller-identity --region "$REGION" 2>&1) || {
  echo -e "${_RED}FATAL:${_NC} sts:GetCallerIdentity failed — check AWS credentials and region."
  echo "$IDENTITY"
  exit 1
}

ACCOUNT_ID="${ACCOUNT_ID:-$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")}"
PRINCIPAL_ARN=$(echo "$IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")

# Detect SSO assumed-role vs plain IAM user/role
IS_SSO_ROLE=false
ROLE_NAME=""
if [[ "$PRINCIPAL_ARN" == *":assumed-role/"* ]]; then
  IS_SSO_ROLE=true
  ROLE_NAME=$(echo "$PRINCIPAL_ARN" | sed 's|.*:assumed-role/||' | cut -d'/' -f1)
fi

echo "  Principal   : $PRINCIPAL_ARN"
echo "  Account     : $ACCOUNT_ID"
echo "  Region      : $REGION"
echo "  PREFIX      : $PREFIX"
echo "  ArtifactBkt : $ARTIFACT_BUCKET"

if [[ "$IS_SSO_ROLE" == "true" ]]; then
  echo -e "  IdentityType: ${_YELLOW}SSO Assumed Role${_NC} — remediation policy must target role: ${_BOLD}${ROLE_NAME}${_NC}"
else
  echo "  IdentityType: IAM User / Role"
fi

# ── Tracking ──────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
declare -a FAILED_GROUPS=()

# ── Helpers ───────────────────────────────────────────────────────────────────
_section() {
  echo -e "\n${_BOLD}${_BLUE}── $1 ──────────────────────────────────────────────────────────────────${_NC}"
}

# _check  <label>  <group_tag>  <aws cli cmd...>
#
# Runs the command.  AccessDenied / "not authorized" → FAIL.
# Any other error (resource not found, etc.) → PASS (access exists, resource absent).
# Always returns 0 so set -euo pipefail doesn't kill the script.
_check() {
  local LABEL="$1"
  local GROUP="$2"
  shift 2

  local OUT RC=0
  OUT=$("$@" 2>&1) || RC=$?

  if [[ $RC -ne 0 ]] && echo "$OUT" | grep -qiE \
      "AccessDenied|is not authorized|UnauthorizedClient|not authorized to perform|AuthorizationError"; then
    echo -e "  ${_RED}[FAIL]${_NC}  $LABEL"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    if [[ ! " ${FAILED_GROUPS[*]:-} " =~ " ${GROUP} " ]]; then
      FAILED_GROUPS+=("$GROUP")
    fi
  else
    echo -e "  ${_GREEN}[PASS]${_NC}  $LABEL"
    PASS_COUNT=$((PASS_COUNT + 1))
  fi
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Lambda
# Cleanup  : DeleteFunction (12 functions)
# Deploy 07: CreateFunction, UpdateFunctionCode, UpdateFunctionConfiguration,
#            PublishVersion, CreateAlias, UpdateAlias, AddPermission
# ─────────────────────────────────────────────────────────────────────────────
_section "Lambda"
_check "lambda:ListFunctions  (proxy for all Lambda actions)" "lambda" \
  aws lambda list-functions --max-items 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — DynamoDB
# Cleanup  : DeleteTable (6 tables)
# Deploy 04: CreateTable, UpdateTable
# ─────────────────────────────────────────────────────────────────────────────
_section "DynamoDB"
_check "dynamodb:ListTables  (proxy for all DynamoDB actions)" "dynamodb" \
  aws dynamodb list-tables --limit 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Secrets Manager
# Cleanup  : DeleteSecret (force-delete ota-signing-key)
# Deploy 02: CreateSecret, GetSecretValue
# ─────────────────────────────────────────────────────────────────────────────
_section "Secrets Manager"
_check "secretsmanager:ListSecrets  (proxy for all Secrets Manager actions)" "secretsmanager" \
  aws secretsmanager list-secrets --max-results 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — IAM
# Cleanup  : DeleteRole, DeleteRolePolicy, ListRolePolicies (3 roles)
# Deploy 05: CreateRole, PutRolePolicy, PassRole
# Note     : PassRole is critical but has no safe read proxy.
#            It is always included in the remediation policy if IAM checks fail.
# ─────────────────────────────────────────────────────────────────────────────
_section "IAM"
_check "iam:ListRoles  (proxy for all IAM role actions)" "iam" \
  aws iam list-roles --max-items 1
echo -e "  ${_YELLOW}[NOTE]${_NC}  iam:PassRole cannot be probed safely — included in remediation policy if IAM fails."

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — IoT Core
# Cleanup  : DeleteTopicRule (2), DeprecateThingType + DeleteThingType (5 types)
# Deploy 03: CreateThingType, CreateThingGroup, UpdateThingGroup, AddThingToThingGroup
# Deploy 08: CreateTopicRule, ReplaceTopicRule
# ─────────────────────────────────────────────────────────────────────────────
_section "IoT Core"
_check "iot:DescribeEndpoint" "iot" \
  aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION"
_check "iot:ListThingGroups" "iot" \
  aws iot list-thing-groups --max-results 1 --region "$REGION"
_check "iot:ListThingTypes" "iot" \
  aws iot list-thing-types --max-results 1 --region "$REGION"
_check "iot:ListTopicRules" "iot" \
  aws iot list-topic-rules --max-results 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — API Gateway
# Cleanup  : DELETE resources/methods/integrations under /api/v1/ota
# Deploy 09: GET + POST + PUT resources/methods/integrations + deployment
# ─────────────────────────────────────────────────────────────────────────────
_section "API Gateway"
if [[ -n "$API_GATEWAY_ID" ]]; then
  _check "apigateway:GET rest-api ($API_GATEWAY_ID)" "apigateway" \
    aws apigateway get-rest-api --rest-api-id "$API_GATEWAY_ID" --region "$REGION"
  _check "apigateway:GET resources" "apigateway" \
    aws apigateway get-resources --rest-api-id "$API_GATEWAY_ID" --limit 1 --region "$REGION"
else
  echo -e "  ${_YELLOW}[SKIP]${_NC}  API_GATEWAY_ID not set in deploy.config — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — CloudWatch Metrics
# Cleanup  : DeleteAlarms, DeleteDashboards
# Deploy 10: PutDashboard, PutMetricAlarm
# Deploy 12: PutMetricAlarm (DLQ alarms)
# ─────────────────────────────────────────────────────────────────────────────
_section "CloudWatch Metrics"
_check "cloudwatch:ListDashboards  (proxy for dashboard actions)" "cloudwatch" \
  aws cloudwatch list-dashboards --region "$REGION"
_check "cloudwatch:DescribeAlarms  (proxy for alarm actions)" "cloudwatch" \
  aws cloudwatch describe-alarms --max-records 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — CloudWatch Logs
# Cleanup  : DeleteLogGroup (/aws/lambda/ota_*, /ota/rule-errors)
# Deploy 08: CreateLogGroup, PutRetentionPolicy (IoT rule error log group)
# Deploy 07: CreateLogGroup is auto-created by Lambda runtime, but PutRetentionPolicy
#            is set explicitly in 07_deploy_lambdas.sh
# ─────────────────────────────────────────────────────────────────────────────
_section "CloudWatch Logs"
_check "logs:DescribeLogGroups  (proxy for all Logs actions)" "logs" \
  aws logs describe-log-groups --limit 1 --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — SNS
# Cleanup  : DeleteTopic (ota-alerts)
# Deploy 12: CreateTopic, Subscribe (alert email)
# ─────────────────────────────────────────────────────────────────────────────
_section "SNS"
_check "sns:ListTopics  (proxy for all SNS actions)" "sns" \
  aws sns list-topics --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — SQS
# Cleanup  : DeleteQueue (2 DLQs)
# Deploy 12: CreateQueue, SetQueueAttributes
# ─────────────────────────────────────────────────────────────────────────────
_section "SQS"
_check "sqs:ListQueues  (proxy for all SQS actions)" "sqs" \
  aws sqs list-queues --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11 — S3 (artifact bucket only — NOT deleting it)
# Cleanup  : HeadBucket (existence check — abort if missing)
# Deploy 11: GetBucketNotificationConfiguration, PutBucketNotificationConfiguration
# ─────────────────────────────────────────────────────────────────────────────
_section "S3 (artifact bucket: $ARTIFACT_BUCKET)"
_check "s3:HeadBucket" "s3" \
  aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --region "$REGION"
_check "s3:GetBucketNotificationConfiguration  (proxy for Put as well)" "s3" \
  aws s3api get-bucket-notification-configuration --bucket "$ARTIFACT_BUCKET" --region "$REGION"

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${_BOLD}${_BLUE}── Summary ──────────────────────────────────────────────────────────────${_NC}"
echo -e "  ${_GREEN}Passed :${_NC} $PASS_COUNT"
echo -e "  ${_RED}Failed :${_NC} $FAIL_COUNT"

if [[ $FAIL_COUNT -eq 0 ]]; then
  echo -e "\n  ${_GREEN}${_BOLD}All checks passed.${_NC}  You are ready to run cleanup + deploy."
  exit 0
fi

echo -e "\n  ${_RED}${_BOLD}Missing permissions detected.${_NC}  Generating remediation policy…"
echo -e "  Failed groups: ${FAILED_GROUPS[*]:-}"

# ─────────────────────────────────────────────────────────────────────────────
# POLICY GENERATION
# ─────────────────────────────────────────────────────────────────────────────
POLICY_FILE="${DIR}/preflight_missing_permissions.json"
FAILED_GROUPS_STR="${FAILED_GROUPS[*]:-}"

export ACCOUNT_ID REGION API_GATEWAY_ID ARTIFACT_BUCKET FAILED_GROUPS_STR

python3 - <<'PYEOF' > "$POLICY_FILE"
import json, os

account  = os.environ["ACCOUNT_ID"]
region   = os.environ["REGION"]
api_gw   = os.environ.get("API_GATEWAY_ID", "")
artifact = os.environ["ARTIFACT_BUCKET"]
failed   = set(os.environ.get("FAILED_GROUPS_STR", "").split())

statements = []

if "lambda" in failed:
    statements.append({
        "Sid": "LambdaDeploy",
        "Effect": "Allow",
        "Action": [
            "lambda:GetFunction",
            "lambda:ListFunctions",
            "lambda:CreateFunction",
            "lambda:UpdateFunctionCode",
            "lambda:UpdateFunctionConfiguration",
            "lambda:PublishVersion",
            "lambda:CreateAlias",
            "lambda:UpdateAlias",
            "lambda:GetAlias",
            "lambda:AddPermission",
            "lambda:GetPolicy",
            "lambda:DeleteFunction"
        ],
        "Resource": [
            f"arn:aws:lambda:{region}:{account}:function:ota_*",
            f"arn:aws:lambda:{region}:{account}:function:provisioning_validation"
        ]
    })

if "dynamodb" in failed:
    statements.append({
        "Sid": "DynamoDBDeploy",
        "Effect": "Allow",
        "Action": [
            "dynamodb:ListTables",
            "dynamodb:DescribeTable",
            "dynamodb:CreateTable",
            "dynamodb:UpdateTable",
            "dynamodb:DeleteTable"
        ],
        "Resource": [
            f"arn:aws:dynamodb:{region}:{account}:table/ota_*",
            f"arn:aws:dynamodb:{region}:{account}:table/ota_*/index/*"
        ]
    })

if "secretsmanager" in failed:
    statements.append({
        "Sid": "SecretsManagerDeploy",
        "Effect": "Allow",
        "Action": [
            "secretsmanager:ListSecrets",
            "secretsmanager:CreateSecret",
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
            "secretsmanager:DeleteSecret"
        ],
        "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:ota-*"
    })

if "iam" in failed:
    statements.append({
        "Sid": "IAMRolesDeploy",
        "Effect": "Allow",
        "Action": [
            "iam:ListRoles",
            "iam:GetRole",
            "iam:CreateRole",
            "iam:DeleteRole",
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:ListRolePolicies",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:PassRole"
        ],
        "Resource": [
            f"arn:aws:iam::{account}:role/ota-lambda-role",
            f"arn:aws:iam::{account}:role/ota-user-lambda-role",
            f"arn:aws:iam::{account}:role/ota-iot-rule-role",
            f"arn:aws:iam::{account}:role/provisioning-validation-role"
        ]
    })

if "iot" in failed:
    statements.append({
        "Sid": "IoTCoreDeploy",
        "Effect": "Allow",
        "Action": [
            "iot:DescribeEndpoint",
            "iot:ListThingGroups",
            "iot:ListThingTypes",
            "iot:ListTopicRules",
            "iot:CreateThingType",
            "iot:DescribeThingType",
            "iot:DeprecateThingType",
            "iot:DeleteThingType",
            "iot:CreateThingGroup",
            "iot:UpdateThingGroup",
            "iot:DescribeThingGroup",
            "iot:AddThingToThingGroup",
            "iot:CreateTopicRule",
            "iot:GetTopicRule",
            "iot:ReplaceTopicRule",
            "iot:DeleteTopicRule",
            "iot:CreateCloudWatchLogs"
        ],
        "Resource": "*"
    })

if "apigateway" in failed:
    base = (
        f"arn:aws:apigateway:{region}::/restapis/{api_gw}"
        if api_gw else
        f"arn:aws:apigateway:{region}::/restapis/*"
    )
    statements.append({
        "Sid": "APIGatewayDeploy",
        "Effect": "Allow",
        "Action": [
            "apigateway:GET",
            "apigateway:POST",
            "apigateway:PUT",
            "apigateway:PATCH",
            "apigateway:DELETE"
        ],
        "Resource": [base, base + "/*"]
    })

if "cloudwatch" in failed:
    statements.append({
        "Sid": "CloudWatchDeploy",
        "Effect": "Allow",
        "Action": [
            "cloudwatch:ListDashboards",
            "cloudwatch:PutDashboard",
            "cloudwatch:GetDashboard",
            "cloudwatch:DeleteDashboards",
            "cloudwatch:PutMetricAlarm",
            "cloudwatch:DescribeAlarms",
            "cloudwatch:DeleteAlarms"
        ],
        "Resource": "*"
    })

if "logs" in failed:
    statements.append({
        "Sid": "CloudWatchLogsDeploy",
        "Effect": "Allow",
        "Action": [
            "logs:DescribeLogGroups",
            "logs:CreateLogGroup",
            "logs:PutRetentionPolicy",
            "logs:DeleteLogGroup",
            "logs:CreateLogDelivery"
        ],
        "Resource": [
            f"arn:aws:logs:{region}:{account}:log-group:/ota/*",
            f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/ota_*",
            f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/provisioning_validation:*"
        ]
    })

if "sns" in failed:
    statements.append({
        "Sid": "SNSDeploy",
        "Effect": "Allow",
        "Action": [
            "sns:ListTopics",
            "sns:CreateTopic",
            "sns:Subscribe",
            "sns:DeleteTopic"
        ],
        "Resource": f"arn:aws:sns:{region}:{account}:ota-alerts"
    })

if "sqs" in failed:
    statements.append({
        "Sid": "SQSDeploy",
        "Effect": "Allow",
        "Action": [
            "sqs:ListQueues",
            "sqs:CreateQueue",
            "sqs:GetQueueUrl",
            "sqs:GetQueueAttributes",
            "sqs:SetQueueAttributes",
            "sqs:DeleteQueue"
        ],
        "Resource": f"arn:aws:sqs:{region}:{account}:ota_*-dlq"
    })

if "s3" in failed:
    statements.append({
        "Sid": "S3ArtifactBucketDeploy",
        "Effect": "Allow",
        "Action": [
            "s3:HeadBucket",
            "s3:GetBucketNotificationConfiguration",
            "s3:PutBucketNotificationConfiguration"
        ],
        "Resource": f"arn:aws:s3:::{artifact}"
    })

policy = {"Version": "2012-10-17", "Statement": statements}
print(json.dumps(policy, indent=2))
PYEOF

# ─────────────────────────────────────────────────────────────────────────────
# ATTACH INSTRUCTIONS
# ─────────────────────────────────────────────────────────────────────────────
POLICY_NAME="ota-deploy-permissions"

echo -e "\n${_BOLD}Remediation policy written to:${_NC}"
echo -e "  ${_YELLOW}${POLICY_FILE}${_NC}"

echo -e "\n${_BOLD}${_BLUE}── How to grant the missing permissions ─────────────────────────────────${_NC}"

if [[ "$IS_SSO_ROLE" == "true" ]]; then
  echo -e "  ${_YELLOW}You are using an SSO assumed role.${_NC}"
  echo -e "  A Honeywell AWS administrator must run the following commands."
  echo -e "  Target role: ${_BOLD}${ROLE_NAME}${_NC}"
  echo
  echo -e "  ${_BOLD}Step 1 — Upload the file to CloudShell / a machine with admin access.${_NC}"
  echo -e "  ${_BOLD}Step 2 — Create the managed policy:${_NC}"
  echo
  echo    "    aws iam create-policy \\"
  echo    "      --policy-name ${POLICY_NAME} \\"
  echo    "      --policy-document file://preflight_missing_permissions.json \\"
  echo    "      --region ${REGION}"
  echo
  echo -e "  ${_BOLD}Step 3 — Attach it to the SSO role:${_NC}"
  echo
  echo    "    aws iam attach-role-policy \\"
  echo    "      --role-name ${ROLE_NAME} \\"
  echo    "      --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
  echo
  echo -e "  ${_YELLOW}Note:${_NC} Steps 2–3 require iam:CreatePolicy + iam:AttachRolePolicy."
  echo -e "         If your SSO role lacks these, your Honeywell IAM admin must run them."
else
  # Plain IAM user or role
  if [[ "$PRINCIPAL_ARN" == *":role/"* ]]; then
    TARGET_NAME=$(echo "$PRINCIPAL_ARN" | sed 's|.*/||')
    echo -e "  ${_BOLD}Attach as an inline policy on your IAM role:${_NC}"
    echo
    echo    "    aws iam put-role-policy \\"
    echo    "      --role-name ${TARGET_NAME} \\"
    echo    "      --policy-name ${POLICY_NAME} \\"
    echo    "      --policy-document file://preflight_missing_permissions.json"
  else
    TARGET_NAME=$(echo "$PRINCIPAL_ARN" | sed 's|.*/||')
    echo -e "  ${_BOLD}Attach as an inline policy on your IAM user:${_NC}"
    echo
    echo    "    aws iam put-user-policy \\"
    echo    "      --user-name ${TARGET_NAME} \\"
    echo    "      --policy-name ${POLICY_NAME} \\"
    echo    "      --policy-document file://preflight_missing_permissions.json"
  fi
fi

echo
echo -e "  ${_BOLD}After permissions are granted, re-run this script to confirm all checks pass.${_NC}"

exit 1
