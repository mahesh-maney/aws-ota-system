#!/bin/bash
# Phase 11 — S3 event notification → artifact processor Lambda
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

BUCKET="${ARTIFACT_BUCKET:-${PREFIX}-ota-artifacts}"
PROCESSOR_FUNC="${PREFIX}_ota_artifact_processor"
PROCESSOR_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PROCESSOR_FUNC}"

echo "==> S3 → Lambda: $BUCKET → $PROCESSOR_FUNC"

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

echo "==> Configuring S3 event notification"

# One notification entry per deviceType prefix — matches the S3 key structure
# written by the upload_url Lambda: {deviceType}/{packageName}/{version}/{fileName}
# The artifact_processor writes outputs to enc/ and sig/ which do NOT match these
# prefixes, so there is no risk of an infinite trigger loop.
cat > /tmp/s3_notification.json << EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "${PREFIX}-ota-artifact-nc-firmware",
      "LambdaFunctionArn": "${PROCESSOR_ARN}",
      "Events": ["s3:ObjectCreated:Put","s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix","Value": "Network_controller_firmware/"}]}}
    },
    {
      "Id": "${PREFIX}-ota-artifact-nc-zigbee",
      "LambdaFunctionArn": "${PROCESSOR_ARN}",
      "Events": ["s3:ObjectCreated:Put","s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix","Value": "Network_controller_zigbee_firmware/"}]}}
    },
    {
      "Id": "${PREFIX}-ota-artifact-nc-z2m",
      "LambdaFunctionArn": "${PROCESSOR_ARN}",
      "Events": ["s3:ObjectCreated:Put","s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix","Value": "Network_controller_Z2M_Firmware/"}]}}
    },
    {
      "Id": "${PREFIX}-ota-artifact-nc-zigbee-stack",
      "LambdaFunctionArn": "${PROCESSOR_ARN}",
      "Events": ["s3:ObjectCreated:Put","s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix","Value": "Network_controller_zigbee_stack_firmware/"}]}}
    },
    {
      "Id": "${PREFIX}-ota-artifact-nc-misc",
      "LambdaFunctionArn": "${PROCESSOR_ARN}",
      "Events": ["s3:ObjectCreated:Put","s3:ObjectCreated:CompleteMultipartUpload"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix","Value": "Network_controller_Miscellaneous/"}]}}
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
echo "S3 → Lambda pipeline ready."
echo "  Bucket: $BUCKET"
echo "  Processor: $PROCESSOR_FUNC"
