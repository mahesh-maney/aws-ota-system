#!/bin/bash
# OTA End-to-End Production Test Suite
set -uo pipefail

TOKEN=$(cat /tmp/ota_admin_token.txt)
NON_ADMIN_TOKEN=$(cat /tmp/ota_nonadmin_token.txt)
PKCE_TOKEN=$(cat /tmp/ota_pkce_token.txt 2>/dev/null | tr -d '\n' || echo "")
BASE=$(cat /tmp/ota_base_url.txt)
REGION="ap-south-1"
DEVICE_ID="edb39bba-baf1-4700-968c-a42228e53aa0"

# Fetch the macAddress (sort key) for the test device once — needed for UpdateItem
# on digilux_device_data which has composite key (deviceId + macAddress).
DEVICE_MAC=$(aws dynamodb query \
  --table-name digilux_device_data \
  --key-condition-expression "deviceId = :d" \
  --expression-attribute-values "{\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
  --region "$REGION" \
  --query 'Items[0].macAddress.S' --output text 2>/dev/null)

PASS=0; FAIL=0; WARN=0
FAILED_TESTS=()

_pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
_fail() { echo "  ✗ FAIL: $1"; FAIL=$((FAIL+1)); FAILED_TESTS+=("$1"); }
_warn() { echo "  ⚠ WARN: $1"; WARN=$((WARN+1)); }
_section() { echo ""; echo "━━━ $1 ━━━"; }

call() {
  # call METHOD PATH [body]
  local method="$1" path="$2" body="${3:-}"
  local tok="${4:-$TOKEN}"
  if [ -n "$body" ]; then
    curl -s -X "$method" "${BASE}${path}" \
      -H "Authorization: ${tok}" -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s -X "$method" "${BASE}${path}" \
      -H "Authorization: ${tok}" -H "Content-Type: application/json"
  fi
}

http_code() {
  local method="$1" path="$2" body="${3:-}" tok="${4:-$TOKEN}"
  if [ -n "$body" ]; then
    curl -s -o /dev/null -w "%{http_code}" -X "$method" "${BASE}${path}" \
      -H "Authorization: ${tok}" -H "Content-Type: application/json" -d "$body"
  else
    curl -s -o /dev/null -w "%{http_code}" -X "$method" "${BASE}${path}" \
      -H "Authorization: ${tok}" -H "Content-Type: application/json"
  fi
}

assert_code() {
  local got="$1" expected="$2" label="$3"
  [ "$got" = "$expected" ] && _pass "$label (HTTP $got)" || _fail "$label — expected HTTP $expected, got $got"
}

assert_field() {
  local json="$1" field="$2" expected="$3" label="$4"
  local got
  got=$(echo "$json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$field','__MISSING__'))" 2>/dev/null)
  [ "$got" = "$expected" ] && _pass "$label ($field=$got)" || _fail "$label — expected $field=$expected, got $got"
}

assert_has_field() {
  local json="$1" field="$2" label="$3"
  local got
  got=$(echo "$json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('yes' if '$field' in d else 'no')" 2>/dev/null)
  [ "$got" = "yes" ] && _pass "$label (has field: $field)" || _fail "$label — missing field: $field"
}

# ─────────────────────────────────────────────────────────────────────────────
_section "T01 — AUTHENTICATION"
# ─────────────────────────────────────────────────────────────────────────────

code=$(http_code GET "/api/v1/ota/packages" "" "$NON_ADMIN_TOKEN")
# Admin routes use the OTA admin pool authorizer — tokens from the app pool are rejected
# at API Gateway level with 401 (not 403 from Lambda) because they come from a different pool.
assert_code "$code" "401" "Non-admin token rejected on GET /packages"

code=$(http_code GET "/api/v1/ota/packages")
assert_code "$code" "200" "Admin token accepted on GET /packages"

code=$(http_code GET "/api/v1/ota/packages" "" "Bearer invalid.token.here")
[ "$code" = "401" ] || [ "$code" = "403" ] \
  && _pass "Invalid token rejected (HTTP $code)" \
  || _fail "Invalid token should be 401/403, got $code"

# ─────────────────────────────────────────────────────────────────────────────
_section "T02 — INPUT VALIDATION: upload-artefact"
# ─────────────────────────────────────────────────────────────────────────────

code=$(http_code POST "/api/v1/ota/packages/upload-artefact" '{"version":"1.0.0","releaseType":"PROD"}')
assert_code "$code" "400" "Missing deviceType → 400"

code=$(http_code POST "/api/v1/ota/packages/upload-artefact" '{"deviceType":"Network_controller_firmware","releaseType":"PROD"}')
assert_code "$code" "400" "Missing version → 400"

code=$(http_code POST "/api/v1/ota/packages/upload-artefact" '{"deviceType":"Network_controller_firmware","version":"1.0.0"}')
assert_code "$code" "400" "Missing releaseType → 400"

code=$(http_code POST "/api/v1/ota/packages/upload-artefact" '{"deviceType":"INVALID_DEVICE","version":"1.0.0","releaseType":"PROD"}')
assert_code "$code" "400" "Invalid deviceType → 400"

# ─────────────────────────────────────────────────────────────────────────────
_section "T03 — INPUT VALIDATION: deployments"
# ─────────────────────────────────────────────────────────────────────────────

code=$(http_code POST "/api/v1/ota/deployments" '{"version":"1.0.0","targetType":"THING","targetId":"x"}')
assert_code "$code" "400" "Missing packageName → 400"

code=$(http_code POST "/api/v1/ota/deployments" '{"packageName":"x","version":"1.0.0","targetType":"INVALID","targetId":"x"}')
assert_code "$code" "400" "Invalid targetType → 400"

code=$(http_code POST "/api/v1/ota/deployments" '{"packageName":"nonexistent-pkg","version":"9.9.9","targetType":"THING","targetId":"x"}')
assert_code "$code" "404" "Non-existent package → 404"

# Note: Re-deploy of already-installed version is tested after T10 where the
# package name and installed version are both known (see end of T10 section).

# ─────────────────────────────────────────────────────────────────────────────
_section "T04 — INPUT VALIDATION: compatibility check"
# ─────────────────────────────────────────────────────────────────────────────

code=$(http_code GET "/api/v1/controllers/nonexistent-device-id/updates/available")
assert_code "$code" "404" "Unknown deviceId → 404"

code=$(http_code GET "/api/v1/controllers/${DEVICE_ID}/updates/available")
assert_code "$code" "200" "Known deviceId → 200"

# ─────────────────────────────────────────────────────────────────────────────
_section "T05 — PACKAGE UPLOAD FLOW"
# ─────────────────────────────────────────────────────────────────────────────

# Use a unique version to avoid collision
TEST_VERSION="5.0.$(date +%s)-$RANDOM"

# Generate test artifact — must be a valid tar containing manifest.json
# artifact_processor validates tar structure before promoting PENDING → ACTIVE
_TEST_DIR=$(mktemp -d)
cat > "$_TEST_DIR/manifest.json" <<MANIFEST_EOF
{"packageName":"HomeAssistantUtility","version":"${TEST_VERSION}","files":[{"name":"payload.bin","type":1}]}
MANIFEST_EOF
echo "e2e test payload $(date)" > "$_TEST_DIR/payload.bin"
tar -czf /tmp/test_artifact.bin -C "$_TEST_DIR" manifest.json payload.bin
rm -rf "$_TEST_DIR"
TEST_CHECKSUM=$(sha256sum /tmp/test_artifact.bin | awk '{print $1}')
echo "  → test artifact SHA256: ${TEST_CHECKSUM:0:16}..."

UPLOAD_RESP=$(call POST "/api/v1/ota/packages/upload-artefact" \
  "{\"deviceType\":\"Network_controller_firmware\",\"version\":\"${TEST_VERSION}\",\"releaseType\":\"PROD\",\"checksum\":\"${TEST_CHECKSUM}\",\"releaseNotes\":\"E2E test package\"}")

# Extract HTTP code from UPLOAD_RESP directly — do NOT make a second call
# (a second call would overwrite the uploadToken in DynamoDB, causing a token mismatch)
UPLOAD_CODE=$(echo "$UPLOAD_RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
# If it has uploadUrl it's a 200; if it has 'message' or 'error' it failed
print('200' if 'uploadUrl' in d else '400')
" 2>/dev/null || echo "500")
assert_code "$UPLOAD_CODE" "200" "Upload URL request returns 200"

assert_field "$UPLOAD_RESP" "status" "PENDING" "Package starts as PENDING"
assert_has_field "$UPLOAD_RESP" "uploadUrl" "Response has uploadUrl"
assert_has_field "$UPLOAD_RESP" "s3Key" "Response has s3Key"
assert_has_field "$UPLOAD_RESP" "uploadToken" "Response has uploadToken"

# Derive packageName, uploadUrl, uploadToken from response
TEST_PKG_NAME=$(echo "$UPLOAD_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('packageName',''))")
UPLOAD_URL=$(echo "$UPLOAD_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadUrl',''))")
S3_KEY=$(echo "$UPLOAD_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('s3Key',''))")
UPLOAD_TOKEN=$(echo "$UPLOAD_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadToken',''))")
echo "  → packageName: $TEST_PKG_NAME  s3Key: $S3_KEY"

# PUT binary to S3 — must include x-amz-meta-upload-token (baked into presigned URL signature)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$UPLOAD_URL" \
  -H "Content-Type: application/octet-stream" \
  -H "x-amz-meta-upload-token: ${UPLOAD_TOKEN}" \
  --data-binary @/tmp/test_artifact.bin)
assert_code "$HTTP_CODE" "200" "Binary PUT to S3 pre-signed URL → 200"

# Wait for artifact processor (S3 event → Lambda → ACTIVE)
echo "  → Waiting for artifact_processor (up to 30s)..."
for i in $(seq 1 30); do
  sleep 1
  STATUS=$(aws dynamodb get-item \
    --table-name digilux_ota_packages \
    --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION}\"}}" \
    --region "$REGION" \
    --query 'Item.status.S' --output text 2>/dev/null)
  [ "$STATUS" = "ACTIVE" ] && break
done
[ "$STATUS" = "ACTIVE" ] \
  && _pass "Package auto-promoted to ACTIVE in ${i}s (S3 event → artifact_processor)" \
  || _fail "Package not ACTIVE after 30s — status=$STATUS"

# Fetch full package record once — reuse for all assertions
PKG_ITEM=$(aws dynamodb get-item \
  --table-name digilux_ota_packages \
  --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION}\"}}" \
  --region "$REGION" --output json 2>/dev/null)

SHA256=$(echo "$PKG_ITEM"   | python3 -c "import json,sys; print(json.load(sys.stdin).get('Item',{}).get('sha256',{}).get('S',''))" 2>/dev/null)
SIG=$(echo "$PKG_ITEM"      | python3 -c "import json,sys; print(json.load(sys.stdin).get('Item',{}).get('signature',{}).get('S',''))" 2>/dev/null)
ENC_KEY=$(echo "$PKG_ITEM"  | python3 -c "import json,sys; print(json.load(sys.stdin).get('Item',{}).get('encS3Key',{}).get('S',''))" 2>/dev/null)
SIG_KEY=$(echo "$PKG_ITEM"  | python3 -c "import json,sys; print(json.load(sys.stdin).get('Item',{}).get('sigS3Key',{}).get('S',''))" 2>/dev/null)

# (+) sha256 and ECDSA signature written
[ -n "$SHA256" ] && [ "$SHA256" != "None" ] \
  && _pass "SHA256 written by artifact_processor: ${SHA256:0:16}..." \
  || _fail "SHA256 missing after ACTIVE promotion"
[ -n "$SIG" ] && [ "$SIG" != "None" ] \
  && _pass "ECDSA signature written by artifact_processor" \
  || _fail "Signature missing after ACTIVE promotion"

# (+) encS3Key stored and follows opaque UUID format enc/<uuid>.enc
[ -n "$ENC_KEY" ] && [ "$ENC_KEY" != "None" ] \
  && _pass "encS3Key written by artifact_processor: $ENC_KEY" \
  || _fail "encS3Key missing after ACTIVE promotion"

echo "$ENC_KEY" | python3 -c "
import sys, re
key = sys.stdin.read().strip()
pat = r'^enc/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.enc$'
sys.exit(0 if re.match(pat, key) else 1)
" 2>/dev/null \
  && _pass "encS3Key is opaque UUID format (enc/<uuid>.enc)" \
  || _fail "encS3Key is NOT in UUID format — URL masking may be broken: $ENC_KEY"

# (+) sigS3Key stored and follows opaque UUID format sig/<uuid>.sig
[ -n "$SIG_KEY" ] && [ "$SIG_KEY" != "None" ] \
  && _pass "sigS3Key written by artifact_processor: $SIG_KEY" \
  || _fail "sigS3Key missing after ACTIVE promotion"

echo "$SIG_KEY" | python3 -c "
import sys, re
key = sys.stdin.read().strip()
pat = r'^sig/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sig$'
sys.exit(0 if re.match(pat, key) else 1)
" 2>/dev/null \
  && _pass "sigS3Key is opaque UUID format (sig/<uuid>.sig)" \
  || _fail "sigS3Key is NOT in UUID format: $SIG_KEY"

# (-) encS3Key must NOT leak package name, version, or device type
echo "$ENC_KEY" | python3 -c "
import sys
key = sys.stdin.read().strip().lower()
leaks = ['homeassistantutility', 'network_controller', '${TEST_PKG_NAME}'.lower(), '${TEST_VERSION}'.lower()]
found = [l for l in leaks if l in key]
sys.exit(1 if found else 0)
" 2>/dev/null \
  && _pass "encS3Key leaks no package name / version / device type" \
  || _fail "encS3Key leaks sensitive info in path: $ENC_KEY"

# (-) encS3Key and sigS3Key must be different UUIDs (each artifact gets its own key)
[ "$ENC_KEY" != "$SIG_KEY" ] \
  && _pass "encS3Key and sigS3Key are distinct UUIDs" \
  || _fail "encS3Key and sigS3Key are identical — UUID generation may be broken"

# Duplicate upload of same ACTIVE version → 409
code=$(http_code POST "/api/v1/ota/packages/upload-artefact" \
  "{\"deviceType\":\"Network_controller_firmware\",\"version\":\"${TEST_VERSION}\",\"releaseType\":\"PROD\",\"checksum\":\"${TEST_CHECKSUM}\"}")
assert_code "$code" "409" "Duplicate ACTIVE version upload → 409"

echo "TEST_VERSION=$TEST_VERSION" > /tmp/ota_test_version.txt
echo "TEST_PKG_NAME=$TEST_PKG_NAME" >> /tmp/ota_test_version.txt

# ─────────────────────────────────────────────────────────────────────────────
_section "T06 — LIST PACKAGES"
# ─────────────────────────────────────────────────────────────────────────────

LIST=$(call GET "/api/v1/ota/packages")
COUNT=$(echo "$LIST" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))")
[ "$COUNT" -gt 0 ] && _pass "GET /packages returns $COUNT packages" || _fail "GET /packages returned 0 packages"

LIST_FILTERED=$(call GET "/api/v1/ota/packages?deviceType=Network_controller_firmware")
assert_has_field "$LIST_FILTERED" "packages" "Filter by deviceType returns packages array"

# ─────────────────────────────────────────────────────────────────────────────
_section "T07 — COMPATIBILITY CHECK"
# ─────────────────────────────────────────────────────────────────────────────

COMPAT=$(call GET "/api/v1/controllers/${DEVICE_ID}/updates/available")
assert_has_field "$COMPAT" "availableUpdates" "Compatibility response has availableUpdates"
assert_has_field "$COMPAT" "installedVersions" "Compatibility response has installedVersions"
assert_has_field "$COMPAT" "pendingJobId" "Compatibility response has pendingJobId"

CURR_VER=$(echo "$COMPAT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('installedVersions',{}).get('controller-app','none'))")
echo "  → Device installed controller-app: $CURR_VER"

# ─────────────────────────────────────────────────────────────────────────────
_section "T08 — DEPLOYMENT CREATION"
# ─────────────────────────────────────────────────────────────────────────────

TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)

# Single call — capture body and HTTP status code together to avoid duplicate job creation
DEPLOY_RESP=$(curl -s -w "\n%{http_code}" -X POST "${BASE}/api/v1/ota/deployments" \
  -H "Authorization: ${TOKEN}" -H "Content-Type: application/json" \
  -d "{\"packageName\":\"${TEST_PKG_NAME}\",\"version\":\"${TEST_VERSION}\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
DEPLOY=$(echo "$DEPLOY_RESP" | head -1)
DEPLOY_CODE=$(echo "$DEPLOY_RESP" | tail -1)

JOB_ID=$(echo "$DEPLOY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)

if [ -n "$JOB_ID" ] && [ "$JOB_ID" != "None" ]; then
  _pass "Deployment created: $JOB_ID"
  assert_field "$DEPLOY" "status" "AWAITING_CONSENT" "New deployment starts as AWAITING_CONSENT (consent-gated)"
  assert_field "$DEPLOY" "rolloutStage" "CANARY" "Rollout stage preserved"
  assert_has_field "$DEPLOY" "consentCount" "Response has consentCount"
  echo "$JOB_ID" > /tmp/ota_test_job_id.txt
else
  _fail "Deployment creation returned no jobId: $DEPLOY"
  echo "NOJOB" > /tmp/ota_test_job_id.txt
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T09 — JOB STATUS & LIST"
# ─────────────────────────────────────────────────────────────────────────────

JOB_ID=$(cat /tmp/ota_test_job_id.txt)
if [ "$JOB_ID" != "NOJOB" ]; then
  JOB=$(call GET "/api/v1/ota/deployments/${JOB_ID}")
  # AWAITING_CONSENT jobs have no IoT Job yet — iotStatus only present after consent accepted
  JOB_STATUS_VAL=$(echo "$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  if [ "$JOB_STATUS_VAL" = "AWAITING_CONSENT" ]; then
    _pass "GET job returns AWAITING_CONSENT — iotStatus/iotJobStatus absent until consent given"
  else
    assert_has_field "$JOB" "iotStatus" "GET job includes live iotStatus"
    assert_has_field "$JOB" "iotJobStatus" "GET job includes iotJobStatus"
  fi
  assert_has_field "$JOB" "deviceStatuses" "GET job includes deviceStatuses"
  IOT_STATUS=$(echo "$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin).get('iotJobStatus',''))")
  echo "  → IoT Job status: $IOT_STATUS"

  LIST_JOBS=$(call GET "/api/v1/ota/deployments")
  JOBS_COUNT=$(echo "$LIST_JOBS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))")
  [ "$JOBS_COUNT" -gt 0 ] && _pass "GET /deployments returns $JOBS_COUNT jobs" || _fail "GET /deployments returned 0"
else
  _warn "Skipping job status tests — no job ID available"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T10 — DEVICE STATUS HANDLER (simulate SUCCEEDED)"
# ─────────────────────────────────────────────────────────────────────────────

JOB_ID=$(cat /tmp/ota_test_job_id.txt)
TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)

if [ "$JOB_ID" != "NOJOB" ]; then
  # Publish IN_PROGRESS then SUCCEEDED
  printf '{"jobId":"%s","status":"IN_PROGRESS","progress":50,"packageName":"%s","version":"%s"}' \
    "$JOB_ID" "$TEST_PKG_NAME" "$TEST_VERSION" > /tmp/mqtt_inprogress.json
  printf '{"jobId":"%s","status":"SUCCEEDED","progress":100,"packageName":"%s","version":"%s","installedVersion":"%s"}' \
    "$JOB_ID" "$TEST_PKG_NAME" "$TEST_VERSION" "$TEST_VERSION" > /tmp/mqtt_succeeded.json

  aws iot-data publish \
    --topic "iot/device/${DEVICE_ID}/ota/status" \
    --cli-binary-format raw-in-base64-out \
    --payload "$(cat /tmp/mqtt_inprogress.json)" \
    --region "$REGION" 2>/dev/null && _pass "IN_PROGRESS published to IoT MQTT topic" \
    || _fail "Failed to publish IN_PROGRESS"

  sleep 2

  aws iot-data publish \
    --topic "iot/device/${DEVICE_ID}/ota/status" \
    --cli-binary-format raw-in-base64-out \
    --payload "$(cat /tmp/mqtt_succeeded.json)" \
    --region "$REGION" 2>/dev/null && _pass "SUCCEEDED published to IoT MQTT topic" \
    || _fail "Failed to publish SUCCEEDED"

  sleep 5

  # Verify job marked SUCCEEDED
  JOB_STATUS=$(aws dynamodb get-item \
    --table-name digilux_ota_jobs \
    --key "{\"jobId\":{\"S\":\"${JOB_ID}\"}}" \
    --region "$REGION" --query 'Item.status.S' --output text 2>/dev/null)
  [ "$JOB_STATUS" = "SUCCEEDED" ] \
    && _pass "Job status updated to SUCCEEDED in DynamoDB" \
    || _fail "Job status not updated — got: $JOB_STATUS"

  # Verify installed version updated in device_data
  INV_VER=$(aws dynamodb query \
    --table-name digilux_device_data \
    --key-condition-expression "deviceId = :d" \
    --expression-attribute-values "{\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
    --region "$REGION" \
    --query "Items[0].installedVersions.M.\"${TEST_PKG_NAME}\".S" --output text 2>/dev/null)
  [ "$INV_VER" = "$TEST_VERSION" ] \
    && _pass "Device data updated: ${TEST_PKG_NAME}=$INV_VER" \
    || _fail "installedVersions not updated — got ${TEST_PKG_NAME}=$INV_VER, expected $TEST_VERSION"

  # Verify pendingJobId cleared
  PENDING=$(aws dynamodb query \
    --table-name digilux_device_data \
    --key-condition-expression "deviceId = :d" \
    --expression-attribute-values "{\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
    --region "$REGION" \
    --query 'Items[0].pendingJobId' --output text 2>/dev/null)
  [ "$PENDING" = "None" ] || [ -z "$PENDING" ] || [ "$PENDING" = "True" ] \
    && _pass "pendingJobId cleared after SUCCEEDED" \
    || _warn "pendingJobId not cleared: $PENDING"

  # Re-deploy same package+version → admin can always create consent-gated deployment (201)
  # Version guard lives in user_consent (user side), not job_create (admin side)
  code=$(http_code POST "/api/v1/ota/deployments" \
    "{\"packageName\":\"${TEST_PKG_NAME}\",\"version\":\"${TEST_VERSION}\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\"}")
  assert_code "$code" "201" "Re-deploy same version → 201 (admin creates consent record regardless of installed version)"
else
  _warn "Skipping status handler tests — no job ID"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T11 — SIMULATE FAILED UPDATE (with rollback detail)"
# ─────────────────────────────────────────────────────────────────────────────

# Create a second deployment using the same (now-installed) package
# Reset installed version to something lower so job_create allows the deploy
TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET installedVersions.#pkg = :v, pendingJobId = :null" \
  --expression-attribute-names "{\"#pkg\":\"${TEST_PKG_NAME}\"}" \
  --expression-attribute-values "{\":v\":{\"S\":\"1.0.0\"},\":null\":{\"NULL\":true}}" \
  --region "$REGION" > /dev/null 2>&1
echo "  → Reset device to ${TEST_PKG_NAME}@1.0.0 for failure test"

FAIL_DEPLOY=$(call POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"${TEST_PKG_NAME}\",\"version\":\"${TEST_VERSION}\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
FAIL_JOB_ID=$(echo "$FAIL_DEPLOY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)

if [ -n "$FAIL_JOB_ID" ] && [ "$FAIL_JOB_ID" != "None" ]; then
  _pass "Created failure-test job: $FAIL_JOB_ID"

  # Simulate FAILED with rollback detail
  printf '{"jobId":"%s","status":"FAILED","progress":0,"packageName":"%s","version":"%s","error":"tarball extraction failed: disk full","statusDetail":"Install failed — previous version restored"}' \
    "$FAIL_JOB_ID" "$TEST_PKG_NAME" "$TEST_VERSION" > /tmp/mqtt_failed.json

  aws iot-data publish \
    --topic "iot/device/${DEVICE_ID}/ota/status" \
    --cli-binary-format raw-in-base64-out \
    --payload "$(cat /tmp/mqtt_failed.json)" \
    --region "$REGION" 2>/dev/null
  sleep 4

  FAIL_STATUS=$(aws dynamodb get-item \
    --table-name digilux_ota_jobs \
    --key "{\"jobId\":{\"S\":\"${FAIL_JOB_ID}\"}}" \
    --region "$REGION" --query 'Item.status.S' --output text 2>/dev/null)
  [ "$FAIL_STATUS" = "FAILED" ] \
    && _pass "FAILED status recorded in DynamoDB (device rolled back)" \
    || _fail "Job status not FAILED — got: $FAIL_STATUS"

  # pendingJobId should be cleared even on FAILED
  PENDING=$(aws dynamodb query \
    --table-name digilux_device_data \
    --key-condition-expression "deviceId = :d" \
    --expression-attribute-values "{\":d\":{\"S\":\"${DEVICE_ID}\"}}" \
    --region "$REGION" --query 'Items[0].pendingJobId' --output text 2>/dev/null)
  [ "$PENDING" = "None" ] || [ -z "$PENDING" ] || [ "$PENDING" = "True" ] \
    && _pass "pendingJobId cleared after FAILED (device can accept next job)" \
    || _warn "pendingJobId not cleared after FAILED: $PENDING"
else
  _warn "Could not create failure-test job"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T12 — ABORT FLOW"
# ─────────────────────────────────────────────────────────────────────────────

# Reset device version and create a new job to abort
TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET installedVersions.#pkg = :v, pendingJobId = :null" \
  --expression-attribute-names "{\"#pkg\":\"${TEST_PKG_NAME}\"}" \
  --expression-attribute-values "{\":v\":{\"S\":\"1.0.0\"},\":null\":{\"NULL\":true}}" \
  --region "$REGION" > /dev/null 2>&1

ABORT_DEPLOY=$(call POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"${TEST_PKG_NAME}\",\"version\":\"${TEST_VERSION}\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
ABORT_JOB_ID=$(echo "$ABORT_DEPLOY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)

if [ -n "$ABORT_JOB_ID" ] && [ "$ABORT_JOB_ID" != "None" ]; then
  ABORT_RESP=$(call POST "/api/v1/ota/deployments/${ABORT_JOB_ID}/abort")
  ABORT_STATUS=$(echo "$ABORT_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
  [ "$ABORT_STATUS" = "CANCELLED" ] && _pass "Abort returns status=CANCELLED" || _fail "Abort failed: $ABORT_RESP"

  # Abort again → should return 400 (already cancelled)
  ABORT2_CODE=$(http_code POST "/api/v1/ota/deployments/${ABORT_JOB_ID}/abort")
  [ "$ABORT2_CODE" = "400" ] && _pass "Aborting already-cancelled job → 400" || _warn "Expected 400 on double-abort, got $ABORT2_CODE"
else
  _warn "Could not create abort-test job"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T13 — AUDIT LOGS IN CLOUDWATCH"
# ─────────────────────────────────────────────────────────────────────────────

check_audit_log() {
  local log_group="$1" event_name="$2" label="$3"
  local start_ms=$(( ($(date +%s) - 120) * 1000 ))

  FOUND=$(aws logs filter-log-events \
    --log-group-name "$log_group" \
    --start-time "$start_ms" \
    --filter-pattern "{ $.event = \"$event_name\" }" \
    --region "$REGION" \
    --query 'events[0].message' --output text 2>/dev/null)

  [ -n "$FOUND" ] && [ "$FOUND" != "None" ] \
    && _pass "$label — audit event found in $log_group" \
    || _warn "$label — no audit event found yet in $log_group (may need more time)"
}

check_audit_log "/aws/lambda/digilux_ota_upload_url"     "PACKAGE_UPLOAD_URL_REQUESTED" "PACKAGE_UPLOAD_URL_REQUESTED audit"
check_audit_log "/aws/lambda/digilux_ota_artifact_processor" "PACKAGE_REGISTERED_ACTIVE" "PACKAGE_REGISTERED_ACTIVE audit"
check_audit_log "/aws/lambda/digilux_ota_job_create"     "DEPLOYMENT_CREATED"          "DEPLOYMENT_CREATED audit"
check_audit_log "/aws/lambda/digilux_ota_job_create"     "DEPLOYMENT_ABORTED"          "DEPLOYMENT_ABORTED audit"
check_audit_log "/aws/lambda/digilux_ota_status_handler" "DEVICE_UPDATE_SUCCEEDED"     "DEVICE_UPDATE_SUCCEEDED audit"
check_audit_log "/aws/lambda/digilux_ota_status_handler" "DEVICE_UPDATE_FAILED"        "DEVICE_UPDATE_FAILED audit"

# ─────────────────────────────────────────────────────────────────────────────
_section "T14 — DYNAMODB CONSISTENCY"
# ─────────────────────────────────────────────────────────────────────────────

# Verify all 4 tables exist and have records
for TABLE in digilux_ota_packages digilux_device_data digilux_ota_jobs digilux_ota_compatibility; do
  COUNT=$(aws dynamodb scan --table-name "$TABLE" --region "$REGION" \
    --select COUNT --query 'Count' --output text 2>/dev/null)
  [ "$COUNT" -gt 0 ] && _pass "Table $TABLE has $COUNT records" || _warn "Table $TABLE is empty"
done

# ─────────────────────────────────────────────────────────────────────────────
_section "T15 — IOT INFRASTRUCTURE"
# ─────────────────────────────────────────────────────────────────────────────

# Verify IoT Rules exist
for RULE in digilux_ota_status_ingest digilux_ota_device_register; do
  STATUS=$(aws iot get-topic-rule --rule-name "$RULE" --region "$REGION" \
    --query 'rule.ruleDisabled' --output text 2>/dev/null)
  [ "$STATUS" = "False" ] && _pass "IoT Rule $RULE is active" || _fail "IoT Rule $RULE missing or disabled"
done

# Verify Thing Groups exist
for GROUP in DGX-Canary DGX-Beta DGX-Production DGX-Controllers; do
  EXISTS=$(aws iot describe-thing-group --thing-group-name "$GROUP" \
    --region "$REGION" --query 'thingGroupName' --output text 2>/dev/null)
  [ "$EXISTS" = "$GROUP" ] && _pass "Thing Group $GROUP exists" || _fail "Thing Group $GROUP missing"
done

# Verify S3 bucket and event notifications
NOTIF_COUNT=$(aws s3api get-bucket-notification-configuration \
  --bucket digilux-ota-artifacts --region "$REGION" \
  --query 'length(LambdaFunctionConfigurations)' --output text 2>/dev/null)
[ "$NOTIF_COUNT" = "4" ] && _pass "S3 bucket has 4 event notifications configured" \
  || _warn "S3 bucket event notifications: expected 4, got $NOTIF_COUNT"

# Verify ECDSA signing key in Secrets Manager
SECRET=$(aws secretsmanager describe-secret \
  --secret-id digilux-ota-signing-key --region "$REGION" \
  --query 'Name' --output text 2>/dev/null)
[ "$SECRET" = "digilux-ota-signing-key" ] \
  && _pass "ECDSA signing key present in Secrets Manager" \
  || _fail "ECDSA signing key missing from Secrets Manager"

# ─────────────────────────────────────────────────────────────────────────────
_section "T16 — LAMBDA HEALTH"
# ─────────────────────────────────────────────────────────────────────────────

for FUNC in digilux_ota_upload_url digilux_ota_artifact_processor digilux_ota_job_create \
            digilux_ota_compatibility_check digilux_ota_status_handler digilux_ota_device_register; do
  STATE=$(aws lambda get-function --function-name "$FUNC" --region "$REGION" \
    --query 'Configuration.State' --output text 2>/dev/null)
  RUNTIME=$(aws lambda get-function --function-name "$FUNC" --region "$REGION" \
    --query 'Configuration.Runtime' --output text 2>/dev/null)
  [ "$STATE" = "Active" ] \
    && _pass "Lambda $FUNC: State=$STATE Runtime=$RUNTIME" \
    || _fail "Lambda $FUNC not Active — State=$STATE"
done

# ─────────────────────────────────────────────────────────────────────────────
_section "T17 — URL MASKING & CLOUDFRONT DISABLED"
# ─────────────────────────────────────────────────────────────────────────────

# (+) CloudFront env vars must be absent on digilux_ota_user_consent
CF_DOMAIN=$(aws lambda get-function-configuration \
  --function-name digilux_ota_user_consent --region "$REGION" \
  --query 'Environment.Variables.CLOUDFRONT_DOMAIN' --output text 2>/dev/null)
[ -z "$CF_DOMAIN" ] || [ "$CF_DOMAIN" = "None" ] \
  && _pass "user_consent: CLOUDFRONT_DOMAIN is not set (CloudFront disabled)" \
  || _fail "user_consent: CLOUDFRONT_DOMAIN is still set to '$CF_DOMAIN' — CloudFront NOT disabled"

CF_KP=$(aws lambda get-function-configuration \
  --function-name digilux_ota_user_consent --region "$REGION" \
  --query 'Environment.Variables.CLOUDFRONT_KEY_PAIR_ID' --output text 2>/dev/null)
[ -z "$CF_KP" ] || [ "$CF_KP" = "None" ] \
  && _pass "user_consent: CLOUDFRONT_KEY_PAIR_ID is not set" \
  || _fail "user_consent: CLOUDFRONT_KEY_PAIR_ID is still set to '$CF_KP'"

CF_SECRET=$(aws lambda get-function-configuration \
  --function-name digilux_ota_user_consent --region "$REGION" \
  --query 'Environment.Variables.CLOUDFRONT_PRIVATE_KEY_SECRET' --output text 2>/dev/null)
[ -z "$CF_SECRET" ] || [ "$CF_SECRET" = "None" ] \
  && _pass "user_consent: CLOUDFRONT_PRIVATE_KEY_SECRET is not set" \
  || _fail "user_consent: CLOUDFRONT_PRIVATE_KEY_SECRET is still set"

# (+) digilux_ota_user_get_download_link also has no CloudFront env vars
DL_CF=$(aws lambda get-function-configuration \
  --function-name digilux_ota_user_get_download_link --region "$REGION" \
  --query 'Environment.Variables.CLOUDFRONT_DOMAIN' --output text 2>/dev/null)
[ -z "$DL_CF" ] || [ "$DL_CF" = "None" ] \
  && _pass "user_get_download_link: CLOUDFRONT_DOMAIN is not set" \
  || _fail "user_get_download_link: CLOUDFRONT_DOMAIN is set to '$DL_CF'"

# (+) encS3Key in S3 — object actually exists under enc/ prefix (not old path)
TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)
ENC_KEY_S3=$(aws dynamodb get-item \
  --table-name digilux_ota_packages \
  --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION}\"}}" \
  --region "$REGION" --query 'Item.encS3Key.S' --output text 2>/dev/null)

if [ -n "$ENC_KEY_S3" ] && [ "$ENC_KEY_S3" != "None" ]; then
  aws s3api head-object \
    --bucket digilux-ota-artifacts \
    --key "$ENC_KEY_S3" \
    --region "$REGION" > /dev/null 2>&1 \
    && _pass "Encrypted artifact exists in S3 at opaque key: $ENC_KEY_S3" \
    || _fail "Encrypted artifact NOT found in S3 at: $ENC_KEY_S3"

  # (-) No object exists at the OLD readable path
  OLD_KEY="Network_controller_firmware/${TEST_PKG_NAME}/${TEST_VERSION}/${TEST_PKG_NAME}-${TEST_VERSION}.enc"
  aws s3api head-object \
    --bucket digilux-ota-artifacts \
    --key "$OLD_KEY" \
    --region "$REGION" > /dev/null 2>&1 \
    && _fail "Artifact found at OLD readable path — UUID masking not applied: $OLD_KEY" \
    || _pass "No artifact at old readable path (UUID masking confirmed)"
else
  _warn "Skipping S3 object existence check — encS3Key not found in DynamoDB"
fi

# (+) Two uploads of different versions produce different UUIDs (no key collision)
TEST_VERSION_B="5.0.$(date +%s)-uuid-collision-check"
UPLOAD_B=$(call POST "/api/v1/ota/packages/upload-artefact" \
  "{\"deviceType\":\"Network_controller_firmware\",\"version\":\"${TEST_VERSION_B}\",\"releaseType\":\"PROD\",\"checksum\":\"${TEST_CHECKSUM}\",\"releaseNotes\":\"UUID collision check upload\"}")
UPLOAD_URL_B=$(echo "$UPLOAD_B" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadUrl',''))" 2>/dev/null)
UPLOAD_TOKEN_B=$(echo "$UPLOAD_B" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadToken',''))" 2>/dev/null)

if [ -n "$UPLOAD_URL_B" ] && [ "$UPLOAD_URL_B" != "None" ]; then
  curl -s -o /dev/null -X PUT "$UPLOAD_URL_B" \
    -H "Content-Type: application/octet-stream" \
    -H "x-amz-meta-upload-token: ${UPLOAD_TOKEN_B}" \
    --data-binary @/tmp/test_artifact.bin

  echo "  → Waiting for second package to go ACTIVE (UUID collision check)..."
  for i in $(seq 1 30); do
    sleep 1
    STATUS_B=$(aws dynamodb get-item \
      --table-name digilux_ota_packages \
      --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION_B}\"}}" \
      --region "$REGION" --query 'Item.status.S' --output text 2>/dev/null)
    [ "$STATUS_B" = "ACTIVE" ] && break
  done

  if [ "$STATUS_B" = "ACTIVE" ]; then
    ENC_KEY_B=$(aws dynamodb get-item \
      --table-name digilux_ota_packages \
      --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION_B}\"}}" \
      --region "$REGION" --query 'Item.encS3Key.S' --output text 2>/dev/null)
    [ "$ENC_KEY_S3" != "$ENC_KEY_B" ] \
      && _pass "Two uploads produce distinct UUID keys (no collision): ...${ENC_KEY_S3: -12} vs ...${ENC_KEY_B: -12}" \
      || _fail "UUID collision — two uploads got the same encS3Key: $ENC_KEY_S3"
  else
    _warn "Second package did not reach ACTIVE in 15s — skipping collision check"
  fi
else
  _warn "Skipping UUID collision check — second upload failed"
fi

# (-) Presigned URL from a job document must not contain cloudfront.net
JOB_ID=$(cat /tmp/ota_test_job_id.txt 2>/dev/null || echo "NOJOB")
if [ "$JOB_ID" != "NOJOB" ]; then
  JOB_DOC=$(aws iot get-job-document --job-id "$JOB_ID" --region "$REGION" \
    --query 'document' --output text 2>/dev/null)
  if [ -n "$JOB_DOC" ]; then
    echo "$JOB_DOC" | python3 -c "
import json, sys
doc = json.loads(sys.stdin.read())
url = doc.get('artifact', {}).get('presignedUrl', '')
if 'cloudfront.net' in url:
    print('CLOUDFRONT')
    sys.exit(1)
sys.exit(0)
" 2>/dev/null \
      && _pass "Job document presignedUrl does not use CloudFront (S3 direct)" \
      || _fail "Job document presignedUrl still uses CloudFront — env vars not cleared"

    # (-) presignedUrl must not contain packageName or version
    echo "$JOB_DOC" | python3 -c "
import json, sys
doc = json.loads(sys.stdin.read())
url = doc.get('artifact', {}).get('presignedUrl', '').lower()
pkg  = doc.get('packageName', '').lower()
ver  = doc.get('version', '').replace('.', '-').lower()
leaks = [x for x in [pkg, ver] if x and x in url]
if leaks:
    print('LEAKS: ' + str(leaks))
    sys.exit(1)
sys.exit(0)
" 2>/dev/null \
      && _pass "presignedUrl does not leak packageName or version in path" \
      || _fail "presignedUrl leaks packageName/version — UUID masking not applied to this job"
  else
    _warn "Could not fetch job document for URL inspection"
  fi
else
  _warn "Skipping job document URL checks — no job ID available"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T18 — CHECK UPDATES: JOB STATUS IN RESPONSE"
# Prereq: digilux_ota_user_check_updates Lambda must have OTA_JOBS_TABLE env var set.
# ─────────────────────────────────────────────────────────────────────────────

TEST_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
TEST_PKG_NAME=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)
CU_JOB_ID="digilux-ota-e2e-cu-job-$(date +%s)"
NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")

# Helper: extract a top-level field from the device entry matching DEVICE_ID
cu_device_field() {
  local resp="$1" field="$2"
  echo "$resp" | python3 -c "
import json,sys
devs = json.load(sys.stdin).get('devices', [])
dev  = next((x for x in devs if x.get('deviceId') == '${DEVICE_ID}'), {})
print(dev.get('$field', '__MISSING__'))
" 2>/dev/null
}

# Helper: extract a field from activeJob inside the device entry
cu_activejob_field() {
  local resp="$1" field="$2"
  echo "$resp" | python3 -c "
import json,sys
devs = json.load(sys.stdin).get('devices', [])
dev  = next((x for x in devs if x.get('deviceId') == '${DEVICE_ID}'), {})
aj   = dev.get('activeJob', {})
print(aj.get('$field', '__MISSING__'))
" 2>/dev/null
}

# Helper: check whether the device entry contains an activeJob key at all
cu_has_active_job() {
  local resp="$1"
  echo "$resp" | python3 -c "
import json,sys
devs = json.load(sys.stdin).get('devices', [])
dev  = next((x for x in devs if x.get('deviceId') == '${DEVICE_ID}'), {})
print('yes' if 'activeJob' in dev else 'no')
" 2>/dev/null
}

# ── Baseline: no pendingJobId → normal response, no activeJob ────────────────
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "REMOVE pendingJobId" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
CU_OK=$(echo "$CU_RESP" | python3 -c "import json,sys; print('yes' if 'devices' in json.load(sys.stdin) else 'no')" 2>/dev/null)
[ "$CU_OK" = "yes" ] \
  && _pass "check_updates baseline → 200 with devices array" \
  || _fail "check_updates baseline failed — no devices key in response"

[ "$(cu_has_active_job "$CU_RESP")" = "no" ] \
  && _pass "No pendingJobId → no activeJob in response (unchanged behaviour)" \
  || _fail "No pendingJobId but activeJob appeared in response"

# ── Insert synthetic job record + set pendingJobId on device ─────────────────
aws dynamodb put-item \
  --table-name digilux_ota_jobs \
  --item "{
    \"jobId\":       {\"S\":\"${CU_JOB_ID}\"},
    \"packageName\": {\"S\":\"${TEST_PKG_NAME}\"},
    \"version\":     {\"S\":\"${TEST_VERSION}\"},
    \"targetId\":    {\"S\":\"${DEVICE_ID}\"},
    \"status\":      {\"S\":\"AWAITING_CONSENT\"},
    \"createdAt\":   {\"N\":\"${NOW_MS}\"}
  }" \
  --region "$REGION" > /dev/null 2>&1

aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET pendingJobId = :jid" \
  --expression-attribute-values "{\":jid\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --region "$REGION" > /dev/null 2>&1

# ── (+) AWAITING_CONSENT → JOB_ACTIVE + in-progress message ──────────────────
CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")

OTA_STATUS=$(cu_device_field "$CU_RESP" "otaStatus")
[ "$OTA_STATUS" = "JOB_ACTIVE" ] \
  && _pass "AWAITING_CONSENT job → otaStatus=JOB_ACTIVE" \
  || _fail "AWAITING_CONSENT — expected otaStatus=JOB_ACTIVE, got: $OTA_STATUS"

AJ_STATUS=$(cu_activejob_field "$CU_RESP" "status")
[ "$AJ_STATUS" = "AWAITING_CONSENT" ] \
  && _pass "AWAITING_CONSENT → activeJob.status=AWAITING_CONSENT" \
  || _fail "activeJob.status — expected AWAITING_CONSENT, got: $AJ_STATUS"

AJ_VER=$(cu_activejob_field "$CU_RESP" "version")
[ "$AJ_VER" = "$TEST_VERSION" ] \
  && _pass "AWAITING_CONSENT → activeJob.version=$TEST_VERSION" \
  || _fail "activeJob.version — expected $TEST_VERSION, got: $AJ_VER"

AJ_JOB_ID=$(cu_activejob_field "$CU_RESP" "jobId")
[ "$AJ_JOB_ID" = "$CU_JOB_ID" ] \
  && _pass "AWAITING_CONSENT → activeJob.jobId matches" \
  || _fail "activeJob.jobId mismatch — expected $CU_JOB_ID, got: $AJ_JOB_ID"

AJ_MSG=$(cu_activejob_field "$CU_RESP" "message")
echo "$AJ_MSG" | python3 -c "
import sys
msg = sys.stdin.read()
ok  = 'in progress' in msg.lower() and '${TEST_VERSION}' in msg
sys.exit(0 if ok else 1)
" 2>/dev/null \
  && _pass "AWAITING_CONSENT message contains version and 'in progress'" \
  || _fail "AWAITING_CONSENT message wrong: $AJ_MSG"

# (-) availableVersion must NOT appear when job is active (version compare skipped)
AV=$(cu_device_field "$CU_RESP" "availableVersion")
[ "$AV" = "__MISSING__" ] \
  && _pass "AWAITING_CONSENT response has no availableVersion (version compare skipped)" \
  || _fail "availableVersion unexpectedly present while job is active: $AV"

# ── (+) QUEUED → JOB_ACTIVE + in-progress message ────────────────────────────
aws dynamodb update-item \
  --table-name digilux_ota_jobs \
  --key "{\"jobId\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --update-expression "SET #s = :s" \
  --expression-attribute-names "{\"#s\":\"status\"}" \
  --expression-attribute-values "{\":s\":{\"S\":\"QUEUED\"}}" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
AJ_STATUS=$(cu_activejob_field "$CU_RESP" "status")
[ "$AJ_STATUS" = "QUEUED" ] \
  && _pass "QUEUED job → activeJob.status=QUEUED" \
  || _fail "QUEUED job — activeJob.status: $AJ_STATUS"

AJ_MSG=$(cu_activejob_field "$CU_RESP" "message")
echo "$AJ_MSG" | python3 -c "
import sys; sys.exit(0 if 'in progress' in sys.stdin.read().lower() else 1)
" 2>/dev/null \
  && _pass "QUEUED job message is in-progress variant" \
  || _fail "QUEUED job message wrong: $AJ_MSG"

# ── (+) IN_PROGRESS → JOB_ACTIVE + in-progress message ───────────────────────
aws dynamodb update-item \
  --table-name digilux_ota_jobs \
  --key "{\"jobId\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --update-expression "SET #s = :s" \
  --expression-attribute-names "{\"#s\":\"status\"}" \
  --expression-attribute-values "{\":s\":{\"S\":\"IN_PROGRESS\"}}" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
AJ_STATUS=$(cu_activejob_field "$CU_RESP" "status")
[ "$AJ_STATUS" = "IN_PROGRESS" ] \
  && _pass "IN_PROGRESS job → activeJob.status=IN_PROGRESS" \
  || _fail "IN_PROGRESS job — activeJob.status: $AJ_STATUS"

AJ_MSG=$(cu_activejob_field "$CU_RESP" "message")
echo "$AJ_MSG" | python3 -c "
import sys; sys.exit(0 if 'in progress' in sys.stdin.read().lower() else 1)
" 2>/dev/null \
  && _pass "IN_PROGRESS job message is in-progress variant" \
  || _fail "IN_PROGRESS job message wrong: $AJ_MSG"

# ── (+) FAILED → JOB_ACTIVE + failed message ─────────────────────────────────
aws dynamodb update-item \
  --table-name digilux_ota_jobs \
  --key "{\"jobId\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --update-expression "SET #s = :s" \
  --expression-attribute-names "{\"#s\":\"status\"}" \
  --expression-attribute-values "{\":s\":{\"S\":\"FAILED\"}}" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
AJ_STATUS=$(cu_activejob_field "$CU_RESP" "status")
[ "$AJ_STATUS" = "FAILED" ] \
  && _pass "FAILED job → activeJob.status=FAILED" \
  || _fail "FAILED job — activeJob.status: $AJ_STATUS"

AJ_MSG=$(cu_activejob_field "$CU_RESP" "message")
echo "$AJ_MSG" | python3 -c "
import sys
msg = sys.stdin.read()
ok  = 'failed' in msg.lower() and '${TEST_VERSION}' in msg and 'support' in msg.lower()
sys.exit(0 if ok else 1)
" 2>/dev/null \
  && _pass "FAILED message contains version, 'failed', and 'support'" \
  || _fail "FAILED message wrong: $AJ_MSG"

# (-) FAILED response must also not expose availableVersion
AV=$(cu_device_field "$CU_RESP" "availableVersion")
[ "$AV" = "__MISSING__" ] \
  && _pass "FAILED job response has no availableVersion (version compare skipped)" \
  || _fail "availableVersion present while job is FAILED: $AV"

# ── (+) SUCCEEDED → falls through, no activeJob ───────────────────────────────
aws dynamodb update-item \
  --table-name digilux_ota_jobs \
  --key "{\"jobId\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --update-expression "SET #s = :s" \
  --expression-attribute-names "{\"#s\":\"status\"}" \
  --expression-attribute-values "{\":s\":{\"S\":\"SUCCEEDED\"}}" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
[ "$(cu_has_active_job "$CU_RESP")" = "no" ] \
  && _pass "SUCCEEDED job → no activeJob (falls through to normal update check)" \
  || _fail "SUCCEEDED job unexpectedly returned activeJob block"

# ── (-) Stale pendingJobId (no matching job record) → graceful fallthrough ────
GHOST_JOB_ID="digilux-ota-ghost-$(date +%s)"
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET pendingJobId = :jid" \
  --expression-attribute-values "{\":jid\":{\"S\":\"${GHOST_JOB_ID}\"}}" \
  --region "$REGION" > /dev/null 2>&1

CU_RESP=$(call GET "/api/v1/ota/my/updates" "" "$NON_ADMIN_TOKEN")
CU_OK=$(echo "$CU_RESP" | python3 -c "import json,sys; print('yes' if 'devices' in json.load(sys.stdin) else 'no')" 2>/dev/null)
[ "$CU_OK" = "yes" ] \
  && _pass "Stale pendingJobId (ghost job record) → 200 graceful fallthrough, no 500" \
  || _fail "Stale pendingJobId caused a crash: response=$CU_RESP"

[ "$(cu_has_active_job "$CU_RESP")" = "no" ] \
  && _pass "Ghost job → no activeJob block (warning logged, normal update check proceeds)" \
  || _fail "Ghost job unexpectedly produced an activeJob block"

# (-) Unauthenticated request must be rejected
code=$(http_code GET "/api/v1/ota/my/updates" "" "Bearer invalid.token.here")
[ "$code" = "401" ] || [ "$code" = "403" ] \
  && _pass "check_updates rejects invalid token (HTTP $code)" \
  || _fail "check_updates should reject invalid token, got $code"

# (-) Admin token must not work on user endpoint (different Cognito pool)
code=$(http_code GET "/api/v1/ota/my/updates" "" "$TOKEN")
[ "$code" = "401" ] || [ "$code" = "403" ] \
  && _pass "Admin token rejected on user endpoint GET /my/updates (HTTP $code)" \
  || _warn "Admin token accepted on user endpoint — pool isolation may be misconfigured (HTTP $code)"

# ── Cleanup ───────────────────────────────────────────────────────────────────
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "REMOVE pendingJobId" \
  --region "$REGION" > /dev/null 2>&1

aws dynamodb delete-item \
  --table-name digilux_ota_jobs \
  --key "{\"jobId\":{\"S\":\"${CU_JOB_ID}\"}}" \
  --region "$REGION" > /dev/null 2>&1

echo "  → T18 cleanup done"

# ── Audit log check for new events ───────────────────────────────────────────
check_audit_log "/aws/lambda/digilux_ota_user_check_updates" "ACTIVE_JOB_REPORTED"  "ACTIVE_JOB_REPORTED audit in check_updates"
check_audit_log "/aws/lambda/digilux_ota_user_check_updates" "USER_CHECK_UPDATES"   "USER_CHECK_UPDATES summary audit (jobActive count present)"


# ─────────────────────────────────────────────────────────────────────────────
_section "T21 — USER CONSENT: POST /api/v1/ota/my/updates/consent"
# ─────────────────────────────────────────────────────────────────────────────
# API Gateway authorizer for this endpoint requires PKCE OAuth scopes
# (smarthome_server/read + write).  When /tmp/ota_pkce_token.txt is present
# (generated by infrastructure/get_pkce_token.py), ALL business-logic tests
# call the real API endpoint — full stack, API Gateway + Lambda.
# Without the file, business-logic tests fall back to Lambda direct invocation
# (auth layer is still verified via real API in T21.1-3).
# T21.7 always uses Lambda direct (needs a different-user auth context).
# ─────────────────────────────────────────────────────────────────────────────

T21_VERSION=$(grep TEST_VERSION /tmp/ota_test_version.txt | cut -d= -f2)
T21_PKG=$(grep TEST_PKG_NAME /tmp/ota_test_version.txt | cut -d= -f2)
T21_USER_ID="41f35d4a-d0d1-709e-634f-fc6198a3872d"   # demotesthw5@yopmail.com
T21_THING_NAME=$(aws dynamodb get-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --region "$REGION" --query 'Item.thingName.S' --output text 2>/dev/null)
T21_CONSENT_URL="/api/v1/ota/my/updates/consent"

# Helper: invoke user_consent Lambda, populate T21_STATUS and T21_BODY
t21_invoke() {
  local user_id="$1" body_json="$2"
  python3.9 -W ignore -c "
import json, sys
uid, body = sys.argv[1], sys.argv[2]
print(json.dumps({
  'httpMethod': 'POST',
  'path': '/api/v1/ota/my/updates/consent',
  'headers': {'Content-Type': 'application/json'},
  'body': body,
  'requestContext': {'authorizer': {'claims': {
    'sub': uid, 'email': 'test@test.com', 'cognito:username': uid
  }}}
}))" "$user_id" "$body_json" > /tmp/t21_event.json 2>/dev/null
  aws lambda invoke \
    --function-name digilux_ota_user_consent \
    --region "$REGION" \
    --payload fileb:///tmp/t21_event.json \
    /tmp/t21_response.json > /dev/null 2>&1
  T21_STATUS=$(python3.9 -W ignore -c \
    "import json; print(json.load(open('/tmp/t21_response.json')).get('statusCode',0))" 2>/dev/null)
  T21_BODY=$(python3.9 -W ignore -c \
    "import json; print(json.load(open('/tmp/t21_response.json')).get('body','{}'))" 2>/dev/null)
}


# Helper: call consent via REAL API (if PKCE_TOKEN available) else Lambda direct
# T21.7 still uses t21_invoke directly (needs to impersonate a different user).
t21_call() {
  local body_json="$1"
  if [ -n "${PKCE_TOKEN:-}" ]; then
    local raw
    raw=$(curl -s -w "\n%{http_code}" -X POST "${BASE}${T21_CONSENT_URL}" \
      -H "Authorization: Bearer $PKCE_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$body_json")
    T21_STATUS=$(echo "$raw" | tail -1)
    T21_BODY=$(echo "$raw" | head -1)
  else
    t21_call "$body_json"
  fi
}
# Helper: extract a field from T21_BODY
t21_field() {
  echo "$T21_BODY" | python3.9 -c \
    "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('$1','__MISSING__'))" 2>/dev/null
}

# Helper: invoke check_updates Lambda for T21_USER_ID, return the device entry for DEVICE_ID
t21_check_updates() {
  python3.9 -W ignore -c "
import boto3, json, sys
client = boto3.client('lambda', region_name='ap-south-1')
event = {
  'httpMethod': 'GET',
  'path': '/api/v1/ota/device/available-updates',
  'headers': {},
  'requestContext': {'authorizer': {'claims': {
    'sub': '${T21_USER_ID}', 'email': 'test@test.com',
    'cognito:username': '${T21_USER_ID}'
  }}}
}
resp = client.invoke(FunctionName='digilux_ota_user_check_updates',
                     InvocationType='RequestResponse',
                     Payload=json.dumps(event).encode())
result = json.loads(resp['Payload'].read())
body = json.loads(result.get('body','{}'))
devices = body.get('devices', [])
dev = next((d for d in devices if d.get('deviceId') == '${DEVICE_ID}'), {})
print(json.dumps(dev))
" 2>/dev/null
}

# Helper: read current pendingJobId from device_data
t21_pending_job() {
  aws dynamodb get-item \
    --table-name digilux_device_data \
    --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
    --region "$REGION" \
    --query 'Item.pendingJobId.S' --output text 2>/dev/null
}

# Ensure no stale pendingJobId on the test device before we start
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "REMOVE pendingJobId" \
  --region "$REGION" > /dev/null 2>&1

# ── AUTH LAYER (real API endpoint — confirms scope enforcement) ───────────────

T21_BODY_SAMPLE="{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"

# (-) No Authorization header → 401
code=$(http_code POST "$T21_CONSENT_URL" "$T21_BODY_SAMPLE" "")
assert_code "$code" "401" "T21.1 (-) No token → 401 (API Gateway)"

# (-) Malformed token → 401
code=$(http_code POST "$T21_CONSENT_URL" "$T21_BODY_SAMPLE" "Bearer invalid.token.here")
[ "$code" = "401" ] || [ "$code" = "403" ] \
  && _pass "T21.2 (-) Malformed token rejected (HTTP $code)" \
  || _fail "T21.2 (-) Malformed token should be 401/403, got $code"

# (-) USER_PASSWORD_AUTH token — right pool but lacks smarthome_server scopes → 401
T21_NO_SCOPE_TOKEN=$(python3.9 -W ignore -c "
import boto3
r = boto3.client('cognito-idp', region_name='ap-south-1').initiate_auth(
    AuthFlow='USER_PASSWORD_AUTH',
    AuthParameters={'USERNAME': 'demotesthw5@yopmail.com', 'PASSWORD': 'DigiluxTest@9900'},
    ClientId='q7189jitfkk4ttesepkgls491'
)
print(r['AuthenticationResult']['AccessToken'])
" 2>/dev/null)
code=$(http_code POST "$T21_CONSENT_URL" "$T21_BODY_SAMPLE" "Bearer ${T21_NO_SCOPE_TOKEN}")
assert_code "$code" "401" \
  "T21.3 (-) Right pool, no smarthome_server OAuth scope → 401 (scope enforcement confirmed)"

# ── INPUT VALIDATION (real API when PKCE_TOKEN set, else Lambda direct) ────────

# (-) Missing deviceId → 400
t21_call \
  "{\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"
[ "$T21_STATUS" = "400" ] \
  && _pass "T21.4 (-) Missing deviceId → 400" \
  || _fail "T21.4 (-) Missing deviceId — expected 400, got $T21_STATUS"

# (-) Missing packageName → 400
t21_call \
  "{\"deviceId\":\"${DEVICE_ID}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"
[ "$T21_STATUS" = "400" ] \
  && _pass "T21.5 (-) Missing packageName → 400" \
  || _fail "T21.5 (-) Missing packageName — expected 400, got $T21_STATUS"

# (-) Missing version → 400
t21_call \
  "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"accepted\":true}"
[ "$T21_STATUS" = "400" ] \
  && _pass "T21.6 (-) Missing version → 400" \
  || _fail "T21.6 (-) Missing version — expected 400, got $T21_STATUS"

# (-) Device belongs to a different userId → 404
t21_invoke "00000000-0000-0000-0000-000000000000" \
  "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"
[ "$T21_STATUS" = "404" ] \
  && _pass "T21.7 (-) Device not owned by caller → 404" \
  || _fail "T21.7 (-) Device not owned by caller — expected 404, got $T21_STATUS"

# (-) Package version does not exist → 404
t21_call \
  "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"0.0.0-nonexistent\",\"accepted\":true}"
[ "$T21_STATUS" = "404" ] \
  && _pass "T21.8 (-) Non-existent package version → 404" \
  || _fail "T21.8 (-) Non-existent package version — expected 404, got $T21_STATUS"

# State: a failed consent must not create a job or touch device_data
T21_PENDING_AFTER_FAIL=$(t21_pending_job)
[ "$T21_PENDING_AFTER_FAIL" = "None" ] || [ -z "$T21_PENDING_AFTER_FAIL" ] \
  && _pass "T21.8 (-) State: failed consent left device_data unchanged (no pendingJobId)" \
  || _fail "T21.8 (-) State: failed consent wrote pendingJobId='$T21_PENDING_AFTER_FAIL' — must not happen"

T21_CU_AFTER_FAIL=$(t21_check_updates)
T21_CU_STATUS_AFTER_FAIL=$(echo "$T21_CU_AFTER_FAIL" | python3.9 -c \
  "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('otaStatus','__MISSING__'))" 2>/dev/null)
[ "$T21_CU_STATUS_AFTER_FAIL" != "JOB_ACTIVE" ] \
  && _pass "T21.8 (-) State: check_updates does not show JOB_ACTIVE after a failed consent" \
  || _fail "T21.8 (-) State: check_updates shows JOB_ACTIVE after a failed consent — spurious job created"

# ── HAPPY PATH: accepted=true ─────────────────────────────────────────────────

t21_call \
  "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"

[ "$T21_STATUS" = "202" ] \
  && _pass "T21.9 (+) accepted=true → 202" \
  || _fail "T21.9 (+) accepted=true — expected 202, got $T21_STATUS: $(echo $T21_BODY | head -c 120)"

T21_JOB_ID=$(t21_field "jobId")
T21_JOB_RESP_STATUS=$(t21_field "status")

[ -n "$T21_JOB_ID" ] && [ "$T21_JOB_ID" != "__MISSING__" ] \
  && _pass "T21.9 (+) Response contains jobId: $T21_JOB_ID" \
  || _fail "T21.9 (+) Response missing jobId — body: $T21_BODY"

[ "$T21_JOB_RESP_STATUS" = "QUEUED" ] \
  && _pass "T21.9 (+) Response status=QUEUED" \
  || _fail "T21.9 (+) Expected status=QUEUED in response, got: $T21_JOB_RESP_STATUS"

# State: pendingJobId written to device_data
T21_PENDING=$(t21_pending_job)
[ "$T21_PENDING" = "$T21_JOB_ID" ] \
  && _pass "T21.10 (+) pendingJobId written to device_data: $T21_PENDING" \
  || _fail "T21.10 (+) pendingJobId mismatch — expected $T21_JOB_ID, got $T21_PENDING"

# State: check_updates immediately reflects JOB_ACTIVE for this device
T21_CU_AFTER_CONSENT=$(t21_check_updates)
T21_CU_OTA_STATUS=$(echo "$T21_CU_AFTER_CONSENT" | python3.9 -c \
  "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('otaStatus','__MISSING__'))" 2>/dev/null)
T21_CU_ACTIVE_JOB_ID=$(echo "$T21_CU_AFTER_CONSENT" | python3.9 -c \
  "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('activeJob',{}).get('jobId','__MISSING__'))" 2>/dev/null)
[ "$T21_CU_OTA_STATUS" = "JOB_ACTIVE" ] \
  && _pass "T21.10 (+) State: check_updates shows otaStatus=JOB_ACTIVE after accepted=true" \
  || _fail "T21.10 (+) State: check_updates expected JOB_ACTIVE, got $T21_CU_OTA_STATUS"
[ "$T21_CU_ACTIVE_JOB_ID" = "$T21_JOB_ID" ] \
  && _pass "T21.10 (+) State: check_updates activeJob.jobId matches consent response ($T21_JOB_ID)" \
  || _fail "T21.10 (+) State: check_updates activeJob.jobId mismatch — expected $T21_JOB_ID, got $T21_CU_ACTIVE_JOB_ID"

# (+) IoT Job exists in AWS IoT targeted at the correct Thing
if [ -n "$T21_JOB_ID" ] && [ "$T21_JOB_ID" != "__MISSING__" ]; then
  T21_IOT_TARGETS=$(aws iot describe-job --job-id "$T21_JOB_ID" --region "$REGION" \
    --query 'job.targets' --output json 2>/dev/null)
  echo "$T21_IOT_TARGETS" | python3.9 -c "
import json, sys
targets = json.load(sys.stdin)
arn = 'arn:aws:iot:ap-south-1:986906626244:thing/${T21_THING_NAME}'
print('OK' if arn in targets else 'MISSING')
" 2>/dev/null | grep -q "^OK$" \
    && _pass "T21.11 (+) IoT Job $T21_JOB_ID targeted at thing/${T21_THING_NAME}" \
    || _fail "T21.11 (+) IoT Job target mismatch — targets: $T21_IOT_TARGETS"

  # (+) Job document contains all required fields
  T21_JOB_DOC=$(aws iot get-job-document --job-id "$T21_JOB_ID" --region "$REGION" \
    --query 'document' --output text 2>/dev/null)
  T21_DOC_RESULT=$(echo "$T21_JOB_DOC" | python3.9 -c "
import json, sys
doc = json.loads(sys.stdin.read())
art = doc.get('artifact', {})
missing = [f for f in ['presignedUrl','sha256','signature','size'] if not art.get(f)]
missing += [f for f in ['packageName','version'] if not doc.get(f)]
print('MISSING:' + ','.join(missing) if missing else 'OK')
" 2>/dev/null)
  [ "$T21_DOC_RESULT" = "OK" ] \
    && _pass "T21.12 (+) Job document has presignedUrl, sha256, signature, size, packageName, version" \
    || _fail "T21.12 (+) Job document missing required fields: $T21_DOC_RESULT"

  # (+) presignedUrl is opaque UUID path — packageName and version must not appear in URL
  T21_URL_RESULT=$(echo "$T21_JOB_DOC" | python3.9 -c "
import json, sys, re
doc  = json.loads(sys.stdin.read())
url  = doc.get('artifact', {}).get('presignedUrl', '').lower()
pkg  = doc.get('packageName', '').lower()
ver  = doc.get('version', '').replace('.', '-').lower()
leaks = [x for x in [pkg, ver] if x and x in url]
has_uuid_path = bool(re.search(r'enc/[0-9a-f-]{36}\.enc', url))
if leaks:
    print('LEAKS:' + str(leaks))
elif not has_uuid_path:
    print('NOT_UUID_PATH:' + url[:80])
else:
    print('OK')
" 2>/dev/null)
  case "$T21_URL_RESULT" in
    OK)      _pass "T21.13 (+) presignedUrl uses opaque UUID path (enc/<uuid>.enc) — no package info leaked" ;;
    LEAKS*)  _fail "T21.13 (+) presignedUrl leaks package info: $T21_URL_RESULT" ;;
    *)       _fail "T21.13 (+) presignedUrl not in enc/<uuid>.enc format: $T21_URL_RESULT" ;;
  esac
else
  _warn "T21.11-13 skipped — T21.9 did not return a jobId"
fi

# ── WRONG VERSION WHILE JOB IS ACTIVE ────────────────────────────────────────
# This is the real-world scenario: device has a live job, user submits consent
# with a non-existent version. Must return 404 AND leave the active job untouched.
# (This is what caused the "job appeared on failed consent" confusion in production.)

if [ -n "$T21_JOB_ID" ] && [ "$T21_JOB_ID" != "__MISSING__" ]; then
  t21_call \
    "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"0.0.0-wrong\",\"accepted\":true}"
  [ "$T21_STATUS" = "404" ] \
    && _pass "T21.16 (-) Wrong version while job active → 404" \
    || _fail "T21.16 (-) Wrong version while job active — expected 404, got $T21_STATUS"

  # State: pendingJobId must be the ORIGINAL job — not changed, not cleared
  T21_PENDING_AFTER_WRONG=$(t21_pending_job)
  [ "$T21_PENDING_AFTER_WRONG" = "$T21_JOB_ID" ] \
    && _pass "T21.16 (-) State: pendingJobId unchanged after wrong-version consent ($T21_JOB_ID)" \
    || _fail "T21.16 (-) State: pendingJobId changed — expected $T21_JOB_ID, got $T21_PENDING_AFTER_WRONG"

  # State: check_updates still shows the SAME original job — not a new one, not gone
  T21_CU_AFTER_WRONG=$(t21_check_updates)
  T21_CU_JOB_AFTER_WRONG=$(echo "$T21_CU_AFTER_WRONG" | python3.9 -c \
    "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('activeJob',{}).get('jobId','__MISSING__'))" 2>/dev/null)
  [ "$T21_CU_JOB_AFTER_WRONG" = "$T21_JOB_ID" ] \
    && _pass "T21.16 (-) State: check_updates still shows original job after wrong-version consent" \
    || _fail "T21.16 (-) State: check_updates job changed — expected $T21_JOB_ID, got $T21_CU_JOB_AFTER_WRONG"
else
  _warn "T21.16 skipped — T21.9 did not produce a jobId"
fi

# ── DECLINE PATH: accepted=false ──────────────────────────────────────────────
# Create a fresh admin deployment to generate a PENDING consent record, then decline it.

T21_DECLINE_DEPLOY=$(call POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
T21_DECLINE_DEPLOY_ID=$(echo "$T21_DECLINE_DEPLOY" | python3.9 -c \
  "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)

if [ -n "$T21_DECLINE_DEPLOY_ID" ] && [ "$T21_DECLINE_DEPLOY_ID" != "None" ]; then
  sleep 1  # allow DynamoDB to commit the PENDING consent record
  # Cancel any stale PENDING consent records from earlier test phases (T08, T10 re-deploy)
  # so _find_pending_consent() picks up only this deployment's record
  python3.9 -W ignore -c "
import boto3
client = boto3.client('dynamodb', region_name='ap-south-1')
resp = client.query(
    TableName='digilux_ota_user_consents',
    IndexName='userId-deviceId-index',
    KeyConditionExpression='userId = :u AND deviceId = :d',
    FilterExpression='#st = :p AND deploymentId <> :new',
    ExpressionAttributeNames={'#st': 'status'},
    ExpressionAttributeValues={
        ':u': {'S': '${T21_USER_ID}'},
        ':d': {'S': '${DEVICE_ID}'},
        ':p': {'S': 'PENDING'},
        ':new': {'S': '${T21_DECLINE_DEPLOY_ID}'},
    }
)
for item in resp.get('Items', []):
    client.update_item(
        TableName='digilux_ota_user_consents',
        Key={'consentId': item['consentId']},
        UpdateExpression='SET #st = :c',
        ExpressionAttributeNames={'#st': 'status'},
        ExpressionAttributeValues={':c': {'S': 'CANCELLED'}}
    )
    print(f'Cancelled stale consent {item[\"consentId\"][\"S\"]}')
" 2>/dev/null

  t21_call \
    "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":false}"
  T21_DECLINE_FIELD=$(t21_field "status")
  [ "$T21_STATUS" = "200" ] && [ "$T21_DECLINE_FIELD" = "DECLINED" ] \
    && _pass "T21.14 (+) accepted=false with pending admin consent → 200 DECLINED" \
    || _fail "T21.14 (+) accepted=false — expected 200/DECLINED, got HTTP $T21_STATUS body=$T21_BODY"

  # State: consent record in digilux_ota_user_consents must be DECLINED
  T21_CONSENT_STATUS=$(aws dynamodb query \
    --table-name digilux_ota_user_consents \
    --index-name deploymentId-index \
    --key-condition-expression "deploymentId = :d" \
    --expression-attribute-values "{\":d\":{\"S\":\"${T21_DECLINE_DEPLOY_ID}\"}}" \
    --region "$REGION" \
    --query 'Items[0].status.S' --output text 2>/dev/null)
  [ "$T21_CONSENT_STATUS" = "DECLINED" ] \
    && _pass "T21.14 (+) State: consent record in DynamoDB is DECLINED" \
    || _fail "T21.14 (+) State: consent record expected DECLINED, got '$T21_CONSENT_STATUS'"

  # State: no IoT job was created for the decline — digilux_ota_jobs must have no QUEUED job
  # for this deploymentId
  T21_DECLINE_JOB_COUNT=$(aws dynamodb query \
    --table-name digilux_ota_jobs \
    --index-name deploymentId-index \
    --key-condition-expression "deploymentId = :d" \
    --expression-attribute-values "{\":d\":{\"S\":\"${T21_DECLINE_DEPLOY_ID}\"}}" \
    --region "$REGION" \
    --query 'Count' --output text 2>/dev/null)
  [ "$T21_DECLINE_JOB_COUNT" = "0" ] || [ -z "$T21_DECLINE_JOB_COUNT" ] \
    && _pass "T21.14 (+) State: no IoT job created for declined consent" \
    || _fail "T21.14 (+) State: IoT job was created despite consent being DECLINED — $T21_DECLINE_JOB_COUNT job(s) found"
else
  _warn "T21.14 skipped — admin deployment creation failed, cannot test decline path"
fi

# ── DUPLICATE CONSENT while pendingJobId is active ───────────────────────────

if [ -n "$T21_JOB_ID" ] && [ "$T21_JOB_ID" != "__MISSING__" ]; then
  t21_call \
    "{\"deviceId\":\"${DEVICE_ID}\",\"packageName\":\"${T21_PKG}\",\"version\":\"${T21_VERSION}\",\"accepted\":true}"
  echo "$T21_STATUS" | grep -qE "^4[0-9]{2}$" \
    && _pass "T21.15 (-) Duplicate consent while job active → $T21_STATUS (blocked)" \
    || _fail "T21.15 (-) Duplicate consent should be 4xx, got $T21_STATUS: $T21_BODY"

  # State: pendingJobId must still be the original job — not a new one
  T21_PENDING_AFTER_DUP=$(t21_pending_job)
  [ "$T21_PENDING_AFTER_DUP" = "$T21_JOB_ID" ] \
    && _pass "T21.15 (-) State: pendingJobId still the original job after blocked duplicate" \
    || _fail "T21.15 (-) State: pendingJobId changed from $T21_JOB_ID to $T21_PENDING_AFTER_DUP"
else
  _warn "T21.15 skipped — T21.9 did not produce a jobId"
fi

# ── CLEANUP ───────────────────────────────────────────────────────────────────
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "REMOVE pendingJobId" \
  --region "$REGION" > /dev/null 2>&1

# ─────────────────────────────────────────────────────────────────────────────

_section "T19 — ARTIFACT PROCESSOR: MANIFEST ENRICHMENT"
# ─────────────────────────────────────────────────────────────────────────────
# Helper: upload a tar to a fresh package version, poll until ACTIVE or CORRUPTED.
# Sets T19_STATUS to the resulting DynamoDB status.
_t19_upload() {
  local version="$1" tarfile="$2" checksum="$3"
  local resp url token pkg

  resp=$(call POST "/api/v1/ota/packages/upload-artefact" \
    "{\"deviceType\":\"Network_controller_firmware\",\"version\":\"${version}\",\"releaseType\":\"PROD\",\"checksum\":\"${checksum}\",\"releaseNotes\":\"T19 manifest test\"}")

  url=$(echo "$resp"   | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadUrl',''))"   2>/dev/null)
  token=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uploadToken',''))" 2>/dev/null)
  pkg=$(echo "$resp"   | python3 -c "import json,sys; print(json.load(sys.stdin).get('packageName',''))" 2>/dev/null)
  T19_PKG="$pkg"

  if [ -z "$url" ] || [ "$url" = "None" ]; then
    T19_STATUS="NO_UPLOAD_URL"
    return
  fi

  curl -s -o /dev/null -X PUT "$url" \
    -H "Content-Type: application/octet-stream" \
    -H "x-amz-meta-upload-token: $token" \
    --data-binary @"$tarfile"

  T19_STATUS="PENDING"
  for i in $(seq 1 30); do
    sleep 1
    T19_STATUS=$(aws dynamodb get-item \
      --table-name digilux_ota_packages \
      --key "{\"packageName\":{\"S\":\"${pkg}\"},\"version\":{\"S\":\"${version}\"}}" \
      --region "$REGION" \
      --query 'Item.status.S' --output text 2>/dev/null)
    [ "$T19_STATUS" = "ACTIVE" ] || [ "$T19_STATUS" = "CORRUPTED" ] && break
  done
}

_t19_checksum() { python3 -c "import hashlib; print(hashlib.sha256(open('$1','rb').read()).hexdigest())"; }

_t19_cleanup() {
  local pkg="$1" ver="$2"
  aws dynamodb delete-item \
    --table-name digilux_ota_packages \
    --key "{\"packageName\":{\"S\":\"${pkg}\"},\"version\":{\"S\":\"${ver}\"}}" \
    --region "$REGION" > /dev/null 2>&1
}

T19_BASE="5.19.$(date +%s)"

# ── (+) Valid tar with object-format manifest → ACTIVE + SHA256 differs ──────
echo "  → T19.1: Valid tar with enriched manifest"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-1"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"packageName":"HomeAssistantUtility","version":"T19_VER","files":[{"name":"ha-controller.jar","type":1},{"name":"app.config.yaml","type":5}]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
echo "fake firmware binary" > "$_T19_DIR/ha-controller.jar"
echo "logLevel: DEBUG" > "$_T19_DIR/app.config.yaml"
tar -czf /tmp/t19_valid.tar -C "$_T19_DIR" manifest.json ha-controller.jar app.config.yaml
rm -rf "$_T19_DIR"
T19_RAW_SHA=$(_t19_checksum /tmp/t19_valid.tar)
_t19_upload "$T19_VER" /tmp/t19_valid.tar "$T19_RAW_SHA"
[ "$T19_STATUS" = "ACTIVE" ] \
  && _pass "T19.1 (+) Valid tar with 2-file object manifest → ACTIVE" \
  || _fail "T19.1 (+) Valid tar should be ACTIVE, got: $T19_STATUS"

# Enriched tar SHA256 must differ from raw upload SHA256 (proves repacking)
T19_STORED_SHA=$(aws dynamodb get-item \
  --table-name digilux_ota_packages \
  --key "{\"packageName\":{\"S\":\"${T19_PKG}\"},\"version\":{\"S\":\"${T19_VER}\"}}" \
  --region "$REGION" \
  --query 'Item.sha256.S' --output text 2>/dev/null)
[ -n "$T19_STORED_SHA" ] && [ "$T19_STORED_SHA" != "$T19_RAW_SHA" ] \
  && _pass "T19.1 (+) Stored SHA256 differs from raw upload → manifest enrichment confirmed" \
  || _fail "T19.1 (+) SHA256 unchanged — manifest enrichment may not have run (raw=$T19_RAW_SHA stored=$T19_STORED_SHA)"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (+) Unknown type value → warns but still ACTIVE ──────────────────────────
echo "  → T19.2: Unknown file type warns but does not abort"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-2"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"packageName":"HomeAssistantUtility","version":"T19_VER","files":[{"name":"payload.bin","type":99}]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
echo "payload for unknown type" > "$_T19_DIR/payload.bin"
tar -czf /tmp/t19_unknown_type.tar -C "$_T19_DIR" manifest.json payload.bin
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_unknown_type.tar)
_t19_upload "$T19_VER" /tmp/t19_unknown_type.tar "$T19_SUM"
[ "$T19_STATUS" = "ACTIVE" ] \
  && _pass "T19.2 (+) Unknown type=99 → warn only, package is ACTIVE" \
  || _fail "T19.2 (+) Unknown type should not quarantine, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) Missing manifest.json → CORRUPTED ────────────────────────────────────
echo "  → T19.3: Missing manifest.json"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-3"
echo "no manifest here" > "$_T19_DIR/payload.bin"
tar -czf /tmp/t19_no_manifest.tar -C "$_T19_DIR" payload.bin
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_no_manifest.tar)
_t19_upload "$T19_VER" /tmp/t19_no_manifest.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.3 (-) Missing manifest.json → CORRUPTED" \
  || _fail "T19.3 (-) Missing manifest.json should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) File in manifest missing from tar → CORRUPTED ────────────────────────
echo "  → T19.4: File listed in manifest missing from tar"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-4"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"packageName":"HomeAssistantUtility","version":"T19_VER","files":[{"name":"missing-file.jar","type":1}]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
tar -czf /tmp/t19_missing_file.tar -C "$_T19_DIR" manifest.json
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_missing_file.tar)
_t19_upload "$T19_VER" /tmp/t19_missing_file.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.4 (-) File listed in manifest missing from tar → CORRUPTED" \
  || _fail "T19.4 (-) Missing file should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) manifest.json missing required field (packageName) → CORRUPTED ────────
echo "  → T19.5: manifest.json missing required field"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-5"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"version":"T19_VER","files":[{"name":"payload.bin","type":1}]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
echo "payload" > "$_T19_DIR/payload.bin"
tar -czf /tmp/t19_missing_field.tar -C "$_T19_DIR" manifest.json payload.bin
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_missing_field.tar)
_t19_upload "$T19_VER" /tmp/t19_missing_field.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.5 (-) manifest.json missing packageName field → CORRUPTED" \
  || _fail "T19.5 (-) Missing required field should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) files[] entry missing 'name' field → CORRUPTED ───────────────────────
echo "  → T19.6: files[] entry missing name field"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-6"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"packageName":"HomeAssistantUtility","version":"T19_VER","files":[{"type":1}]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
tar -czf /tmp/t19_missing_name.tar -C "$_T19_DIR" manifest.json
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_missing_name.tar)
_t19_upload "$T19_VER" /tmp/t19_missing_name.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.6 (-) files[] entry missing 'name' → CORRUPTED" \
  || _fail "T19.6 (-) Missing name field should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) files[] entry is a string not an object → CORRUPTED ──────────────────
echo "  → T19.7: files[] entry is a string (old format)"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-7"
cat > "$_T19_DIR/manifest.json" <<'MEOF'
{"packageName":"HomeAssistantUtility","version":"T19_VER","files":["payload.bin"]}
MEOF
sed -i '' "s/T19_VER/${T19_VER}/" "$_T19_DIR/manifest.json"
echo "payload" > "$_T19_DIR/payload.bin"
tar -czf /tmp/t19_string_entry.tar -C "$_T19_DIR" manifest.json payload.bin
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_string_entry.tar)
_t19_upload "$T19_VER" /tmp/t19_string_entry.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.7 (-) files[] string entry (old format) → CORRUPTED" \
  || _fail "T19.7 (-) String entry should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) Not a valid tar → CORRUPTED ──────────────────────────────────────────
echo "  → T19.8: Not a valid tar (random bytes)"
T19_VER="${T19_BASE}-8"
echo "this is not a tar file at all" > /tmp/t19_not_a_tar.bin
T19_SUM=$(_t19_checksum /tmp/t19_not_a_tar.bin)
_t19_upload "$T19_VER" /tmp/t19_not_a_tar.bin "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.8 (-) Non-tar upload → CORRUPTED" \
  || _fail "T19.8 (-) Non-tar should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

# ── (-) manifest.json contains invalid JSON → CORRUPTED ──────────────────────
echo "  → T19.9: manifest.json is invalid JSON"
_T19_DIR=$(mktemp -d)
T19_VER="${T19_BASE}-9"
echo "{ this is not valid json }" > "$_T19_DIR/manifest.json"
tar -czf /tmp/t19_bad_json.tar -C "$_T19_DIR" manifest.json
rm -rf "$_T19_DIR"
T19_SUM=$(_t19_checksum /tmp/t19_bad_json.tar)
_t19_upload "$T19_VER" /tmp/t19_bad_json.tar "$T19_SUM"
[ "$T19_STATUS" = "CORRUPTED" ] \
  && _pass "T19.9 (-) Invalid JSON manifest → CORRUPTED" \
  || _fail "T19.9 (-) Invalid JSON manifest should be CORRUPTED, got: $T19_STATUS"
_t19_cleanup "$T19_PKG" "$T19_VER"

check_audit_log "/aws/lambda/digilux_ota_artifact_processor" "PACKAGE_REGISTERED_ACTIVE" "T19 artifact_processor ACTIVE audit present"
check_audit_log "/aws/lambda/digilux_ota_artifact_processor" "ARTIFACT_SIGNED"           "T19 artifact_processor SIGNED audit present"

# ─────────────────────────────────────────────────────────────────────────────
_section "T20 — ARTIFACT PROCESSOR: 4-FIELD ECDSA SIGNATURE"
# ─────────────────────────────────────────────────────────────────────────────

T20_BASE="5.20.$(date +%s)"

# ── (+) ECDSA signature verifies over version|size|packageName|sha256 ────────
echo "  → T20.1: 4-field ECDSA signature verification"
_T20_DIR=$(mktemp -d)
T20_VER="${T20_BASE}-1"
cat > "$_T20_DIR/manifest.json" <<MEOF
{"packageName":"HomeAssistantUtility","version":"${T20_VER}","files":[{"name":"payload.bin","type":1}]}
MEOF
echo "t20 firmware payload" > "$_T20_DIR/payload.bin"
tar -czf /tmp/t20_valid.tar -C "$_T20_DIR" manifest.json payload.bin
rm -rf "$_T20_DIR"
T20_RAW_SIZE=$(wc -c < /tmp/t20_valid.tar | tr -d ' ')
T20_SUM=$(_t19_checksum /tmp/t20_valid.tar)
_t19_upload "$T20_VER" /tmp/t20_valid.tar "$T20_SUM"
[ "$T19_STATUS" = "ACTIVE" ] \
  && _pass "T20.1 (+) Package reached ACTIVE" \
  || _fail "T20.1 (+) Package should be ACTIVE for signature test, got: $T19_STATUS"

if [ "$T19_STATUS" = "ACTIVE" ]; then
  # Verify ECDSA signature: derive public key from signing secret, reconstruct 4-field input
  T20_RESULT=$(T20_VER="$T20_VER" T20_PKG="$T19_PKG" REGION="$REGION" python3.9 <<'PYEOF'
import boto3, json, base64, os, sys
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ver    = os.environ["T20_VER"]
pkg    = os.environ["T20_PKG"]
region = os.environ["REGION"]

sm  = boto3.client("secretsmanager", region_name=region)
ddb = boto3.resource("dynamodb", region_name=region)

try:
    secret   = sm.get_secret_value(SecretId="digilux-ota-signing-key")
    key_data = json.loads(secret["SecretString"])
    priv_key = serialization.load_pem_private_key(key_data["privateKey"].encode(), password=None)
    pub_key  = priv_key.public_key()

    item  = ddb.Table("digilux_ota_packages").get_item(
        Key={"packageName": pkg, "version": ver}
    )["Item"]
    sha256        = item["sha256"]
    sig_b64       = item["signature"]
    artifact_size = int(item["artifactSize"])

    signing_input = f"{ver}|{artifact_size}|{pkg}|{sha256}".encode()
    sig_bytes     = base64.b64decode(sig_b64)
    pub_key.verify(sig_bytes, signing_input, ec.ECDSA(hashes.SHA256()))
    print("VALID")
except Exception as e:
    print(f"INVALID:{e}")
PYEOF
  )
  [ "$T20_RESULT" = "VALID" ] \
    && _pass "T20.1 (+) ECDSA signature verifies over version|size|packageName|sha256" \
    || _fail "T20.1 (+) ECDSA signature invalid: $T20_RESULT"

  # artifactSize in DynamoDB must be the enriched tar size, not the raw upload size
  T20_STORED_SIZE=$(aws dynamodb get-item \
    --table-name digilux_ota_packages \
    --key "{\"packageName\":{\"S\":\"${T19_PKG}\"},\"version\":{\"S\":\"${T20_VER}\"}}" \
    --region "$REGION" \
    --query 'Item.artifactSize.N' --output text 2>/dev/null)
  [ -n "$T20_STORED_SIZE" ] && [ "$T20_STORED_SIZE" != "$T20_RAW_SIZE" ] \
    && _pass "T20.2 (+) artifactSize is enriched size ($T20_STORED_SIZE bytes), not raw upload ($T20_RAW_SIZE bytes)" \
    || _fail "T20.2 (+) artifactSize mismatch: stored=$T20_STORED_SIZE raw=$T20_RAW_SIZE (should differ after manifest enrichment)"

  # Verify old single-field signature does NOT verify (proves scheme changed)
  T20_OLD_RESULT=$(T20_VER="$T20_VER" T20_PKG="$T19_PKG" REGION="$REGION" python3.9 <<'PYEOF'
import boto3, json, base64, os
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ver    = os.environ["T20_VER"]
pkg    = os.environ["T20_PKG"]
region = os.environ["REGION"]

sm  = boto3.client("secretsmanager", region_name=region)
ddb = boto3.resource("dynamodb", region_name=region)

secret   = sm.get_secret_value(SecretId="digilux-ota-signing-key")
key_data = json.loads(secret["SecretString"])
priv_key = serialization.load_pem_private_key(key_data["privateKey"].encode(), password=None)
pub_key  = priv_key.public_key()

item    = ddb.Table("digilux_ota_packages").get_item(
    Key={"packageName": pkg, "version": ver}
)["Item"]
sha256  = item["sha256"]
sig_b64 = item["signature"]

sig_bytes = base64.b64decode(sig_b64)
try:
    pub_key.verify(sig_bytes, sha256.encode(), ec.ECDSA(hashes.SHA256()))
    print("VALID")   # should NOT happen
except Exception:
    print("INVALID") # expected — old single-field input no longer works
PYEOF
  )
  [ "$T20_OLD_RESULT" = "INVALID" ] \
    && _pass "T20.3 (+) Old single-field (sha256-only) signature input rejected — 4-field scheme enforced" \
    || _fail "T20.3 (+) Old single-field input still verifies — signature scheme may not have changed"
fi
_t19_cleanup "$T19_PKG" "$T20_VER"


# ─────────────────────────────────────────────────────────────────────────────
_section "RESULTS"
# ─────────────────────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL + WARN))
echo ""
echo "  Total: $TOTAL   ✓ Passed: $PASS   ✗ Failed: $FAIL   ⚠ Warnings: $WARN"
echo ""

if [ ${#FAILED_TESTS[@]} -gt 0 ]; then
  echo "  Failed tests:"
  for t in "${FAILED_TESTS[@]}"; do echo "    - $t"; done
fi

[ "$FAIL" -eq 0 ] && echo "  OVERALL: PASS" || echo "  OVERALL: FAIL"
