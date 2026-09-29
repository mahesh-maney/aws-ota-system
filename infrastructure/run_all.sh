#!/bin/bash
# run_all.sh — Alias for deploy.sh (for backwards compatibility).
# New deployments should use deploy.sh which supports deploy.config.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$DIR/deploy.config" ]]; then
  exec bash "$DIR/deploy.sh" "$@"
fi

# ── Legacy mode: no deploy.config — run each script directly ──────────────────
# This preserves old behaviour for the Digilux dev environment where scripts
# read hardcoded values from ota.config.
echo "No deploy.config found — running in legacy mode."
echo "To use zero-touch deployment, copy deploy.config.template → deploy.config."
echo ""

export AWS_PAGER="" PAGER=cat

STEPS=(
  "01_s3.sh"
  "02_secrets.sh"
  "03_iot_setup.sh"
  "04_dynamodb.sh"
  "05_iam_roles.sh"
  "07_deploy_lambdas.sh"
  "08_iot_rules.sh"
  "09_api_gateway.sh"
  "10_cloudwatch.sh"
  "11_s3_events.sh"
  "12_production_hardening.sh"
)

for STEP in "${STEPS[@]}"; do
  echo "------------------------------------------------------------"
  echo " Running: $STEP"
  echo "------------------------------------------------------------"
  chmod +x "$DIR/$STEP"
  bash "$DIR/$STEP"
  echo ""
done

echo "========================================"
echo " Deployment Complete"
echo "========================================"
