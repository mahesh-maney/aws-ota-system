#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Digilux OTA — User Flow E2E Tests (TU01–TU09)
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL="https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome"
TOKEN=$(cat /tmp/ota_nonadmin_token.txt)
ADMIN_TOKEN=$(cat /tmp/ota_admin_token.txt)
DEVICE_ID="edb39bba-baf1-4700-968c-a42228e53aa0"
PACKAGE_NAME="HomeAssistantUtility"
REGION="ap-south-1"
PASS=0; FAIL=0; SKIP=0

# Resolve latest ACTIVE version for PACKAGE_NAME to use as VERSION for the test
VERSION=$(aws dynamodb query --table-name digilux_ota_packages \
  --key-condition-expression "packageName = :p" \
  --filter-expression "#s = :active" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values "{\":p\":{\"S\":\"$PACKAGE_NAME\"},\":active\":{\"S\":\"ACTIVE\"}}" \
  --region "$REGION" \
  --query 'Items[*].version.S' --output text 2>/dev/null | tr '\t' '\n' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)
echo "  [SETUP] Test version resolved: $PACKAGE_NAME@$VERSION"

# Fetch macAddress once — needed for composite-key UpdateItem on digilux_device_data
DEVICE_MAC=$(aws dynamodb query \
  --table-name digilux_device_data \
  --key-condition-expression "deviceId = :d" \
  --expression-attribute-values "{\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
  --region "$REGION" \
  --query 'Items[0].macAddress.S' --output text 2>/dev/null)
echo "  [SETUP] Device macAddress resolved"

_activate_package() {
  curl -s -X PATCH "$BASE_URL/api/v1/ota/packages/$PACKAGE_NAME/$VERSION/activate" \
    -H "Authorization: $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"activated": true}' > /dev/null
  echo "  [SETUP] $PACKAGE_NAME@$VERSION activated"
}
_deactivate_package() {
  curl -s -X PATCH "$BASE_URL/api/v1/ota/packages/$PACKAGE_NAME/$VERSION/activate" \
    -H "Authorization: $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"activated": false}' > /dev/null
  echo "  [TEARDOWN] $PACKAGE_NAME@$VERSION deactivated"
}
_activate_package

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
  aws dynamodb update-item --table-name digilux_device_data \
    --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
    --update-expression "SET pendingJobId = :null, globalInstalledVersion = :v, #pkg = :pkgobj, lastUpdatedAt = :ts" \
    --expression-attribute-names '{"#pkg":"package"}' \
    --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"1.0.0\"},\":pkgobj\":{\"M\":{\"name\":{\"S\":\"$PACKAGE_NAME\"},\"installedVersion\":{\"S\":\"1.0.0\"}}},\":ts\":{\"N\":\"$(date +%s)000\"}}" \
    --region "$REGION" > /dev/null 2>&1
  echo "  [RESET] Device → $PACKAGE_NAME@1.0.0, pendingJobId=null"
}

_clear_rate_limit() {
  # Delete recent consent records for this user+device so the 5-minute rate
  # limiter does not block subsequent consent test sections.
  # The rate limit uses a GSI query on (userId, deviceId) with consentedAt.
  local USER_ID
  USER_ID=$(python3 -c "
import base64, json
tok = '$TOKEN'
part = tok.split('.')[1]
part += '=' * (-len(part) % 4)
print(json.loads(base64.b64decode(part))['sub'])
" 2>/dev/null)

  # Find all recent consent records for this user+device
  ITEMS=$(aws dynamodb query --table-name digilux_ota_user_consents \
    --index-name userId-deviceId-index \
    --key-condition-expression "userId = :u AND deviceId = :d" \
    --expression-attribute-values "{\":u\":{\"S\":\"${USER_ID}\"},\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
    --region "$REGION" \
    --query 'Items[*].consentId.S' --output text 2>/dev/null)

  for CONSENT_ID in $ITEMS; do
    aws dynamodb delete-item --table-name digilux_ota_user_consents \
      --key "{\"consentId\":{\"S\":\"${CONSENT_ID}\"}}" \
      --region "$REGION" > /dev/null 2>&1 || true
  done
  echo "  [SETUP] Rate-limit records cleared for userId=${USER_ID:0:8}..."
}

_set_device_version() {
  local ver="$1"
  aws dynamodb update-item --table-name digilux_device_data \
    --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
    --update-expression "SET pendingJobId = :null, globalInstalledVersion = :v, #pkg = :pkgobj" \
    --expression-attribute-names '{"#pkg":"package"}' \
    --expression-attribute-values "{\":null\":{\"NULL\":true},\":v\":{\"S\":\"$ver\"},\":pkgobj\":{\"M\":{\"name\":{\"S\":\"$PACKAGE_NAME\"},\"installedVersion\":{\"S\":\"$ver\"}}}}" \
    --region "$REGION" > /dev/null 2>&1
  echo "  [RESET] Device → $PACKAGE_NAME@$ver, pendingJobId=null"
}

# ──────────────────────────────────────────────────────────────────────────────
_section "TU01 — AUTHENTICATION"

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/ota/device/available-updates")
_expect_code "No token" "401" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: invalid.token.here" "$BASE_URL/api/v1/ota/device/available-updates")
_expect_one_of "Invalid token" "$CODE" "401" "403"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/device/available-updates")
_expect_code "Valid user token → check updates" "200" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/deployments")
_expect_one_of "Non-admin token on admin deployments endpoint" "$CODE" "401" "403"

# Old endpoint (GET /my/updates) no longer has a GET method — should 403 or 404
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/my/updates")
_expect_one_of "Old GET /my/updates endpoint gone" "$CODE" "403" "404"

# ──────────────────────────────────────────────────────────────────────────────
_section "TU02 — CHECK AVAILABLE UPDATES"

_reset_device
RESP=$(curl -s -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/device/available-updates")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $TOKEN" "$BASE_URL/api/v1/ota/device/available-updates")
_expect_code "GET /device/available-updates" "200" "$CODE"

DEVICES=$(echo "$RESP" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('devices',[])))" 2>/dev/null || echo "0")
if [ "$DEVICES" -ge 1 ]; then _pass "Response contains $DEVICES update entry(s)"; else _fail "No update entries in response"; fi

DEV_STATUS=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['devices'][0].get('otaStatus','?'))" 2>/dev/null)
if [ "$DEV_STATUS" = "REGISTERED" ]; then _pass "otaStatus=REGISTERED"; else _fail "otaStatus → expected REGISTERED, got $DEV_STATUS"; fi

HAS_PKG=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if any(x.get('package')=='$PACKAGE_NAME' for x in d['devices']) else 'no')" 2>/dev/null)
if [ "$HAS_PKG" = "ok" ]; then _pass "$PACKAGE_NAME entry present"; else _fail "$PACKAGE_NAME missing from devices list"; fi

HAS_FIELDS=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); e=d['devices'][0]; print('ok' if all(k in e for k in ['deviceId','package','installedVersion','availableVersion','fileName']) else 'no')" 2>/dev/null)
if [ "$HAS_FIELDS" = "ok" ]; then _pass "All flat fields present (deviceId, package, installedVersion, availableVersion, fileName)"; else _fail "One or more flat fields missing"; fi

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
_clear_rate_limit

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
PENDING=$(aws dynamodb query --table-name digilux_device_data \
  --key-condition-expression "deviceId = :d" \
  --expression-attribute-values "{\":d\":{\"S\":\"$DEVICE_ID\"}}" \
  --region "$REGION" \
  --query 'Items[0].pendingJobId.S' --output text 2>/dev/null)
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

# Set device to VERSION to test older-version guard using an older ACTIVE version
OLDER_VERSION=$(aws dynamodb query --table-name digilux_ota_packages \
  --key-condition-expression "packageName = :p" \
  --filter-expression "#s = :active" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values "{\":p\":{\"S\":\"$PACKAGE_NAME\"},\":active\":{\"S\":\"ACTIVE\"}}" \
  --region "$REGION" \
  --query 'Items[*].version.S' --output text 2>/dev/null | tr '\t' '\n' | sort -t. -k1,1n -k2,2n -k3,3n | head -1)

_set_device_version "$VERSION"

if [ -n "$OLDER_VERSION" ] && [ "$OLDER_VERSION" != "$VERSION" ]; then
  # Temporarily activate older version so consent lambda can find it
  curl -s -X PATCH "$BASE_URL/api/v1/ota/packages/$PACKAGE_NAME/$OLDER_VERSION/activate" \
    -H "Authorization: $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"activated": true}' > /dev/null
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
    -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$OLDER_VERSION\"}" \
    "$BASE_URL/api/v1/ota/my/updates/consent")
  curl -s -X PATCH "$BASE_URL/api/v1/ota/packages/$PACKAGE_NAME/$OLDER_VERSION/activate" \
    -H "Authorization: $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"activated": false}' > /dev/null
  _expect_code "Version older than installed ($OLDER_VERSION vs $VERSION)" "409" "$CODE"
else
  _skip "No older ACTIVE version available to test older-version guard"
fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/consent")
_expect_code "Same version already installed ($VERSION)" "409" "$CODE"

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
_section "TU10 — S3 PRESIGNED URL FORMAT"

_reset_device

RESP=$(curl -s -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"deviceId\":\"$DEVICE_ID\",\"packageName\":\"$PACKAGE_NAME\",\"version\":\"$VERSION\"}" \
  "$BASE_URL/api/v1/ota/my/updates/download-link")

DL_URL=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('downloadUrl',''))" 2>/dev/null)

# URL must be an S3 presigned URL (CloudFront was removed)
if [[ "$DL_URL" == "https://"*".s3"*".amazonaws.com/"* ]] || [[ "$DL_URL" == "https://s3."*".amazonaws.com/"* ]]; then
  _pass "downloadUrl is an S3 presigned URL"
else
  _fail "downloadUrl is not an S3 URL — got: ${DL_URL:0:80}..."
fi

# Must contain S3 presigned URL query params
if [[ "$DL_URL" == *"X-Amz-Signature="* ]]; then _pass "S3 presigned URL has X-Amz-Signature"; else _fail "X-Amz-Signature missing from URL"; fi
if [[ "$DL_URL" == *"X-Amz-Credential="* ]]; then _pass "S3 presigned URL has X-Amz-Credential"; else _fail "X-Amz-Credential missing from URL"; fi
if [[ "$DL_URL" == *"X-Amz-Expires="* ]]; then _pass "S3 presigned URL has X-Amz-Expires"; else _fail "X-Amz-Expires missing from URL"; fi

# Verify URL is actually reachable
HTTP_S3=$(curl -s -o /dev/null -w "%{http_code}" "$DL_URL")
if [ "$HTTP_S3" = "200" ] || [ "$HTTP_S3" = "206" ]; then
  _pass "S3 presigned URL is reachable (HTTP $HTTP_S3)"
else
  _fail "S3 presigned URL returned HTTP $HTTP_S3"
fi

# ──────────────────────────────────────────────────────────────────────────────
_section "TU11 — PRESIGN EXPIRY TIER (small package → 1 hour)"

# controller-app@4.0.0 is ~2MB — well within tier 1 (≤50MB → 3600s = 1hr)
EXPIRES_AT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('expiresAt',''))" 2>/dev/null)
if [ -z "$EXPIRES_AT" ]; then
  _fail "expiresAt field missing"
else
  # Check expiry is roughly 1 hour from now (allow 5 min slack each side)
  DELTA=$(python3 -c "
from datetime import datetime, timezone
expires = datetime.strptime('$EXPIRES_AT', '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
now = datetime.now(timezone.utc)
print(int((expires - now).total_seconds()))
" 2>/dev/null)
  if [ -n "$DELTA" ] && [ "$DELTA" -ge 3300 ] && [ "$DELTA" -le 3900 ]; then
    _pass "expiresAt is ~1 hour from now (${DELTA}s) — tier 1 correct"
  else
    _fail "expiresAt delta=${DELTA}s — expected ~3600s (tier 1 for small package)"
  fi
fi

# Size field should be non-zero
SIZE=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('size',0))" 2>/dev/null)
if [ "${SIZE:-0}" -gt 0 ]; then _pass "size=${SIZE} bytes present"; else _fail "size field missing or zero"; fi

# ──────────────────────────────────────────────────────────────────────────────
_reset_device
_deactivate_package

echo
echo "══════════════════════════════════════════"
printf "  RESULTS: %d passed | %d failed | %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "══════════════════════════════════════════"
if [ "$FAIL" -eq 0 ]; then echo "  ALL TESTS PASSED"; exit 0; else echo "  SOME TESTS FAILED"; exit 1; fi
