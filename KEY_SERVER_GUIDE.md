# Digilux OTA Key Server — How It Works

*Last updated: 2026-09-24 · Author: Digilux Engineering*

---

## Table of Contents

1. [Overview](#overview)
2. [The Problem](#the-problem)
3. [Key Concepts](#key-concepts)
4. [Solution Architecture](#solution-architecture)
5. [Flow Diagrams](#flow-diagrams)
6. [Security Properties](#security-properties)
7. [API Reference](#api-reference)
8. [Operational Guide](#operational-guide)
9. [Developer Integration Guide](#developer-integration-guide)

---

## Overview

This document explains the **Digilux OTA Key Server** — a small, secure service that manages the cryptographic keys used to protect firmware updates delivered to customer devices.

The key server is hosted entirely on **Digilux-controlled infrastructure**, separate from any customer (e.g. Honeywell) AWS account. This separation is intentional and is the whole point of the feature.

### Who should read this

This document is written for anyone who wants to understand how the key server works — no deep cryptography knowledge required. Technical terms are explained when first introduced. Developers integrating with the key server will find a step-by-step guide in the final section.

### What the key server does in one sentence

> The key server holds the master secret key; it issues "locked boxes" (wrapped keys) at upload time and opens them on demand at download time — so Honeywell never sees the secret.

---

## The Problem

### Why encryption alone is not enough

Digilux firmware files (`.tar`, `.bin`, `.jar`) are encrypted before being stored in AWS S3. The encrypted file is safe at rest. But to decrypt it, the device needs a key.

The naive approach is to store the decryption key somewhere in AWS — perhaps in a DynamoDB record next to the encrypted artifact. This is exactly what the system did in its early design.

### The Honeywell constraint

Digilux's OTA system will be deployed into **Honeywell's AWS account** for their production environment. Honeywell engineers have administrative access to their own AWS account — as they should.

If the decryption key lives inside Honeywell's AWS, Honeywell could:

- Read the key directly from DynamoDB or Secrets Manager
- Decrypt any firmware artifact without going through Digilux
- Bypass Digilux's licensing, versioning, or entitlement controls entirely

Honeywell raised this concern explicitly: *"We will not approve any architecture where a critical secret lives in our AWS account with a dependency on Digilux."*

### The other obvious option — and why it also fails

Storing the key on **Digilux's own OTA AWS account** creates the same problem from the other side: Digilux's OTA account is a shared environment. An accidental IAM misconfiguration, a compromised credential, or a noisy-neighbour Lambda could expose keys across all customers.

### The right answer

The decryption key must live **outside both** AWS accounts, on infrastructure that:

1. Only Digilux controls and operates
2. Is never accessible to Honeywell AWS IAM principals
3. Can be audited and rotated without touching Honeywell's environment

That is exactly what the Digilux Key Server provides.

---

## Key Concepts

Before the architecture makes sense, a few terms need to be defined clearly.

### Symmetric encryption (AES-256-GCM)

**AES** (Advanced Encryption Standard) is the industry-standard algorithm for encrypting data with a shared secret key. **256** refers to the key length in bits — longer means harder to brute-force. **GCM** (Galois/Counter Mode) is an operating mode that also produces a short authentication tag, proving the data was not tampered with after encryption.

We use AES-256-GCM for two purposes:
- Encrypting the firmware artifact
- Wrapping (encrypting) the per-artifact key with the master key

### Data key

A **data key** (also called a content encryption key, or CEK) is a randomly generated 32-byte (256-bit) secret used to encrypt exactly one artifact. A new data key is generated for every upload. If one data key is ever compromised, only that one artifact is affected.

### Master key

The **master key** is a long-lived 32-byte secret that lives exclusively on the Digilux Key Server. It is never stored in any AWS account. Its only job is to *wrap* (encrypt) and *unwrap* (decrypt) data keys.

### Envelope encryption

**Envelope encryption** is the technique of encrypting a key with another key. The analogy:

> Imagine putting a letter (the data key) in a locked box (encrypted with the master key). You can ship the locked box anywhere — even to someone you don't fully trust — because only the person with the master key can open the box.

The firmware artifact is encrypted with the data key. The data key is wrapped by the master key. Only the wrapped key (the locked box) is stored in Honeywell's AWS. The master key never leaves Digilux infrastructure.

### Wrapped key format

A wrapped key is stored as a text string with the format:

```
v1:<base64url-encoded bytes>
```

The `v1:` prefix makes it easy to support key rotation in the future — a new master key version would produce `v2:` wrapped keys, and both can be active simultaneously during a migration.

The binary payload inside is always 60 bytes: a 12-byte random nonce, 32 bytes of encrypted key material, and a 16-byte authentication tag.

### Presigned URL

An AWS **presigned URL** is a temporary, time-limited HTTPS link that grants read access to a specific S3 object. The device downloads the encrypted artifact using this URL. The URL does not reveal the encryption key — the device still needs the unwrapped data key to decrypt the bytes it downloads.

---

## Solution Architecture

### Components and where they live

```
┌─────────────────────────────────────────────────────────────────┐
│  DIGILUX INFRASTRUCTURE                                         │
│                                                                 │
│  ┌──────────────────────────────────┐                          │
│  │       Digilux Key Server         │                          │
│  │  (keys.digilux.co.in)            │                          │
│  │                                  │                          │
│  │  • Holds master key              │                          │
│  │  • POST /wrap   → wrapped key    │                          │
│  │  • POST /unwrap → plain key      │                          │
│  │  • GET  /health                  │                          │
│  └──────────────────────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
           ▲ wrap request          ▲ unwrap request
           │ (upload time)         │ (consent time)
           │                       │
┌─────────────────────────────────────────────────────────────────┐
│  HONEYWELL AWS ACCOUNT (or Digilux OTA account)                 │
│                                                                 │
│  ┌────────────────────┐    ┌────────────────────────────────┐  │
│  │  artifact_processor│    │       user_consent Lambda      │  │
│  │  Lambda            │    │                                │  │
│  │  (runs on upload)  │    │  (runs when device owner       │  │
│  └────────┬───────────┘    │   taps "Install Update")       │  │
│           │                └────────────────────────────────┘  │
│           │                                                     │
│  ┌────────▼────────────────────────────────────────────────┐   │
│  │  DynamoDB: digilux_ota_packages                         │   │
│  │                                                         │   │
│  │  packageName | version | encS3Key | wrappedDataKey      │   │
│  │  HomeAsst... │  4.5.0  │ enc/uuid │ v1:ABC...XYZ        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  S3 Bucket: digilux-ota-artifacts                       │   │
│  │                                                         │   │
│  │  enc/<uuid>.enc   ← encrypted artifact (AES-256-GCM)   │   │
│  │  sig/<uuid>.sig   ← ECDSA signature                    │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### What each component stores

| Component | Stores | Does NOT store |
|-----------|--------|----------------|
| Key Server (Digilux infra) | Master key (in Secrets Manager) | Any artifact data |
| DynamoDB (Honeywell AWS) | Wrapped data key (`v1:...`) | Master key, plain data key |
| S3 (Honeywell AWS) | Encrypted artifact | Plain artifact, data key |
| Device (on-premise) | Nothing permanent | Receives plain key only during active OTA |

### Why this is safe even if Honeywell's AWS is compromised

An attacker with full read access to Honeywell's AWS would find:
- The encrypted artifact (useless without the data key)
- The wrapped data key `v1:ABC...XYZ` (useless without the master key)
- No master key anywhere

To decrypt anything, they would also need to compromise the Digilux Key Server — a completely separate infrastructure that Honeywell has no access to.

---

## Flow Diagrams

### Flow 1 — Firmware Upload (Admin side)

This flow runs once per firmware version, when a Digilux admin uploads a new `.tar` or `.bin` file through the Admin Web UI.

```
Admin Browser          artifact_processor Lambda          Key Server
     │                         │                            │
     │  POST /upload            │                            │
     ├──────────────────────────►│                            │
     │                         │  1. Generate random         │
     │                         │     32-byte data key (dek)  │
     │                         │                            │
     │                         │  2. Encrypt artifact with   │
     │                         │     dek  (AES-256-GCM)      │
     │                         │     → store enc/<uuid>.enc  │
     │                         │       in S3                 │
     │                         │                            │
     │                         │  3. POST /wrap              │
     │                         │     { "key": "<hex dek>" }  │
     │                         ├────────────────────────────►│
     │                         │                            │  4. Encrypt dek with
     │                         │                            │     master key
     │                         │                            │     → "v1:ABC...XYZ"
     │                         │  { "wrappedKey": "v1:..." } │
     │                         │◄────────────────────────────┤
     │                         │                            │
     │                         │  5. Store wrappedDataKey    │
     │                         │     in DynamoDB package     │
     │                         │     record. Delete plain    │
     │                         │     dek from memory.        │
     │  201 Created             │                            │
     │◄─────────────────────────┤                            │
```

**After this flow:** S3 has the encrypted artifact. DynamoDB has the wrapped key. The plain data key exists nowhere — it was only in Lambda memory during processing.

---

### Flow 2 — Device Update (Device owner consents)

This flow runs once per device per update, when the device owner taps "Install Update" in the Digilux app.

```
Device Owner App    user_consent Lambda       Key Server       IoT Core
     │                    │                     │                │
     │ POST /consent      │                     │                │
     │ { accepted: true } │                     │                │
     ├────────────────────►│                     │                │
     │                    │ 1. Verify user owns  │                │
     │                    │    the device        │                │
     │                    │                     │                │
     │                    │ 2. Read wrappedKey   │                │
     │                    │    from DynamoDB     │                │
     │                    │                     │                │
     │                    │ 3. POST /unwrap      │                │
     │                    │    { "wrappedKey":   │                │
     │                    │      "v1:ABC...XYZ" }│                │
     │                    ├─────────────────────►│                │
     │                    │                     │ 4. Decrypt with │
     │                    │                     │    master key   │
     │                    │  { "key": "<hex>" } │                │
     │                    │◄─────────────────────┤                │
     │                    │                     │                │
     │                    │ 5. Generate S3       │                │
     │                    │    presigned URL     │                │
     │                    │                     │                │
     │                    │ 6. Create IoT Job    │                │
     │                    │    with { presignedUrl,              │
     │                    │           dataKey (hex),             │
     │                    │           sha256, signature }        │
     │                    ├──────────────────────────────────────►│
     │  202 Accepted      │                     │                │
     │◄───────────────────┤                     │                │
```

```
               Device (controller firmware)
                     │
                     │ IoT Job delivered via MQTT
                     │ { presignedUrl, dataKey, sha256, signature }
                     │
                     │ 1. Download encrypted artifact from presignedUrl
                     │ 2. Verify ECDSA signature
                     │ 3. Decrypt with dataKey (AES-256-GCM)
                     │ 4. Install firmware
                     │ 5. Erase dataKey from memory
                     │
                     ▼
                Firmware updated
```

**After this flow:** The plain data key was in Lambda memory and device RAM only during active use. It is never written to disk on either side.

---

## Security Properties

### Layered security at every stage

#### At rest (in AWS)

| Data | Where stored | Protected by |
|------|-------------|-------------|
| Firmware artifact | S3 (`enc/<uuid>.enc`) | AES-256-GCM encryption with per-artifact key |
| Data key | DynamoDB (`wrappedDataKey`) | AES-256-GCM encryption with master key |
| Master key | Digilux Secrets Manager | AWS KMS + Secrets Manager access controls |
| ECDSA signing key | Digilux Secrets Manager | Same |

#### In transit

- All API calls use HTTPS (TLS 1.2+)
- Presigned S3 URLs are time-limited (1 hour for small files, up to 48 hours for large files)
- IoT Job documents are delivered over MQTT with mutual TLS (device certificate + AWS IoT CA)

#### Per-artifact key isolation

Every firmware upload generates a **fresh random 32-byte data key**. If one key were ever extracted from a device during an active update, only that one artifact is affected. All other past and future firmware versions remain protected by their own independent keys.

#### URL masking

S3 keys are stored as opaque UUIDs (`enc/550e8400-e29b-41d4-a716-446655440000.enc`), not as `firmware/HomeAssistantUtility/4.5.0.enc`. A leaked presigned URL reveals nothing about the package name, version, or device type.

### What the key server never does

- It never touches the artifact itself — only 32-byte key material flows through it
- It never stores per-artifact keys — it only holds the master key
- It never talks to Honeywell's AWS — only Digilux's own Lambda functions call it
- It never logs key material — logs record operation results, not the key values

### Authentication to the key server

Each service that calls the key server (currently `artifact_processor` and `user_consent`) is issued a named API key stored in AWS Secrets Manager on **Digilux's** account (not Honeywell's). The key server validates the `Authorization: Bearer <api-key>` header on every request.

API keys are stored in Secrets Manager as a JSON map:
```json
{
  "artifact_processor": "dlx-ks-v1-abc123...",
  "user_consent":        "dlx-ks-v1-def456..."
}
```

This means each caller has its own revocable credential. If the `user_consent` key is compromised, only that key is rotated — `artifact_processor` is unaffected.

### Audit trail

Every wrap and unwrap operation emits an audit log entry:

```json
{
  "audit": true,
  "event": "key_unwrapped",
  "actor": "user_consent",
  "resource": "key_material",
  "result": "success",
  "ts": 1727123456.789
}
```

These logs go to AWS CloudWatch Logs on Digilux infrastructure. Every successful and failed attempt is recorded, enabling detection of unusual access patterns.

---

## API Reference

Base URL: `https://keys.digilux.co.in` (actual URL set at deployment)

All requests require:
```
Authorization: Bearer <api-key>
Content-Type: application/json
```

---

### GET /health

Returns the health of the key server. Use for monitoring and smoke tests.

**Request:** No body required.

**Response 200:**
```json
{
  "status": "ok",
  "version": "v1"
}
```

**Response 401:** Invalid or missing API key.

---

### POST /wrap

Wraps (encrypts) a 32-byte data key using the master key. Call this once per firmware artifact, immediately after encrypting the artifact.

**Request body:**
```json
{
  "key": "a1b2c3d4e5f6..."   // 64-character hex string (32 bytes)
}
```

**Response 200:**
```json
{
  "wrappedKey": "v1:ABC...XYZ"
}
```

The `wrappedKey` string is safe to store in DynamoDB. It is meaningless without the master key.

**Error responses:**

| Code | Meaning |
|------|---------|
| 400  | Missing or invalid `key` field (must be 64-char hex) |
| 401  | Invalid or missing API key |
| 500  | Internal error (master key unavailable, etc.) |

---

### POST /unwrap

Unwraps (decrypts) a previously wrapped data key. Call this at consent time, to obtain the plain data key for embedding in the IoT Job document.

**Request body:**
```json
{
  "wrappedKey": "v1:ABC...XYZ"
}
```

**Response 200:**
```json
{
  "key": "a1b2c3d4e5f6..."   // 64-character hex string (plain data key)
}
```

**Error responses:**

| Code | Meaning |
|------|---------|
| 400  | Missing or invalid `wrappedKey` field |
| 401  | Invalid or missing API key |
| 422  | Decryption failed — key was tampered with or wrong master key |
| 500  | Internal error |

---

### /api/v1/ota/keys/* aliases

The key server also accepts requests at:
- `GET  /api/v1/ota/keys/health`
- `POST /api/v1/ota/keys/wrap`
- `POST /api/v1/ota/keys/unwrap`

These are equivalent to the short paths and exist for API Gateway routing compatibility.

---

## Operational Guide

### Provisioning the key server

#### Step 1: Generate the master key

On a secure machine (not the Lambda itself), generate a cryptographically random 32-byte key and store it in base64:

```bash
python3 -c "
import os, base64
key = os.urandom(32)
print(base64.b64encode(key).decode())
"
```

Store the result in AWS Secrets Manager on **Digilux infrastructure** (not Honeywell's account):

```bash
aws secretsmanager create-secret \
  --name digilux/ota/key-server/master-key \
  --secret-string '{"masterKey": "<base64-value>"}'
```

#### Step 2: Provision API keys for callers

Generate one key per service:

```bash
python3 -c "
import secrets
print('dlx-ks-v1-' + secrets.token_hex(32))
"
```

Store all keys in a single secret:

```bash
aws secretsmanager create-secret \
  --name digilux/ota/key-server/api-keys \
  --secret-string '{
    "artifact_processor": "dlx-ks-v1-...",
    "user_consent": "dlx-ks-v1-..."
  }'
```

Then pass the caller's own key as `KEY_SERVER_API_KEY` in that Lambda's environment variables.

#### Step 3: Deploy the Lambda

```bash
cd infrastructure/06_lambdas/digilux_ota_key_server
pip install cryptography -t package/
cp lambda_function.py package/
cd package && zip -r ../key_server.zip .
aws lambda create-function \
  --function-name digilux_ota_key_server \
  --runtime python3.9 \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://key_server.zip \
  --environment Variables='{
    "MASTER_KEY_SECRET": "digilux/ota/key-server/master-key",
    "API_KEYS_SECRET": "digilux/ota/key-server/api-keys"
  }'
```

---

### Rotating the master key

Key rotation is safe and zero-downtime because of the `v1:` version prefix.

1. Generate a new master key and store it as a new Secrets Manager version with a staged label (e.g. `AWSPENDING`)
2. Update the Lambda to read both `v1` and `v2` master keys
3. New uploads produce `v2:` wrapped keys
4. Existing `v1:` wrapped keys continue to unwrap using the old master key
5. After all devices have been updated (no outstanding `v1:` wrapped keys in active deployments), retire the old key

---

### Rotating an API key (one caller compromised)

1. Generate a new API key for the affected caller
2. Update the Secrets Manager secret to replace that caller's entry
3. Update the caller Lambda's environment variable `KEY_SERVER_API_KEY`
4. The old key stops working immediately after the secret update (Lambda cache TTL is 5 minutes)
5. Other callers are unaffected

---

### Adding a new caller

1. Generate a new API key: `python3 -c "import secrets; print('dlx-ks-v1-' + secrets.token_hex(32))"`
2. Add it to the Secrets Manager JSON: `{..., "new_service": "dlx-ks-v1-..."}`
3. Set `KEY_SERVER_API_KEY=dlx-ks-v1-...` in the new Lambda's environment variables

---

### Monitoring

Key metrics to alarm on in CloudWatch:

| Metric | Alarm threshold | What it means |
|--------|----------------|---------------|
| `key_wrap_error` log entries | > 0 in 5 min | Upload pipeline broken |
| `key_unwrap_error` log entries | > 0 in 5 min | Consent pipeline broken |
| `auth_failed` log entries | > 10 in 1 min | Possible credential abuse |
| Lambda errors | > 1% | General health issue |
| Lambda duration p99 | > 2000ms | Secrets Manager latency |

All log events are structured JSON, making CloudWatch Logs Insights queries straightforward:

```sql
filter audit = 1 and event = "key_unwrapped"
| stats count() by actor, bin(5m)
```

---

## Developer Integration Guide

This section explains exactly what changes are needed in `artifact_processor` and `user_consent` to integrate with the key server.

### artifact_processor changes (upload time)

**Current behaviour:** The Lambda generates a data key, encrypts the artifact, and stores the plain data key in DynamoDB.

**Required change:** After encrypting the artifact, call `/wrap` and store the wrapped key instead.

```python
import os, requests

KEY_SERVER_URL = os.environ["KEY_SERVER_URL"]      # e.g. https://keys.digilux.co.in
KEY_SERVER_API_KEY = os.environ["KEY_SERVER_API_KEY"]

def _wrap_data_key(dek_hex: str) -> str:
    """Send plain data key to key server; receive wrapped key."""
    resp = requests.post(
        f"{KEY_SERVER_URL}/wrap",
        json={"key": dek_hex},
        headers={"Authorization": f"Bearer {KEY_SERVER_API_KEY}"},
        timeout=5,
    )
    resp.raise_for_status()          # 4xx/5xx → exception → Lambda returns 500
    return resp.json()["wrappedKey"]  # "v1:ABC...XYZ"
```

Then in the DynamoDB `put_item` call, store `wrappedDataKey` instead of `dataKey`:

```python
# Before (insecure):
"dataKey": dek_hex

# After (secure):
"wrappedDataKey": _wrap_data_key(dek_hex)
```

**Important:** Clear `dek_hex` from all local variables after wrapping — it must not persist in DynamoDB or logs.

---

### user_consent changes (consent time)

**Current behaviour:** The Lambda reads the plain `dataKey` from DynamoDB and embeds it in the IoT Job document.

**Required change:** Read `wrappedDataKey` from DynamoDB, call `/unwrap`, use the resulting plain key in the Job document.

```python
KEY_SERVER_URL = os.environ["KEY_SERVER_URL"]
KEY_SERVER_API_KEY = os.environ["KEY_SERVER_API_KEY"]

def _unwrap_data_key(wrapped_key: str) -> str:
    """Send wrapped key to key server; receive plain data key."""
    resp = requests.post(
        f"{KEY_SERVER_URL}/unwrap",
        json={"wrappedKey": wrapped_key},
        headers={"Authorization": f"Bearer {KEY_SERVER_API_KEY}"},
        timeout=5,
    )
    if resp.status_code == 422:
        raise ValueError("Key decryption failed — possible tampering")
    resp.raise_for_status()
    return resp.json()["key"]   # 64-char hex, plain data key
```

Then build the IoT Job document:

```python
wrapped_key = pkg["wrappedDataKey"]
dek_hex = _unwrap_data_key(wrapped_key)    # call key server

job_doc = {
    "operationType": operation_type,
    "packageName":   package_name,
    "version":       version,
    "artifact": {
        "presignedUrl": presigned_url,
        "sha256":       sha256,
        "signature":    signature,
        "size":         artifact_size,
        "dataKey":      dek_hex,       # plain key, in-flight only
    },
    ...
}
del dek_hex   # erase from memory after embedding in job_doc
```

The plain `dek_hex` is embedded in the IoT Job document, which is encrypted by AWS IoT in transit and at rest, and delivered to the specific target device only.

---

### Error handling guidance

| Scenario | HTTP status from key server | Recommended Lambda behaviour |
|----------|---------------------------|------------------------------|
| Key server unreachable | `requests.exceptions.ConnectionError` | Return 503 to client — do not swallow the error |
| Invalid API key | 401 | Return 500 — this is a configuration bug, alert on-call |
| Artifact data key tampered in DB | 422 | Return 500 + trigger alert — possible data integrity issue |
| Key server returns 5xx | 500 | Return 503 — retry is safe (wrap/unwrap are idempotent for same input) |

---

### Testing locally

The key server Lambda can be invoked directly or via SAM:

```bash
# Health check
curl https://keys.digilux.co.in/health \
  -H "Authorization: Bearer dlx-ks-v1-..."

# Wrap a key
curl -X POST https://keys.digilux.co.in/wrap \
  -H "Authorization: Bearer dlx-ks-v1-..." \
  -H "Content-Type: application/json" \
  -d '{"key": "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"}'

# Unwrap it back (wrappedKey = value from wrap response)
curl -X POST https://keys.digilux.co.in/unwrap \
  -H "Authorization: Bearer dlx-ks-v1-..." \
  -H "Content-Type: application/json" \
  -d '{"wrappedKey": "v1:<value from wrap response>"}'
# The key in the response should match the original.
```

The test suite covers 58 cases including all error paths:

```bash
pip install pytest cryptography
pytest infrastructure/tests/test_key_server.py -v
```
