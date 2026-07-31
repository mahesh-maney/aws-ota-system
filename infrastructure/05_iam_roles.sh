#!/bin/bash
# Phase 5 — IAM roles and policies for OTA Lambda functions and IoT rules
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME="digilux-ota-lambda-role"
RULE_ROLE_NAME="digilux-ota-iot-rule-role"

# ── Lambda execution role ─────────────────────────────────────────────────────
echo "==> Lambda execution role: $ROLE_NAME"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null; then
  echo "    Role already exists."
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "Execution role for Digilux OTA Lambda functions"
  echo "    Role created."
fi

echo "==> Attaching inline policy to $ROLE_NAME"
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "digilux-ota-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"logs:CreateLogGroup\",
          \"logs:CreateLogStream\",
          \"logs:PutLogEvents\"
        ],
        \"Resource\": \"arn:aws:logs:$REGION:$ACCOUNT_ID:log-group:/aws/lambda/digilux_ota_*\"
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\",
          \"dynamodb:PutItem\",
          \"dynamodb:UpdateItem\",
          \"dynamodb:DeleteItem\",
          \"dynamodb:Query\",
          \"dynamodb:Scan\"
        ],
        \"Resource\": [
          \"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/digilux_ota_*\",
          \"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/digilux_device_inventory\",
          \"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/digilux_device_data\"
        ]
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"s3:GetObject\",
          \"s3:PutObject\",
          \"s3:ListBucket\"
        ],
        \"Resource\": [
          \"arn:aws:s3:::digilux-ota-artifacts\",
          \"arn:aws:s3:::digilux-ota-artifacts/*\"
        ]
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"iot:CreateJob\",
          \"iot:DescribeJob\",
          \"iot:ListJobs\",
          \"iot:CancelJob\",
          \"iot:ListJobExecutionsForJob\",
          \"iot:ListJobExecutionsForThing\",
          \"iot:DescribeJobExecution\",
          \"iot:DescribeThing\",
          \"iot:ListThings\",
          \"iot:AddThingToThingGroup\",
          \"iot:RemoveThingFromThingGroup\",
          \"iot:DescribeThingGroup\",
          \"iot:GetThingShadow\",
          \"iot:UpdateThingShadow\",
          \"iot:SearchIndex\"
        ],
        \"Resource\": \"*\"
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"secretsmanager:GetSecretValue\"
        ],
        \"Resource\": \"arn:aws:secretsmanager:$REGION:$ACCOUNT_ID:secret:digilux-ota-*\"
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"cognito-idp:GetUser\",
          \"cognito-idp:AdminGetUser\",
          \"cognito-idp:AdminListGroupsForUser\"
        ],
        \"Resource\": \"arn:aws:cognito-idp:$REGION:$ACCOUNT_ID:userpool/ap-south-1_h1o8s7257\"
      }
    ]
  }"
echo "    Inline policy attached."

# ── IoT Rule execution role (to invoke Lambda) ────────────────────────────────
echo "==> IoT Rule execution role: $RULE_ROLE_NAME"

RULE_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "iot.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$RULE_ROLE_NAME" 2>/dev/null; then
  echo "    Role already exists."
else
  aws iam create-role \
    --role-name "$RULE_ROLE_NAME" \
    --assume-role-policy-document "$RULE_TRUST" \
    --description "IoT rule role to invoke Digilux OTA Lambda functions"
  echo "    Role created."
fi

aws iam put-role-policy \
  --role-name "$RULE_ROLE_NAME" \
  --policy-name "digilux-ota-rule-permissions" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": [
        \"arn:aws:lambda:$REGION:$ACCOUNT_ID:function:digilux_ota_status_handler\",
        \"arn:aws:lambda:$REGION:$ACCOUNT_ID:function:digilux_ota_device_register\"
      ]
    }]
  }"
echo "    Rule role policy attached."

echo ""
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
RULE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${RULE_ROLE_NAME}"
echo "Lambda role ARN : $LAMBDA_ROLE_ARN"
echo "Rule role ARN   : $RULE_ROLE_ARN"
echo ""
echo "IAM setup complete."
