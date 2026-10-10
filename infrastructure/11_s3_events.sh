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

echo "==> Configuring S3 event notification (merge — preserves existing non-OTA notifications)"

# Our OTA notification entries — one per deviceType prefix.
# IDs are PREFIX-scoped so we can identify and replace them on re-run without
# touching any notification entries that belong to other systems.
#
# The artifact_processor writes outputs to enc/ and sig/ prefixes which do NOT
# match any of the prefixes below, so there is no risk of an infinite trigger loop.
OTA_ENTRIES=$(cat << EOF
[
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
EOF
)

# Fetch the existing notification config, strip out any stale OTA entries from
# previous runs (matched by our PREFIX), then merge the fresh OTA entries in.
# All other existing notification entries (TopicConfigurations, QueueConfigurations,
# other LambdaFunctionConfigurations from unrelated systems) are preserved exactly.
EXISTING_RAW=$(aws s3api get-bucket-notification-configuration \
  --bucket "$BUCKET" --region "$REGION" 2>/dev/null || echo '{}')

python3 - << PYEOF
import json, sys

existing = json.loads('''${EXISTING_RAW}''')
ota_entries = json.loads('''${OTA_ENTRIES}''')
prefix = "${PREFIX}"

# Remove stale OTA entries from previous runs (any Id starting with our prefix)
existing_lambda = existing.get("LambdaFunctionConfigurations", [])
kept = [e for e in existing_lambda if not e.get("Id", "").startswith(prefix + "-ota-")]

if kept:
    print(f"    Preserving {len(kept)} existing non-OTA LambdaFunctionConfiguration(s):")
    for e in kept:
        print(f"      - {e.get('Id','(no id)')}")
else:
    print("    No existing non-OTA LambdaFunctionConfigurations to preserve.")

# Merge: existing (non-OTA) + our fresh OTA entries
merged = existing.copy()
merged["LambdaFunctionConfigurations"] = kept + ota_entries

# Preserve any TopicConfigurations or QueueConfigurations untouched
for key in ("TopicConfigurations", "QueueConfigurations", "EventBridgeConfiguration"):
    if key in existing:
        print(f"    Preserving existing {key}.")

with open("/tmp/s3_notification.json", "w") as f:
    json.dump(merged, f, indent=2)

print(f"    Writing {len(merged['LambdaFunctionConfigurations'])} LambdaFunctionConfiguration(s) total.")
PYEOF

aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET" \
  --notification-configuration file:///tmp/s3_notification.json \
  --region "$REGION"
echo "    S3 event notifications configured."

echo ""
echo "S3 → Lambda pipeline ready."
echo "  Bucket: $BUCKET"
echo "  Processor: $PROCESSOR_FUNC"
