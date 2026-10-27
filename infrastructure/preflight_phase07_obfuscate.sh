#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# preflight_phase07_obfuscate.sh — Pre-requisite check before running
#                                   OBFUSCATE=true deploy.sh --phase 07
#
# Run this BEFORE the obfuscated deploy to confirm the machine is ready.
#
# Usage:
#   bash infrastructure/preflight_phase07_obfuscate.sh
#   bash infrastructure/preflight_phase07_obfuscate.sh --config path/to.cfg
#
# Exit code: 0 = all checks passed, 1 = one or more failed
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _RED='\033[0;31m'; _YELLOW='\033[1;33m'
_BLUE='\033[0;34m';  _BOLD='\033[1m';   _NC='\033[0m'
OK="[${_GREEN}  OK   ${_NC}]"
FAIL="[${_RED}  FAIL ${_NC}]"
WARN="[${_YELLOW}  WARN ${_NC}]"

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

FAIL_COUNT=0
WARN_COUNT=0

pass() { echo -e "$OK  $*"; }
fail() { echo -e "$FAIL  $*"; (( FAIL_COUNT++ )) || true; }
warn() { echo -e "$WARN  $*"; (( WARN_COUNT++ )) || true; }

echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD}  Phase 07 Obfuscation — Pre-flight Checks${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo ""

# ── 1. deploy.config ─────────────────────────────────────────────────────────
echo -e "${_BOLD}── deploy.config ────────────────────────────────────────────────${_NC}"
if [[ ! -f "$CONFIG_FILE" ]]; then
  fail "deploy.config not found: $CONFIG_FILE"
  echo ""
  echo -e "  ${_YELLOW}→${_NC}  Copy the template and fill in all values:"
  echo     "        cp infrastructure/deploy.config.template infrastructure/deploy.config"
  echo ""
else
  pass "deploy.config found: $CONFIG_FILE"
  set -a; source "$CONFIG_FILE"; set +a

  # Check required keys
  for VAR in PREFIX REGION API_GATEWAY_ID API_GATEWAY_STAGE \
             ADMIN_COGNITO_AUTHORIZER_ID USER_COGNITO_AUTHORIZER_ID \
             USER_COGNITO_POOL_ID DEVICE_DATA_TABLE ALERT_EMAIL SES_SENDER_EMAIL; do
    if [[ -z "${!VAR:-}" || "${!VAR}" == YOUR_* ]]; then
      fail "$VAR is not set in deploy.config"
    else
      pass "$VAR = ${!VAR}"
    fi
  done
fi

echo ""

# ── 2. Required CLI tools ─────────────────────────────────────────────────────
echo -e "${_BOLD}── Required tools ───────────────────────────────────────────────${_NC}"
for CMD in aws zip; do
  if command -v "$CMD" &>/dev/null; then
    pass "$CMD found  ($(command -v $CMD))"
  else
    fail "$CMD not found — install it before proceeding"
  fi
done

echo ""

# ── 3. Python 3.11 ───────────────────────────────────────────────────────────
echo -e "${_BOLD}── Python 3.11 (required for PyArmor cp311 runtime) ─────────────${_NC}"
if command -v python3.11 &>/dev/null; then
  PY311_VERSION=$(python3.11 --version 2>&1)
  PY311_PATH=$(command -v python3.11)
  pass "python3.11 found  ($PY311_PATH)"
  pass "Version: $PY311_VERSION"

  # Check pip is available under python3.11
  if python3.11 -m pip --version &>/dev/null; then
    PIP311_VERSION=$(python3.11 -m pip --version 2>&1 | head -1)
    pass "pip (python3.11): $PIP311_VERSION"
  else
    fail "pip not available under python3.11"
    echo -e "  ${_YELLOW}→${_NC}  Run: python3.11 -m ensurepip --upgrade"
  fi

  # Check if PyArmor is already installed under python3.11
  PYARMOR_BIN="$(python3.11 -c 'import sys; print(sys.prefix)')/bin/pyarmor"
  if [[ -x "$PYARMOR_BIN" ]]; then
    PYARMOR_VERSION=$("$PYARMOR_BIN" --version 2>&1 | head -1)
    pass "PyArmor already installed: $PYARMOR_VERSION"
    pass "Executable: $PYARMOR_BIN"
  else
    warn "PyArmor not yet installed under python3.11 — will be auto-installed during deploy"
    echo -e "       To pre-install: python3.11 -m pip install pyarmor"
  fi

  # Test cross-platform generation works
  echo -e "  Testing PyArmor cross-platform generation (linux.x86_64)..."
  TEST_PY="/tmp/_pyarmor_preflight_test_$$.py"
  TEST_OUT="/tmp/_pyarmor_preflight_out_$$"
  echo "def hello(): pass" > "$TEST_PY"
  if [[ -x "$PYARMOR_BIN" ]]; then
    if "$PYARMOR_BIN" gen --platform linux.x86_64 --output "$TEST_OUT" "$TEST_PY" &>/dev/null; then
      if [[ -d "$TEST_OUT/pyarmor_runtime_000000" ]]; then
        pass "Cross-platform generation OK  (linux.x86_64 runtime generated)"
      else
        fail "PyArmor ran but pyarmor_runtime_000000 not found in output"
      fi
    else
      fail "PyArmor cross-platform generation failed"
      echo -e "  ${_YELLOW}→${_NC}  Try: $PYARMOR_BIN gen --platform linux.x86_64 <script.py>"
    fi
    rm -rf "$TEST_PY" "$TEST_OUT"
  else
    warn "Skipping cross-platform test — PyArmor not yet installed (will install during deploy)"
  fi
else
  fail "python3.11 not found"
  echo ""
  echo -e "  ${_YELLOW}→${_NC}  Install Python 3.11:"
  echo     "        macOS  : brew install python@3.11"
  echo     "        Ubuntu : sudo apt install python3.11"
  echo     "        Windows: download from https://www.python.org/downloads/release/python-3110/"
fi

echo ""

# ── 4. AWS credentials ────────────────────────────────────────────────────────
echo -e "${_BOLD}── AWS credentials ──────────────────────────────────────────────${_NC}"
if CALLER=$(aws sts get-caller-identity --output json 2>&1); then
  CALLER_ACCOUNT=$(echo "$CALLER" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
  CALLER_ARN=$(echo "$CALLER"     | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
  pass "AWS credentials valid"
  pass "Account : $CALLER_ACCOUNT"
  pass "Identity: $CALLER_ARN"

  # Verify account matches config
  EXPECTED_ACCOUNT="${ACCOUNT_ID:-}"
  if [[ -n "$EXPECTED_ACCOUNT" && "$CALLER_ACCOUNT" != "$EXPECTED_ACCOUNT" ]]; then
    fail "Account mismatch — logged in as $CALLER_ACCOUNT but deploy.config expects $EXPECTED_ACCOUNT"
    echo -e "  ${_YELLOW}→${_NC}  Check your AWS_PROFILE or AWS credentials"
  elif [[ -n "$EXPECTED_ACCOUNT" ]]; then
    pass "Account matches deploy.config ($EXPECTED_ACCOUNT)"
  else
    warn "ACCOUNT_ID not set in deploy.config — skipping account match check"
  fi

  # Verify region
  REGION="${REGION:-}"
  if [[ -n "$REGION" ]]; then
    pass "Target region: $REGION"
  else
    warn "REGION not set in deploy.config"
  fi
else
  fail "AWS credentials not configured or expired"
  echo ""
  echo -e "  ${_YELLOW}→${_NC}  Run: aws configure"
  echo     "     Or set: AWS_PROFILE / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
fi

echo ""

# ── 5. Lambda source files ────────────────────────────────────────────────────
echo -e "${_BOLD}── Lambda source files ──────────────────────────────────────────${_NC}"
LAMBDA_DIR="${DIR}/06_lambdas"
if [[ ! -d "$LAMBDA_DIR" ]]; then
  fail "Lambda source directory not found: $LAMBDA_DIR"
else
  pass "Lambda directory found: $LAMBDA_DIR"
  MISSING_LAMBDAS=0
  for SUFFIX in upload_url package_activate artifact_processor \
                job_create status_handler device_register \
                user_check_updates user_consent user_update_status; do
    SRC="${LAMBDA_DIR}/digilux_ota_${SUFFIX}/lambda_function.py"
    if [[ -f "$SRC" ]]; then
      pass "digilux_ota_${SUFFIX}/lambda_function.py"
    else
      fail "digilux_ota_${SUFFIX}/lambda_function.py not found"
      (( MISSING_LAMBDAS++ )) || true
    fi
  done
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
if [[ $FAIL_COUNT -eq 0 && $WARN_COUNT -eq 0 ]]; then
  echo -e "${_GREEN}${_BOLD}  All checks passed. Machine is ready.${_NC}"
  echo ""
  echo -e "  ${_BOLD}Run:${_NC}"
  echo     "    OBFUSCATE=true bash infrastructure/deploy.sh --phase 07"
elif [[ $FAIL_COUNT -eq 0 ]]; then
  echo -e "${_YELLOW}${_BOLD}  ${WARN_COUNT} warning(s) — review above, then proceed if acceptable.${_NC}"
  echo ""
  echo -e "  ${_BOLD}Run:${_NC}"
  echo     "    OBFUSCATE=true bash infrastructure/deploy.sh --phase 07"
else
  echo -e "${_RED}${_BOLD}  ${FAIL_COUNT} check(s) failed — fix the issues above before deploying.${_NC}"
fi
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo ""

[[ $FAIL_COUNT -eq 0 ]]
