#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Digilux OTA — Zero-touch CDK deploy helper
#
# Usage:
#   bash deploy.sh                         # deploys 'digilux' environment
#   bash deploy.sh honeywell-prod          # deploys 'honeywell-prod' environment
#   bash deploy.sh honeywell-prod --diff   # preview changes only (no deploy)
#
# Prerequisites:
#   1. AWS CLI configured with credentials for the target account
#   2. Node.js + CDK CLI:  npm install -g aws-cdk
#   3. Python 3.11+
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV="${1:-digilux}"
DIFF_ONLY=false
[[ "${2:-}" == "--diff" ]] && DIFF_ONLY=true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  Digilux OTA CDK Deploy"
echo "  Environment : $ENV"
echo "  Diff only   : $DIFF_ONLY"
echo "============================================================"
echo ""

# ── Check prerequisites ───────────────────────────────────────────────────────
command -v cdk >/dev/null 2>&1 || {
  echo "ERROR: CDK CLI not found. Install with: npm install -g aws-cdk"
  exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found"; exit 1; }
command -v pip >/dev/null 2>&1 || { echo "ERROR: pip not found"; exit 1; }

echo "CDK version : $(cdk --version)"
echo "Python      : $(python3 --version)"
echo ""

# ── Install Python CDK dependencies ──────────────────────────────────────────
echo "==> Installing CDK Python dependencies..."
pip install -q -r requirements.txt
echo "    Done."
echo ""

# ── Load account + region from config ────────────────────────────────────────
CONFIG_FILE="config/${ENV}.json"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config not found: $CONFIG_FILE"
  echo "Available environments:"
  ls config/*.json | sed 's|config/||; s|\.json||'
  exit 1
fi

ACCOUNT=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['account'])")
REGION=$(python3 -c  "import json; c=json.load(open('$CONFIG_FILE')); print(c['region'])")
echo "Account : $ACCOUNT"
echo "Region  : $REGION"
echo ""

# ── CDK Bootstrap (idempotent — safe to run every time) ──────────────────────
echo "==> Bootstrapping CDK (idempotent)..."
cdk bootstrap "aws://${ACCOUNT}/${REGION}" --quiet
echo ""

# ── Synth — always run to validate before deploy ─────────────────────────────
echo "==> Synthesizing CloudFormation templates..."
cdk synth --all -c env="$ENV" --quiet
echo "    Synth OK."
echo ""

if $DIFF_ONLY; then
  echo "==> Diff (no changes will be deployed):"
  cdk diff --all -c env="$ENV"
  echo ""
  echo "Done. Run without --diff to deploy."
  exit 0
fi

# ── Deploy ────────────────────────────────────────────────────────────────────
echo "==> Deploying all stacks..."
cdk deploy --all -c env="$ENV" \
  --require-approval never \
  --outputs-file "outputs-${ENV}.json"

echo ""
echo "============================================================"
echo "  Deploy complete!"
echo "  Outputs saved to: cdk/outputs-${ENV}.json"
echo ""

# Print API URL
python3 -c "
import json, glob
files = glob.glob('outputs-${ENV}.json')
if files:
    data = json.load(open(files[0]))
    for stack, outputs in data.items():
        if 'ApiUrl' in outputs:
            print(f'  API Base URL : {outputs[\"ApiUrl\"]}api/v1/ota')
" 2>/dev/null || true

echo "============================================================"
