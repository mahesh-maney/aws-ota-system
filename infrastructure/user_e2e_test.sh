#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Digilux OTA — User Flow E2E Tests (TU01–TU09)
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL="https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome"
TOKEN=$(cat /tmp/ota_nonadmin_token.txt)
DEVICE_ID="edb39bba-baf1-4700-968c-a42228e53aa0"
PACKAGE_NAME="controller-app"
VERSION="4.0.0"
REGION="ap-south-1"
PASS=0; FAIL=0; SKIP=0

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
_skip() { echo "  [SKIP] $1"; SKIP=$((SKIP + 1)); }
_section() { echo; echo "══════════════════════════════════════════"; echo "  $1"; echo "══════════════════════════════════════════"; }
_expect_code() {
  local label="$1" expected="$2" got="$3"
  if [ "$got" = "$expected" ]; then _pass "$label → $got"; else _fail "$label → expected $expected, got $got"; fi
}
_expect_one_of() {
  local label="$1" got="$3"; shift 2
  for e in "$@"; do
    if [ "$got" = "$e" ]; then _pass "$label → $got"; return; fi
  done
  _fail "$label → expected one of [$*], got $got"
}

_reset_device() {
  aws dynamodb update-item --table-name digilux_device_inventory \
    --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"}}" \
    --update-expression "SET pendingJobId = :null, installedVersions.#pkg = :v, lastUpdatedAt = :ts" \
    --expression-attribute-names '{"#pkg":"controller-app"}' \
    --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"2.0.0\"},\":ts\":{\"N\":\"$(date +%s)000\"}}" \
    --region "$REGION" > /dev/null 2>&1
  echo "  [RESET] Device → controller-app@2.0.0, pendingJobId=null"
}

_set_device_version() {
  local ver="$1"
  aws dynamodb update-item --table-name digilux_device_inventory \
    --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"}}" \
    --update-expression "SET pendingJobId = :null, installedVersions.#pkg = :v" \
    --expression-attribute-names '{"#pkg":"controller-app"}' \
    --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"$ver\"}}" \
    --region "$REGION" > /dev/null 2>&1
  echo "  [RESET] Device → controller-app@$ver, pendingJobId=null"
}

# ──────────────────────────────────────────────────────────────────────────────
_section "TU01 — AUTHENTICATION"

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/ota/my/updates")
_expect_code "No token" "401" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: invalid.token.here" "$BASE_URL/api/v1/ota/my/updates")
_expect_one_of "Invalid token" "$CODE" "401" "403"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/my/updates")
_expect_code "Valid user token → check updates" "200" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/deployments")
_expect_code "Non-admin token on admin deployments endpoint" "403" "$CODE"

# ──────────────────────────────────────────────────────────────────────────────
_section "TU02 — CHECK MY UPDATES"

_reset_device
RESP=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/my/updates")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/my/updates")
_expect_code "GET /my/updates" "200" "$CODE"

DEVICES=$(echo "$RESP" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('devices',[])))" 2>/dev/null || echo "0")
if [ "$DEVICES" -ge 1 ]; then _pass "Response contains $DEVICES device(s)"; else _fail "No devices in response"; fi

HAS_TOTAL=$(echo "$RESP" | python3 -c "import json,sys; print('ok' if 'totalUpdates' in json.load(sys.stdin) else 'no')" 2>/dev/null)
if [ "$HAS_TOTAL" = "ok" ]; then _pass "totalUpdates field present"; else _fail "totalUpdates field missing"; fi

DEV_STATUS=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['devices'][0].get('otaStatus','?'))" 2>/dev/null)
if [ "$DEV_STATUS" = "REGISTERED" ]; then _pass "Device otaStatus=REGISTERED"; else _fail "otaStatus → expected REGISTERED, got $DEV_STATUS"; fi

UPDATE_COUNT=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['devices'][0].get('updateCount',0))" 2>/dev/null || echo "0")
if [ "$UPDATE_COUNT" -ge 1 ]; then _pass "updateCount=$UPDATE_COUNT (updates available)"; else _fail "updateCount=0 — no updates found (device may already be on latest)"; fi

HAS_PKG=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); u=d['devices'][0].get('availableUpdates',[]); print('ok' if any(x['packageName']=='controller-app' for x in u) else 'no')" 2>/dev/null)
if [ "$HAS_PKG" = "ok" ]; then _pass "controller-app in availableUpdates"; else _fail "controller-app missing from availableUpdates"; fi

# ──────────────────────────────────────────────────────────────────────────────
_section "TU03 — CONSENT INPUT VALIDATION"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Missing deviceId" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Missing packageName" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Missing version" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"not-a-uuid\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Invalid UUID format" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"version with spaces!\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Invalid version format" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"00000000-0000-0000-0000-000000000001\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Device not owned by user" "404" "$CODE"

# ──────────────────────────────────────────────────────────────────────────────
_section "TU04 — CONSENT HAPPY PATH"

_reset_device

BODY=$(curl -s -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  -w "\n%{http_code}" "$BASE_URL/api/v1/ota/my/updates/consent")
CODE=$(echo "$BODY" | tail -1)
RESP=$(echo "$BODY" | head -1)

_expect_code "Consent" "202" "$CODE"

JOB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)
if [ -n "$JOB_ID" ]; then _pass "jobId returned: $JOB_ID"; echo "$JOB_ID" > /tmp/ota_user_test_job_id.txt; else _fail "jobId missing"; fi

CONSENT_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('consentId',''))" 2>/dev/null)
if [ -n "$CONSENT_ID" ]; then _pass "consentId returned"; else _fail "consentId missing"; fi

JOB_STATUS=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
if [ "$JOB_STATUS" = "QUEUED" ]; then _pass "status=QUEUED"; else _fail "status → expected QUEUED, got $JOB_STATUS"; fi

sleep 1
PENDING=$(aws dynamodb get-item --table-name digilux_device_inventory \
  --key "{\"deviceId\":{\"S\":\"$DEVICE_ID\"}}" \
  --region "$REGION" \
  --query 'Item.pendingJobId.S' --output text 2>/dev/null)
if [ "$PENDING" = "$JOB_ID" ]; then _pass "DynamoDB pendingJobId=$JOB_ID"; else _fail "DynamoDB pendingJobId mismatch — got: $PENDING"; fi

# ──────────────────────────────────────────────────────────────────────────────
_section "TU05 — RATE LIMIT (DISABLED — RATE_LIMIT_MINUTES=0)"

_skip "Rate limit disabled for testing (RATE_LIMIT_MINUTES=0)"
_skip "429 response not expected until re-enabled"

# With rate limit off and pendingJobId set from TU04, second consent → 409 (pending guard)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Second consent with active job (pending guard takes precedence)" "409" "$CODE"

# ──────────────────────────────────────────────────────────────────────────────
_section "TU06 — PENDING JOB GUARD & VERSION CHECKS"

# pendingJobId still set from TU04 → 409
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Consent with active pendingJobId" "409" "$CODE"

ERR=$(curl -s -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
if [[ "$ERR" == *"already in progress"* ]]; then _pass "409 body mentions 'already in progress'"; else _fail "409 error body unexpected: $ERR"; fi

# Set device to 4.0.0 to test older-version guard
_set_device_version "4.0.0"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"3.0.0\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Version older than installed (3.0.0 vs 4.0.0)" "409" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"4.0.0\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Same version already installed (4.0.0)" "409" "$CODE"

_reset_device

# ──────────────────────────────────────────────────────────────────────────────
_section "TU07 — UPDATE STATUS TRACKING"

JOB_ID=$(cat /tmp/ota_user_test_job_id.txt 2>/dev/null || echo "")
if [ -z "$JOB_ID" ]; then
  _skip "No job_id from TU04 — creating fresh consent for status test"
  RESP=$(curl -s -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
    -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
    "$BASE_URL/api/v1/ota/my/updates/consent")
  JOB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)
  echo "$JOB_ID" > /tmp/ota_user_test_job_id.txt
fi

BODY=$(curl -s -w "\n%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/my/updates/$JOB_ID/status")
CODE=$(echo "$BODY" | tail -1)
RESP=$(echo "$BODY" | head -1)
_expect_code "GET /my/updates/$JOB_ID/status" "200" "$CODE"

HAS_STATUS=$(echo "$RESP" | python3 -c "import json,sys; print('ok' if 'status' in json.load(sys.stdin) else 'no')" 2>/dev/null)
if [ "$HAS_STATUS" = "ok" ]; then _pass "status field present"; else _fail "status field missing"; fi

STATUS_VAL=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
case "$STATUS_VAL" in
  QUEUED|IN_PROGRESS|SUCCEEDED|FAILED|CANCELLED) _pass "status=$STATUS_VAL (valid)" ;;
  *) _fail "status invalid: $STATUS_VAL" ;;
esac

HAS_MSG=$(echo "$RESP" | python3 -c "import json,sys; print('ok' if json.load(sys.stdin).get('statusMessage') else 'no')" 2>/dev/null)
if [ "$HAS_MSG" = "ok" ]; then _pass "statusMessage present"; else _fail "statusMessage missing"; fi

# Non-existent job → 404
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" \
  "$BASE_URL/api/v1/ota/my/updates/non-existent-job-id/status")
_expect_code "Non-existent job status" "404" "$CODE"

# ──────────────────────────────────────────────────────────────────────────────
_section "TU08 — DYNAMODB CONSENT RECORD"

JOB_ID=$(cat /tmp/ota_user_test_job_id.txt 2>/dev/null || echo "")
if [ -n "$JOB_ID" ]; then
  CONSENT_ITEM=$(aws dynamodb query --table-name digilux_ota_user_consents \
    --index-name jobId-index \
    --key-condition-expression "jobId = :jid" \
    --expression-attribute-values "{\":jid\":{\"S\":\"$JOB_ID\"}}" \
    --region "$REGION" \
    --query 'Items[0]' --output json 2>/dev/null)

  if [ "$CONSENT_ITEM" != "null" ] && [ -n "$CONSENT_ITEM" ]; then
    _pass "Consent record found in digilux_ota_user_consents"
  else
    _fail "Consent record missing for jobId=$JOB_ID"
  fi

  HAS_USER=$(echo "$CONSENT_ITEM" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('userId',{}).get('S') else 'no')" 2>/dev/null)
  if [ "$HAS_USER" = "ok" ]; then _pass "Consent record has userId"; else _fail "Consent record missing userId"; fi

  HAS_TS=$(echo "$CONSENT_ITEM" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('consentedAt') else 'no')" 2>/dev/null)
  if [ "$HAS_TS" = "ok" ]; then _pass "Consent record has consentedAt timestamp"; else _fail "Consent record missing consentedAt"; fi

  HAS_STATUS=$(echo "$CONSENT_ITEM" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('status') else 'no')" 2>/dev/null)
  if [ "$HAS_STATUS" = "ok" ]; then _pass "Consent record has status field"; else _fail "Consent record missing status"; fi
else
  _skip "No job_id — skipping DynamoDB consent checks"
fi

# ──────────────────────────────────────────────────────────────────────────────
_section "TU09 — DOWNLOAD LINK (APP-MEDIATED FLOW)"

_reset_device

BODY=$(curl -s -w "\n%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/download-link")
CODE=$(echo "$BODY" | tail -1)
RESP=$(echo "$BODY" | head -1)
_expect_code "GET download-link happy path" "200" "$CODE"

DL_URL=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('downloadUrl',''))" 2>/dev/null)
if [ -n "$DL_URL" ]; then _pass "downloadUrl present"; else _fail "downloadUrl missing"; fi

SHA=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('sha256',''))" 2>/dev/null)
if [ -n "$SHA" ]; then _pass "sha256 present: ${SHA:0:16}..."; else _fail "sha256 missing"; fi

SIG=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('signature',''))" 2>/dev/null)
if [ -n "$SIG" ]; then _pass "signature (ECDSA P-256) present"; else _fail "signature missing"; fi

MQTT_VAL=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('mqttDelivered','MISSING'))" 2>/dev/null)
if [ "$MQTT_VAL" != "MISSING" ]; then _pass "mqttDelivered=$MQTT_VAL"; else _fail "mqttDelivered field missing"; fi

HAS_EXPIRES=$(echo "$RESP" | python3 -c "import json,sys; print('ok' if json.load(sys.stdin).get('expiresAt') else 'no')" 2>/dev/null)
if [ "$HAS_EXPIRES" = "ok" ]; then _pass "expiresAt present"; else _fail "expiresAt missing"; fi

# Input validation
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/download-link")
_expect_code "download-link missing deviceId" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"00000000-0000-0000-0000-000000000001\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/download-link")
_expect_code "download-link unowned device" "404" "$CODE"

# version 1.0.0 doesn't exist in packages table → 404 from package lookup
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"1.0.0\"}" \
  "$BASE_URL/api/v1/ota/my/updates/download-link")
_expect_one_of "download-link non-existent version" "$CODE" "404" "409"

# ──────────────────────────────────────────────────────────────────────────────
_reset_device

echo
echo "══════════════════════════════════════════"
printf "  RESULTS: %d passed | %d failed | %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "══════════════════════════════════════════"
if [ "$FAIL" -eq 0 ]; then echo "  ALL TESTS PASSED"; exit 0; else echo "  SOME TESTS FAILED"; exit 1; fi
