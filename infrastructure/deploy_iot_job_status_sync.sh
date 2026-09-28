#!/usr/bin/env bash
# Thin wrapper — prefer deploy_delta.sh for all future deploys.
# Kept so existing notes still work.
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/deploy_delta.sh" digilux_ota_status_handler --iam "$@"
