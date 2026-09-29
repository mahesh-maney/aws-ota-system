#!/usr/bin/env bash
# _lib.sh — Shared logging helpers for OTA deploy scripts
# Usage: source "$(dirname "$0")/_lib.sh"
# Must be sourced AFTER 'set -euo pipefail' in the calling script.

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then   # only add colour when writing to a terminal
  _RED='\033[0;31m' _GREEN='\033[0;32m' _YELLOW='\033[1;33m'
  _BLUE='\033[0;34m' _CYAN='\033[0;36m' _BOLD='\033[1m' _NC='\033[0m'
else
  _RED='' _GREEN='' _YELLOW='' _BLUE='' _CYAN='' _BOLD='' _NC=''
fi

# ── Timestamp ─────────────────────────────────────────────────────────────────
_ts() { date '+%H:%M:%S'; }

# ── Log functions ─────────────────────────────────────────────────────────────
log_info()  { echo -e "$(_ts) ${_BLUE}INFO ${_NC} $*"; }
log_ok()    { echo -e "$(_ts) ${_GREEN}OK   ${_NC} $*"; }
log_skip()  { echo -e "$(_ts) ${_YELLOW}SKIP ${_NC} $*"; }
log_warn()  { echo -e "$(_ts) ${_YELLOW}WARN ${_NC} $*" >&2; }
log_error() { echo -e "$(_ts) ${_RED}ERROR${_NC} $*" >&2; }
log_step()  { echo -e "$(_ts) ${_BOLD}-->  $*${_NC}"; }

# Prints a section banner
log_section() {
  echo ""
  echo -e "${_CYAN}────────────────────────────────────────────────────────────${_NC}"
  echo -e "${_CYAN}  $*${_NC}"
  echo -e "${_CYAN}────────────────────────────────────────────────────────────${_NC}"
}

# ── ERR trap ──────────────────────────────────────────────────────────────────
# Fires on any unhandled non-zero exit (respects set -e exclusions, so it
# will NOT fire inside if/while conditions or && / || lists).
_DEPLOY_SCRIPT_START=$(date +%s)

_on_error() {
  local LINE="$1" CMD="$2" SCRIPT="${BASH_SOURCE[1]:-${0}}"
  echo ""
  log_error "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log_error "Deployment failed!"
  log_error "  Script : $SCRIPT"
  log_error "  Line   : $LINE"
  log_error "  Command: $CMD"
  log_error "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "" >&2
  echo "Troubleshooting tips:" >&2
  echo "  1. Check the AWS CLI error message directly above this line." >&2
  echo "  2. Confirm your AWS credentials: aws sts get-caller-identity" >&2
  echo "  3. Re-run just this phase:  ./deploy.sh --phase $(basename "$SCRIPT" .sh | cut -c1-2)" >&2
  echo "" >&2
}
trap '_on_error $LINENO "$BASH_COMMAND"' ERR

# ── Phase summary ─────────────────────────────────────────────────────────────
# Call at the end of a phase script to print elapsed time.
log_phase_done() {
  local ELAPSED=$(( $(date +%s) - _DEPLOY_SCRIPT_START ))
  echo ""
  log_ok "Phase complete in ${ELAPSED}s."
}

# ── Prerequisite checker ──────────────────────────────────────────────────────
# Usage: require_cmd aws openssl python3 zip npm
require_cmd() {
  local MISSING=()
  for CMD in "$@"; do
    command -v "$CMD" &>/dev/null || MISSING+=("$CMD")
  done
  if [[ ${#MISSING[@]} -gt 0 ]]; then
    log_error "Missing required commands: ${MISSING[*]}"
    log_error "Install them and re-run deploy.sh"
    exit 1
  fi
}

# ── AWS helpers ───────────────────────────────────────────────────────────────
# Run an AWS command and retry once on transient throttle/rate-limit errors.
aws_retry() {
  local OUT ERR_FILE
  ERR_FILE=$(mktemp)
  if OUT=$(aws "$@" 2>"$ERR_FILE"); then
    rm -f "$ERR_FILE"
    echo "$OUT"
    return 0
  fi
  local ERR
  ERR=$(cat "$ERR_FILE"); rm -f "$ERR_FILE"
  # Retry on throttle
  if echo "$ERR" | grep -qi "throttling\|rate exceeded\|too many requests"; then
    log_warn "AWS throttle detected — retrying in 5s..."
    sleep 5
    aws "$@"
  else
    echo "$ERR" >&2
    return 1
  fi
}
