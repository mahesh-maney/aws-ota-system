#!/bin/bash
# Phase 4 — DynamoDB tables for OTA system
set -euo pipefail
export AWS_PAGER="" PAGER=cat
source "$(dirname "$0")/_lib.sh"

log_section "Phase 4: DynamoDB Tables"

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"

# Table names (derived from PREFIX unless overridden)
PACKAGES_TABLE="${PACKAGES_TABLE:-${PREFIX}_ota_packages}"
OTA_JOBS_TABLE="${OTA_JOBS_TABLE:-${PREFIX}_ota_jobs}"
COMPAT_TABLE="${COMPAT_TABLE:-${PREFIX}_ota_compatibility}"
DEPLOYMENTS_TABLE="${DEPLOYMENTS_TABLE:-${PREFIX}_ota_deployments}"
CONSENTS_TABLE="${CONSENTS_TABLE:-${PREFIX}_ota_user_consents}"
BETA_USERS_TABLE="${BETA_USERS_TABLE:-${PREFIX}_ota_beta_users}"

create_table_if_missing() {
  local TABLE_NAME="$1"
  shift
  if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" \
       2>/dev/null | grep -q '"TableStatus"'; then
    log_skip "$TABLE_NAME — already exists."
  else
    log_info "Creating $TABLE_NAME ..."
    aws dynamodb create-table --region "$REGION" --table-name "$TABLE_NAME" "$@" --no-cli-pager
    log_info "Waiting for $TABLE_NAME to become ACTIVE..."
    aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
    log_ok "$TABLE_NAME — ready."
  fi
}

# Adds a GSI to an existing table if the index doesn't already exist.
# This handles the case where Honeywell has a pre-existing table that is
# missing a GSI that the new Lambda code depends on.
add_gsi_if_missing() {
  local TABLE="$1" INDEX_NAME="$2"
  shift 2
  local EXISTS
  EXISTS=$(aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='$INDEX_NAME'].IndexName" \
    --output text 2>/dev/null || true)
  if [[ -n "$EXISTS" ]]; then
    log_skip "GSI $INDEX_NAME on $TABLE — already exists."
    return
  fi
  log_info "Adding GSI $INDEX_NAME to $TABLE ..."
  aws dynamodb update-table --table-name "$TABLE" --region "$REGION" "$@" --no-cli-pager
  # Wait for the GSI to become ACTIVE
  local ATTEMPTS=0
  while [[ $ATTEMPTS -lt 30 ]]; do
    local GSI_STATUS
    GSI_STATUS=$(aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" \
      --query "Table.GlobalSecondaryIndexes[?IndexName=='$INDEX_NAME'].IndexStatus" \
      --output text 2>/dev/null || true)
    [[ "$GSI_STATUS" == "ACTIVE" ]] && break
    log_info "  GSI status: $GSI_STATUS — waiting..."
    sleep 10
    (( ATTEMPTS++ )) || true
  done
  log_ok "GSI $INDEX_NAME added to $TABLE."
}

log_info "Tables to manage:"
log_info "  $PACKAGES_TABLE"
log_info "  $OTA_JOBS_TABLE"
log_info "  $COMPAT_TABLE"
log_info "  $DEPLOYMENTS_TABLE"
log_info "  $CONSENTS_TABLE"
log_info "  $BETA_USERS_TABLE"
log_info "  (${DEVICE_DATA_TABLE:-digilux_device_data} — pre-existing, not created here)"

# ── digilux_ota_packages ──────────────────────────────────────────────────────
log_step "$PACKAGES_TABLE"
create_table_if_missing "$PACKAGES_TABLE" \
  --attribute-definitions \
    AttributeName=packageName,AttributeType=S \
    AttributeName=version,AttributeType=S \
  --key-schema \
    AttributeName=packageName,KeyType=HASH \
    AttributeName=version,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

# ── digilux_ota_jobs ──────────────────────────────────────────────────────────
log_step "$OTA_JOBS_TABLE"
create_table_if_missing "$OTA_JOBS_TABLE" \
  --attribute-definitions \
    AttributeName=jobId,AttributeType=S \
    AttributeName=createdAt,AttributeType=N \
  --key-schema \
    AttributeName=jobId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[
    {
      "IndexName": "createdAt-index",
      "KeySchema": [
        {"AttributeName": "jobId",      "KeyType": "HASH"},
        {"AttributeName": "createdAt",  "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }
  ]'

# ── digilux_ota_compatibility ─────────────────────────────────────────────────
log_step "$COMPAT_TABLE"
create_table_if_missing "$COMPAT_TABLE" \
  --attribute-definitions \
    AttributeName=packageName,AttributeType=S \
    AttributeName=version,AttributeType=S \
  --key-schema \
    AttributeName=packageName,KeyType=HASH \
    AttributeName=version,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

# ── digilux_ota_deployments ───────────────────────────────────────────────────
# Campaign-level records: one per deployment action.
# GSIs:
#   packageName-status-index — query active deployments for a package
#   status-createdAt-index   — list all deployments sorted by time
log_step "$DEPLOYMENTS_TABLE"
create_table_if_missing "$DEPLOYMENTS_TABLE" \
  --attribute-definitions \
    AttributeName=deploymentId,AttributeType=S \
    AttributeName=packageName,AttributeType=S \
    AttributeName=status,AttributeType=S \
    AttributeName=createdAt,AttributeType=N \
  --key-schema \
    AttributeName=deploymentId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[
    {
      "IndexName": "packageName-status-index",
      "KeySchema": [
        {"AttributeName": "packageName", "KeyType": "HASH"},
        {"AttributeName": "status",      "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    },
    {
      "IndexName": "status-createdAt-index",
      "KeySchema": [
        {"AttributeName": "status",     "KeyType": "HASH"},
        {"AttributeName": "createdAt",  "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }
  ]'

# ── digilux_ota_user_consents ─────────────────────────────────────────────────
# Per-device consent records, one per (deployment, device).
# GSIs:
#   userId-deviceId-index   — find consents for a user's device
#   jobId-index             — look up consent by IoT job ID
#   deploymentId-index      — list all consents for a deployment
log_step "$CONSENTS_TABLE"
create_table_if_missing "$CONSENTS_TABLE" \
  --attribute-definitions \
    AttributeName=consentId,AttributeType=S \
    AttributeName=userId,AttributeType=S \
    AttributeName=deviceId,AttributeType=S \
    AttributeName=jobId,AttributeType=S \
    AttributeName=deploymentId,AttributeType=S \
  --key-schema \
    AttributeName=consentId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[
    {
      "IndexName": "userId-deviceId-index",
      "KeySchema": [
        {"AttributeName": "userId",   "KeyType": "HASH"},
        {"AttributeName": "deviceId", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    },
    {
      "IndexName": "jobId-index",
      "KeySchema": [
        {"AttributeName": "jobId", "KeyType": "HASH"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    },
    {
      "IndexName": "deploymentId-index",
      "KeySchema": [
        {"AttributeName": "deploymentId", "KeyType": "HASH"}
      ],
      "Projection": {"ProjectionType": "ALL"}
    }
  ]'

# ── digilux_ota_beta_users ────────────────────────────────────────────────────
# Allowlist for BETA rollout stage.
log_step "$BETA_USERS_TABLE"
create_table_if_missing "$BETA_USERS_TABLE" \
  --attribute-definitions \
    AttributeName=userId,AttributeType=S \
  --key-schema \
    AttributeName=userId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

# ── Ensure deploymentId-index GSI on consents table ──────────────────────────
# If Honeywell had a pre-existing consents table from an earlier deployment,
# it might be missing this GSI (added in the consent-gated OTA flow).
# add_gsi_if_missing is a no-op if the index already exists.
log_step "Ensuring deploymentId-index GSI on $CONSENTS_TABLE"
add_gsi_if_missing "$CONSENTS_TABLE" "deploymentId-index" \
  --attribute-definitions AttributeName=deploymentId,AttributeType=S \
  --global-secondary-index-updates '[{
    "Create": {
      "IndexName": "deploymentId-index",
      "KeySchema": [{"AttributeName":"deploymentId","KeyType":"HASH"}],
      "Projection": {"ProjectionType":"ALL"}
    }
  }]'

log_phase_done
echo ""
log_ok "All DynamoDB tables ready:"
log_ok "  $PACKAGES_TABLE"
log_ok "  $OTA_JOBS_TABLE"
log_ok "  $COMPAT_TABLE"
log_ok "  $DEPLOYMENTS_TABLE  (GSIs: packageName-status-index, status-createdAt-index)"
log_ok "  $CONSENTS_TABLE     (GSIs: userId-deviceId-index, jobId-index, deploymentId-index)"
log_ok "  $BETA_USERS_TABLE"
log_info "  (${DEVICE_DATA_TABLE:-digilux_device_data} — pre-existing, not created here)"
