#!/bin/bash
# Phase 11 — S3 event notification → artifact processor Lambda
set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="digilux-ota-artifacts"
PROCESSOR_FUNC="digilux_ota_artifact_processor"
PROCESSOR_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PROCESSOR_FUNC}"

echo "==> Granting S3 permission to invoke $PROCESSOR_FUNC"
aws lambda remove-permission \
  --function-name "$PROCESSOR_FUNC" \
  --statement-id "s3-invoke-artifact-processor" \
  --region "$REGION" 2>/dev/null || true

aws lambda add-permission \
  --function-name "$PROCESSOR_FUNC" \
  --statement-id "s3-invoke-artifact-processor" \
  --action "lambda:InvokeFunction" \
  --principal "s3.amazonaws.com" \
  --source-arn "arn:aws:s3:::${BUCKET}" \
  --source-account "$ACCOUNT_ID" \
  --region "$REGION" > /dev/null
echo "    Permission granted."

echo "==> Configuring S3 event notification on $BUCKET"

# Build notification config: trigger on PUT to all artifact prefixes
cat > /tmp/s3_notification.json << EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "digilux-ota-artifact-created",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {
              "Name": "prefix",
              "Value": "firmware/"
            }
          ]
        }
      }
    },
    {
      "Id": "digilux-ota-artifact-created-app",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "application/"}
          ]
        }
      }
    },
    {
      "Id": "digilux-ota-artifact-created-drivers",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "drivers/"}
          ]
        }
      }
    },
    {
      "Id": "digilux-ota-artifact-created-zigbee",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "zigbee-devices/"}
          ]
        }
      }
    },
    {
      "Id": "digilux-ota-artifact-created-config",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "config/"}
          ]
        }
      }
    },
    {
      "Id": "digilux-ota-artifact-created-rules",
      "LambdaFunctionArn": "$PROCESSOR_ARN",
      "Events": ["s3:ObjectCreated:Put", "s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {"Name": "prefix", "Value": "rules/"}
          ]
        }
      }
    }
  ]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET" \
  --notification-configuration file:///tmp/s3_notification.json \
  --region "$REGION"

echo "    S3 event notifications configured."
echo ""
echo "S3 → Lambda pipeline is ready."
echo ""
echo "Upload flow:"
echo "  1. POST /api/v1/ota/packages/upload-url  →  get pre-signed PUT URL"
echo "  2. PUT  <uploadUrl> with binary           →  S3 triggers processor Lambda"
echo "  3. Processor computes SHA256, signs, marks package ACTIVE automatically"
echo "  4. POST /api/v1/ota/deployments           →  deploy to devices"
