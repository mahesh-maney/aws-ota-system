#!/bin/bash
# Phase 5 — IAM roles and policies for OTA Lambda functions and IoT rules
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

LAMBDA_ROLE="${LAMBDA_ROLE_NAME:-${PREFIX}-ota-lambda-role}"
USER_ROLE="${USER_LAMBDA_ROLE_NAME:-${PREFIX}-ota-user-lambda-role}"
RULE_ROLE="${IOT_RULE_ROLE_NAME:-${PREFIX}-ota-iot-rule-role}"

ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"
SIGNING_SECRET="${SIGNING_SECRET:-${PREFIX}-ota-signing-key}"
DEVICE_DATA_TABLE="${DEVICE_DATA_TABLE:-${PREFIX}_device_data}"

USER_COGNITO_POOL_ID="${USER_COGNITO_POOL_ID:-}"

make_role() {
  local ROLE_NAME="$1"
  local PRINCIPAL="$2"
  local DESC="$3"
  local TRUST="{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Principal\": {\"Service\": \"${PRINCIPAL}\"},
      \"Action\": \"sts:AssumeRole\"
    }]
  }"
  if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null | grep -q '"RoleName"'; then
    echo "    $ROLE_NAME — already exists."
  else
    aws iam create-role \
      --role-name "$ROLE_NAME" \
      --assume-role-policy-document "$TRUST" \
      --description "$DESC"
    echo "    $ROLE_NAME — created."
  fi
}

# ── Admin Lambda execution role ───────────────────────────────────────────────
echo "==> Admin Lambda role: $LAMBDA_ROLE"
make_role "$LAMBDA_ROLE" "lambda.amazonaws.com" "Admin OTA Lambda execution role (${PREFIX})"

aws iam put-role-policy \
  --role-name "$LAMBDA_ROLE" \
  --policy-name "${PREFIX}-ota-admin-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"Logs\",
        \"Effect\": \"Allow\",
        \"Action\": [\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],
        \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${PREFIX}_ota_*\"
      },
      {
        \"Sid\": \"XRay\",
        \"Effect\": \"Allow\",
        \"Action\": [\"xray:PutTraceSegments\",\"xray:PutTelemetryRecords\"],
        \"Resource\": \"*\"
      },
      {
        \"Sid\": \"DynamoDB\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",
          \"dynamodb:DeleteItem\",\"dynamodb:Query\",\"dynamodb:Scan\"
        ],
        \"Resource\": [
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PREFIX}_ota_*\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PREFIX}_ota_*/index/*\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DEVICE_DATA_TABLE}\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DEVICE_DATA_TABLE}/index/*\"
        ]
      },
      {
        \"Sid\": \"S3Artifacts\",
        \"Effect\": \"Allow\",
        \"Action\": [\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\",\"s3:DeleteObject\"],
        \"Resource\": [
          \"arn:aws:s3:::${ARTIFACT_BUCKET}\",
          \"arn:aws:s3:::${ARTIFACT_BUCKET}/*\"
        ]
      },
      {
        \"Sid\": \"IoT\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"iot:CreateJob\",\"iot:DescribeJob\",\"iot:ListJobs\",\"iot:CancelJob\",
          \"iot:ListJobExecutionsForJob\",\"iot:ListJobExecutionsForThing\",
          \"iot:DescribeJobExecution\",\"iot:DescribeThing\",\"iot:ListThings\",
          \"iot:AddThingToThingGroup\",\"iot:RemoveThingFromThingGroup\",
          \"iot:ListThingGroupsForThing\",\"iot:DescribeThingGroup\",
          \"iot:GetThingShadow\",\"iot:UpdateThingShadow\",
          \"iot:SearchIndex\",\"iot:DescribeEndpoint\"
        ],
        \"Resource\": \"*\"
      },
      {
        \"Sid\": \"IoTJobUpdate\",
        \"Effect\": \"Allow\",
        \"Action\": [\"iot:UpdateJobExecution\",\"iotjobsdata:UpdateJobExecution\"],
        \"Resource\": [
          \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:job/${PREFIX}-ota-*\",
          \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:thing/*\"
        ]
      },
      {
        \"Sid\": \"Secrets\",
        \"Effect\": \"Allow\",
        \"Action\": \"secretsmanager:GetSecretValue\",
        \"Resource\": \"arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:${PREFIX}-ota-*\"
      },
      {
        \"Sid\": \"Cognito\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"cognito-idp:GetUser\",
          \"cognito-idp:AdminGetUser\",
          \"cognito-idp:AdminListGroupsForUser\"
        ],
        \"Resource\": \"arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${USER_COGNITO_POOL_ID}\"
      }
    ]
  }"
echo "    Admin policy attached."

# ── User Lambda execution role ────────────────────────────────────────────────
echo ""
echo "==> User Lambda role: $USER_ROLE"
make_role "$USER_ROLE" "lambda.amazonaws.com" "User OTA Lambda execution role (${PREFIX})"

aws iam put-role-policy \
  --role-name "$USER_ROLE" \
  --policy-name "${PREFIX}-ota-user-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"Logs\",
        \"Effect\": \"Allow\",
        \"Action\": [\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],
        \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${PREFIX}_ota_user_*\"
      },
      {
        \"Sid\": \"XRay\",
        \"Effect\": \"Allow\",
        \"Action\": [\"xray:PutTraceSegments\",\"xray:PutTelemetryRecords\"],
        \"Resource\": \"*\"
      },
      {
        \"Sid\": \"DeviceData\",
        \"Effect\": \"Allow\",
        \"Action\": [\"dynamodb:GetItem\",\"dynamodb:Query\",\"dynamodb:UpdateItem\"],
        \"Resource\": [
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DEVICE_DATA_TABLE}\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${DEVICE_DATA_TABLE}/index/*\"
        ]
      },
      {
        \"Sid\": \"OtaTables\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",
          \"dynamodb:DeleteItem\",\"dynamodb:Query\"
        ],
        \"Resource\": [
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PREFIX}_ota_*\",
          \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${PREFIX}_ota_*/index/*\"
        ]
      },
      {
        \"Sid\": \"IoT\",
        \"Effect\": \"Allow\",
        \"Action\": [\"iot:CreateJob\",\"iot:DescribeJob\",\"iot:DescribeEndpoint\"],
        \"Resource\": \"*\"
      },
      {
        \"Sid\": \"IoTJobTarget\",
        \"Effect\": \"Allow\",
        \"Action\": [\"iot:TagResource\"],
        \"Resource\": [
          \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:job/${PREFIX}-ota-*\",
          \"arn:aws:iot:${REGION}:${ACCOUNT_ID}:thing/*\"
        ]
      },
      {
        \"Sid\": \"S3Presign\",
        \"Effect\": \"Allow\",
        \"Action\": \"s3:GetObject\",
        \"Resource\": \"arn:aws:s3:::${ARTIFACT_BUCKET}/*\"
      },
      {
        \"Sid\": \"Secrets\",
        \"Effect\": \"Allow\",
        \"Action\": \"secretsmanager:GetSecretValue\",
        \"Resource\": \"arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:${PREFIX}-ota-*\"
      },
      {
        \"Sid\": \"Cognito\",
        \"Effect\": \"Allow\",
        \"Action\": [\"cognito-idp:GetUser\",\"cognito-idp:AdminGetUser\"],
        \"Resource\": \"arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${USER_COGNITO_POOL_ID}\"
      },
      {
        \"Sid\": \"SES\",
        \"Effect\": \"Allow\",
        \"Action\": \"ses:SendEmail\",
        \"Resource\": \"*\"
      }
    ]
  }"
echo "    User policy attached."

# ── IoT Rule execution role ───────────────────────────────────────────────────
echo ""
echo "==> IoT Rule role: $RULE_ROLE"
make_role "$RULE_ROLE" "iot.amazonaws.com" "IoT rule role to invoke OTA Lambdas (${PREFIX})"

aws iam put-role-policy \
  --role-name "$RULE_ROLE" \
  --policy-name "${PREFIX}-ota-rule-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Effect\": \"Allow\",
        \"Action\": \"lambda:InvokeFunction\",
        \"Resource\": [
          \"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}_ota_status_handler\",
          \"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}_ota_device_register\"
        ]
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],
        \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/${PREFIX}/ota/*\"
      }
    ]
  }"
echo "    Rule policy attached."

echo ""
echo "IAM setup complete."
echo "  Lambda admin role : arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"
echo "  Lambda user role  : arn:aws:iam::${ACCOUNT_ID}:role/${USER_ROLE}"
echo "  IoT rule role     : arn:aws:iam::${ACCOUNT_ID}:role/${RULE_ROLE}"
