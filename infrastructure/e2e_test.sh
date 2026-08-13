#!/bin/bash
# OTA End-to-End Production Test Suite
set -uo pipefail

TOKEN=$(cat /tmp/ota_admin_token.txt)
NON_ADMIN_TOKEN=$(cat /tmp/ota_nonadmin_token.txt)
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

code=$(http_code POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"controller-app\",\"version\":\"2.0.0\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\"}")
assert_code "$code" "400" "Re-deploy already-installed version → 400"

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

# Generate test artifact and compute its SHA256 checksum upfront
echo "test content for OTA E2E validation $(date)" | gzip > /tmp/test_artifact.bin
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
echo "  → Waiting for artifact_processor (up to 15s)..."
for i in $(seq 1 15); do
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
  || _fail "Package not ACTIVE after 15s — status=$STATUS"

# Verify SHA256 and signature were written
SHA256=$(aws dynamodb get-item \
  --table-name digilux_ota_packages \
  --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION}\"}}" \
  --region "$REGION" --query 'Item.sha256.S' --output text 2>/dev/null)
SIG=$(aws dynamodb get-item \
  --table-name digilux_ota_packages \
  --key "{\"packageName\":{\"S\":\"${TEST_PKG_NAME}\"},\"version\":{\"S\":\"${TEST_VERSION}\"}}" \
  --region "$REGION" --query 'Item.signature.S' --output text 2>/dev/null)
[ -n "$SHA256" ] && [ "$SHA256" != "None" ] \
  && _pass "SHA256 written by artifact_processor: ${SHA256:0:16}..." \
  || _fail "SHA256 missing after ACTIVE promotion"
[ -n "$SIG" ] && [ "$SIG" != "None" ] \
  && _pass "ECDSA signature written by artifact_processor" \
  || _fail "Signature missing after ACTIVE promotion"

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
  assert_field "$DEPLOY" "status" "QUEUED" "New deployment starts as QUEUED"
  assert_field "$DEPLOY" "rolloutStage" "CANARY" "Rollout stage preserved"
  assert_has_field "$DEPLOY" "iotJobArn" "Response has iotJobArn"
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
  assert_has_field "$JOB" "iotStatus" "GET job includes live iotStatus"
  assert_has_field "$JOB" "iotJobStatus" "GET job includes iotJobStatus"
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
else
  _warn "Skipping status handler tests — no job ID"
fi

# ─────────────────────────────────────────────────────────────────────────────
_section "T11 — SIMULATE FAILED UPDATE (with rollback detail)"
# ─────────────────────────────────────────────────────────────────────────────

# Create a second deployment of an earlier version
# First reset the device data version so we can create a new job
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET installedVersions.#pkg = :v" \
  --expression-attribute-names '{"#pkg":"controller-app"}' \
  --expression-attribute-values "{\":v\":{\"S\":\"3.0.0\"}}" \
  --region "$REGION" > /dev/null 2>&1
echo "  → Reset device to controller-app@3.0.0 for failure test"

FAIL_DEPLOY=$(call POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"controller-app\",\"version\":\"4.0.0\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
FAIL_JOB_ID=$(echo "$FAIL_DEPLOY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))" 2>/dev/null)

if [ -n "$FAIL_JOB_ID" ] && [ "$FAIL_JOB_ID" != "None" ]; then
  _pass "Created failure-test job: $FAIL_JOB_ID"

  # Simulate FAILED with rollback detail
  printf '{"jobId":"%s","status":"FAILED","progress":0,"packageName":"controller-app","version":"4.0.0","error":"tarball extraction failed: disk full","statusDetail":"Install failed — previous version restored"}' \
    "$FAIL_JOB_ID" > /tmp/mqtt_failed.json

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
aws dynamodb update-item \
  --table-name digilux_device_data \
  --key "{\"deviceId\":{\"S\":\"${DEVICE_ID}\"},\"macAddress\":{\"S\":\"${DEVICE_MAC}\"}}" \
  --update-expression "SET installedVersions.#pkg = :v" \
  --expression-attribute-names '{"#pkg":"controller-app"}' \
  --expression-attribute-values "{\":v\":{\"S\":\"2.0.0\"}}" \
  --region "$REGION" > /dev/null 2>&1

ABORT_DEPLOY=$(call POST "/api/v1/ota/deployments" \
  "{\"packageName\":\"controller-app\",\"version\":\"4.0.0\",\"targetType\":\"THING\",\"targetId\":\"${DEVICE_ID}\",\"rolloutStage\":\"CANARY\"}")
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
