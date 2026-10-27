#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# validate_phase07.sh — Post-deploy smoke test for all OTA Lambda functions
#
# Run this immediately after phase 07 to verify every Lambda:
#   1. Loaded without PyArmor runtime errors (obfuscated deploy)
#   2. Executed and returned a meaningful response
#
# Usage:
#   bash infrastructure/validate_phase07.sh
#   bash infrastructure/validate_phase07.sh --config path/to.cfg
#
# Exit code: 0 = all passed, 1 = one or more failed
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
export AWS_PAGER="" PAGER=cat

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _RED='\033[0;31m'; _YELLOW='\033[1;33m'
_BLUE='\033[0;34m';  _BOLD='\033[1m';   _NC='\033[0m'
PASS="[${_GREEN}  PASS  ${_NC}]"
FAIL="[${_RED}  FAIL  ${_NC}]"
WARN="[${_YELLOW}  WARN  ${_NC}]"

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIG_FILE="${DIR}/deploy.config"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo -e "${_RED}ERROR:${_NC} Config file not found: $CONFIG_FILE"
  exit 1
fi
set -a; source "$CONFIG_FILE"; set +a

PREFIX="${PREFIX:-digilux}"
FUNC_PREFIX="${FUNC_PREFIX:-ota_}"   # Function name prefix. Use "digilux_ota_" to target the live deployment.
REGION="${REGION:-ap-south-1}"

echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD}  Phase 07 Validation — Lambda Smoke Tests${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "  Prefix      : ${_BOLD}${PREFIX}${_NC}"
echo -e "  Func prefix : ${_BOLD}${FUNC_PREFIX}${_NC}"
echo -e "  Region      : ${_BOLD}${REGION}${_NC}"
echo ""

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
RESULTS=()

# ── invoke_lambda ─────────────────────────────────────────────────────────────
# Invokes a Lambda with a payload, analyses response.
# A PASS means the Lambda loaded and ran (even a 4xx body is fine).
# A FAIL means the Lambda could not load (ImportModuleError = PyArmor issue).
invoke_lambda() {
  local SUFFIX="$1"
  local PAYLOAD="$2"
  local FUNC="${FUNC_PREFIX}${SUFFIX}"
  local OUT="/tmp/_validate_${FUNC}.json"

  printf "  %-45s " "$FUNC"

  # Invoke via prod alias
  local HTTP_STATUS
  HTTP_STATUS=$(aws lambda invoke \
    --function-name "$FUNC" \
    --qualifier prod \
    --payload "$PAYLOAD" \
    --cli-binary-format raw-in-base64-out \
    --region "$REGION" \
    --log-type None \
    "$OUT" \
    --query 'StatusCode' --output text 2>/dev/null) || HTTP_STATUS="ERROR"

  if [[ "$HTTP_STATUS" == "ERROR" ]]; then
    echo -e "$FAIL  Could not invoke — function may not exist or alias missing"
    RESULTS+=("FAIL:${FUNC}: invoke API error — check function exists and prod alias is set")
    (( FAIL_COUNT++ )) || true
    return
  fi

  local BODY=""
  [[ -f "$OUT" ]] && BODY=$(cat "$OUT") || BODY="{}"

  # Check for PyArmor / module load failure
  local ERROR_TYPE=""
  ERROR_TYPE=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('errorType',''))" 2>/dev/null) || ERROR_TYPE=""

  if echo "$ERROR_TYPE" | grep -q "ImportModuleError\|Runtime.ImportModuleError"; then
    local ERROR_MSG=""
    ERROR_MSG=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('errorMessage','')[:120])" 2>/dev/null) || true
    echo -e "$FAIL  Runtime.ImportModuleError — PyArmor runtime mismatch"
    RESULTS+=("FAIL:${FUNC}: ImportModuleError — ${ERROR_MSG}")
    (( FAIL_COUNT++ )) || true
    return
  fi

  if echo "$ERROR_TYPE" | grep -q "Runtime.ExitError\|Runtime.OutOfMemory"; then
    echo -e "$FAIL  ${ERROR_TYPE}"
    RESULTS+=("FAIL:${FUNC}: ${ERROR_TYPE}")
    (( FAIL_COUNT++ )) || true
    return
  fi

  # Get the body statusCode if this is an API Gateway response
  local BODY_STATUS=""
  BODY_STATUS=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('statusCode',''))" 2>/dev/null) || BODY_STATUS=""

  # Any of these are acceptable — Lambda ran successfully
  if [[ -z "$ERROR_TYPE" ]] || echo "$ERROR_TYPE" | grep -qv "Runtime\."; then
    if [[ -n "$BODY_STATUS" ]]; then
      echo -e "$PASS  HTTP ${BODY_STATUS}"
    else
      echo -e "$PASS  Executed (no HTTP wrapper)"
    fi
    RESULTS+=("PASS:${FUNC}")
    (( PASS_COUNT++ )) || true
  else
    # Some other handler error — Lambda loaded but logic error. Still a PASS for our purposes.
    local SHORT_ERR="${ERROR_TYPE:0:60}"
    echo -e "$WARN  Handler error (Lambda loaded OK): ${SHORT_ERR}"
    RESULTS+=("WARN:${FUNC}: handler error ${SHORT_ERR}")
    (( WARN_COUNT++ )) || true
  fi

  rm -f "$OUT"
}

# ── Test payloads ─────────────────────────────────────────────────────────────
# Strategy: use minimal payloads that trigger the Lambda handler.
# 4xx responses are fine — they prove the Lambda loaded and ran.
# We are NOT testing business logic here, only that the Lambda boots.

API_PROXY_GET='{"httpMethod":"GET","path":"/","headers":{"Authorization":"Bearer smoke-test-invalid"},"queryStringParameters":null,"body":null,"requestContext":{"authorizer":{"claims":{"sub":"smoke-test","email":"smoke@test.com","cognito:groups":"ota-admin"}}}}'

API_PROXY_POST='{"httpMethod":"POST","path":"/","headers":{"Authorization":"Bearer smoke-test-invalid","Content-Type":"application/json"},"queryStringParameters":null,"body":"{\"_smokeTest\":true}","requestContext":{"authorizer":{"claims":{"sub":"smoke-test","email":"smoke@test.com","cognito:groups":"ota-admin"}}}}'

IOT_STATUS_PAYLOAD='{"jobId":"smoke-test-job","thingName":"smoke-test-device","status":"IN_PROGRESS","statusDetails":{}}'

IOT_REGISTER_PAYLOAD='{"thingName":"smoke-test-device","certificates":{"certificateId":"smoke"},"package":{"name":"SmokeTest","version":"0.0.1"}}'

S3_EVENT_PAYLOAD='{"Records":[{"s3":{"bucket":{"name":"smoke-test-bucket"},"object":{"key":"pending/smoke-test.zip","size":100}},"eventName":"ObjectCreated:Put"}]}'

echo -e "${_BOLD}── Admin Lambdas ────────────────────────────────────────────────${_NC}"
invoke_lambda "upload_url"          "$API_PROXY_GET"
invoke_lambda "upload_complete"     "$API_PROXY_POST"
invoke_lambda "package_status"      "$API_PROXY_GET"
invoke_lambda "package_activate"    "$API_PROXY_POST"
invoke_lambda "artifact_processor"  "$S3_EVENT_PAYLOAD"
invoke_lambda "artifact_key"        "$API_PROXY_POST"
invoke_lambda "beta_users"          "$API_PROXY_GET"
invoke_lambda "job_create"          "$API_PROXY_POST"
invoke_lambda "job_sync"            "$API_PROXY_POST"
invoke_lambda "key_server"          "$API_PROXY_POST"
invoke_lambda "status_handler"      "$IOT_STATUS_PAYLOAD"
invoke_lambda "device_register"     "$IOT_REGISTER_PAYLOAD"

echo ""
echo -e "${_BOLD}── User Lambdas ─────────────────────────────────────────────────${_NC}"
invoke_lambda "user_check_updates"     "$API_PROXY_GET"
invoke_lambda "user_consent"           "$API_PROXY_POST"
invoke_lambda "user_update_status"     "$IOT_STATUS_PAYLOAD"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD}  Results: ${_GREEN}${PASS_COUNT} passed${_NC}  ${_RED}${FAIL_COUNT} failed${_NC}  ${_YELLOW}${WARN_COUNT} warnings${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"

if [[ $FAIL_COUNT -gt 0 ]]; then
  echo ""
  echo -e "${_RED}${_BOLD}  FAILURES — action required:${_NC}"
  for r in "${RESULTS[@]}"; do
    [[ "$r" == FAIL:* ]] && echo -e "  ${_RED}✖${_NC}  ${r#FAIL:}"
  done
  echo ""
  echo -e "${_BOLD}  Suggestions:${_NC}"
  for r in "${RESULTS[@]}"; do
    if [[ "$r" == *"ImportModuleError"* ]]; then
      echo -e "  ${_YELLOW}→${_NC}  PyArmor runtime mismatch detected."
      echo     "     Cause: obfuscation ran under a different Python version than Lambda (3.11)."
      echo     "     Fix:   ensure python3.11 is installed and re-run:"
      echo     "            OBFUSCATE=true bash infrastructure/deploy.sh --phase 07"
      break
    fi
  done
  for r in "${RESULTS[@]}"; do
    if [[ "$r" == *"invoke API error"* ]]; then
      echo -e "  ${_YELLOW}→${_NC}  One or more functions not found or prod alias missing."
      echo     "     Fix:   re-run phase 07 to create/update the function and alias:"
      echo     "            bash infrastructure/deploy.sh --phase 07"
      break
    fi
  done
fi

if [[ $WARN_COUNT -gt 0 ]]; then
  echo ""
  echo -e "${_YELLOW}${_BOLD}  WARNINGS — review if unexpected:${_NC}"
  for r in "${RESULTS[@]}"; do
    [[ "$r" == WARN:* ]] && echo -e "  ${_YELLOW}⚠${_NC}  ${r#WARN:}"
  done
  echo ""
  echo -e "  ${_YELLOW}→${_NC}  Handler errors on smoke payloads are normal (no real auth/data)."
  echo     "     If the same function fails on real traffic, check CloudWatch logs:"
  echo     "     aws logs tail /aws/lambda/<function_name> --follow --region ${REGION}"
fi

if [[ $FAIL_COUNT -eq 0 ]]; then
  echo ""
  echo -e "  ${_GREEN}✔${_NC}  All Lambdas loaded and executed successfully."
  if [[ $WARN_COUNT -eq 0 ]]; then
    echo -e "  ${_GREEN}✔${_NC}  No warnings. Deployment is healthy."
  fi
  echo ""
  echo -e "  ${_BOLD}Next step:${_NC} Proceed to end-to-end testing via the Admin UI."
fi

echo ""
echo -e "${_BOLD}════════════════════════════════════════════════════════════════${_NC}"

[[ $FAIL_COUNT -eq 0 ]]
