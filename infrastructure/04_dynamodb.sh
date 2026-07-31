#!/bin/bash
# Phase 4 — DynamoDB tables for OTA system
set -euo pipefail

REGION="ap-south-1"

create_table_if_missing() {
  local TABLE_NAME="$1"
  local DEFINITION="$2"

  if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" 2>/dev/null; then
    echo "    $TABLE_NAME already exists, skipping."
  else
    echo "    Creating $TABLE_NAME..."
    eval "aws dynamodb create-table --region $REGION $DEFINITION"
    aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
    echo "    $TABLE_NAME is ready."
  fi
}

# ── digilux_ota_packages ──────────────────────────────────────────────────────
# Stores package metadata: name, version, type, S3 key, hashes, compatibility
echo "==> digilux_ota_packages"
create_table_if_missing "digilux_ota_packages" \
  "--table-name digilux_ota_packages \
   --attribute-definitions \
     AttributeName=packageName,AttributeType=S \
     AttributeName=version,AttributeType=S \
   --key-schema \
     AttributeName=packageName,KeyType=HASH \
     AttributeName=version,KeyType=RANGE \
   --billing-mode PAY_PER_REQUEST \
   --tags Key=Project,Value=digilux Key=Component,Value=ota"

# ── digilux_device_inventory ──────────────────────────────────────────────────
# Per-device installed version registry; also stores thingName<->deviceId mapping
echo "==> digilux_device_inventory"
create_table_if_missing "digilux_device_inventory" \
  "--table-name digilux_device_inventory \
   --attribute-definitions \
     AttributeName=deviceId,AttributeType=S \
   --key-schema \
     AttributeName=deviceId,KeyType=HASH \
   --billing-mode PAY_PER_REQUEST \
   --tags Key=Project,Value=digilux Key=Component,Value=ota"

# ── digilux_ota_jobs ──────────────────────────────────────────────────────────
# Job tracking: status, rollout stage, per-device progress, audit trail
echo "==> digilux_ota_jobs"
create_table_if_missing "digilux_ota_jobs" \
  "--table-name digilux_ota_jobs \
   --attribute-definitions \
     AttributeName=jobId,AttributeType=S \
     AttributeName=createdAt,AttributeType=N \
   --key-schema \
     AttributeName=jobId,KeyType=HASH \
   --billing-mode PAY_PER_REQUEST \
   --global-secondary-indexes '[
     {
       \"IndexName\": \"createdAt-index\",
       \"KeySchema\": [
         {\"AttributeName\": \"jobId\", \"KeyType\": \"HASH\"},
         {\"AttributeName\": \"createdAt\", \"KeyType\": \"RANGE\"}
       ],
       \"Projection\": {\"ProjectionType\": \"ALL\"}
     }
   ]' \
   --tags Key=Project,Value=digilux Key=Component,Value=ota"

# ── digilux_ota_compatibility ─────────────────────────────────────────────────
# Compatibility matrix: which models/hw revisions can receive which package versions
echo "==> digilux_ota_compatibility"
create_table_if_missing "digilux_ota_compatibility" \
  "--table-name digilux_ota_compatibility \
   --attribute-definitions \
     AttributeName=packageName,AttributeType=S \
     AttributeName=version,AttributeType=S \
   --key-schema \
     AttributeName=packageName,KeyType=HASH \
     AttributeName=version,KeyType=RANGE \
   --billing-mode PAY_PER_REQUEST \
   --tags Key=Project,Value=digilux Key=Component,Value=ota"

echo ""
echo "All DynamoDB tables are ready."
