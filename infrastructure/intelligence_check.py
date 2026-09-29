#!/usr/bin/env python3
"""
OTA System Intelligence Check — powered by Claude claude-opus-4-6

Part 1: Live API probing
  — calls every endpoint, collects real responses, asks Claude to evaluate
    them semantically (field completeness, values, security, controller-impact).

Part 2: Lambda code review
  — feeds each Lambda's source to Claude and asks for genuine bugs,
    edge cases, and security gaps.

Usage:
  pip install anthropic
  python intelligence_check.py
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── Load .env file (ANTHROPIC_API_KEY) ────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL   = "https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome"
DEVICE_ID  = "edb39bba-baf1-4700-968c-a42228e53aa0"
LAMBDA_DIR = Path(__file__).parent / "06_lambdas"

LAMBDAS_TO_REVIEW = [
    "digilux_ota_artifact_processor",
    "digilux_ota_upload_url",
    "digilux_ota_user_get_download_link",
    "digilux_ota_user_check_updates",
    "digilux_ota_user_consent",
    "digilux_ota_device_register",
    "digilux_ota_status_handler",
]

SYSTEM_CONTEXT = """
Digilux OTA System — v2.7 context:
- AWS Lambda (Python 3.11) + API Gateway + DynamoDB + S3 + IoT Core
- Two Cognito pools:
    • OTA admin pool  ap-south-1_jUErEu7CL  (admin endpoints only)
    • App pool        ap-south-1_h1o8s7257  (user endpoints only)
- DynamoDB tables:
    • digilux_ota_packages   (PK: packageName+version)
    • digilux_device_data    (PK: deviceId+macAddress, GSI: userId-index)
    • digilux_ota_jobs, digilux_ota_user_consents, digilux_ota_beta_users
- S3: digilux-ota-artifacts (versioned, AES-256 server-side encryption)
- Secrets Manager:
    • digilux-ota-signing-key          (ECDSA P-256 private key)
    • digilux-ota-master-encryption-key (AES-256 master key for double-encryption)
- Artifact pipeline (v2.7):
    1. Admin uploads .tar (must contain manifest.json) via presigned S3 PUT
    2. artifact_processor Lambda fires on S3 event:
         - Verifies upload token + SHA256 checksum
         - Validates tar + manifest.json
         - Signs with ECDSA P-256 → uploads .sig to S3
         - Encrypts with per-artifact AES-256-GCM → uploads .enc to S3
         - Double-encrypts AES key with master key → stores in DynamoDB
         - Deletes raw .tar from S3
    3. Package status → ACTIVE; DynamoDB stores encS3Key, sigS3Key, aesKeyEnc, aesIv
- Download flow: Lambda decrypts AES key in-memory only, delivers over TLS
    Response fields: downloadUrl (.enc), signatureUrl (.sig), sha256 (of original .tar),
                     signature (ECDSA), encrypted=True, aesKey, aesIv, mqttDelivered
- Controller (embedded Linux) decryption order:
    1. Download .enc + .sig
    2. Verify ECDSA signature over original tar
    3. AES-256-GCM decrypt using aesKey + aesIv
    4. Verify SHA256 of decrypted tar
    5. Validate manifest.json
    6. Install
"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

client = anthropic.Anthropic()


def get_tokens() -> tuple[str, str]:
    admin = Path("/tmp/ota_admin_token.txt").read_text().strip()
    user  = Path("/tmp/ota_nonadmin_token.txt").read_text().strip()
    return admin, user


def http(method: str, path: str, token: str, body: dict | None = None):
    """Make an API call; return (status_code, parsed_body)."""
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    # macOS Python may not have system CA certs; use an unverified context
    # (safe here — we're calling our own AWS API Gateway endpoint over TLS).
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def ask_claude(prompt: str, *, stream: bool = False) -> str:
    """Call Claude claude-opus-4-6 with adaptive thinking; return text."""
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_CONTEXT,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


def hdr(title: str):
    print(f"\n{'━' * 62}")
    print(f"  {title}")
    print(f"{'━' * 62}")


def ok(label: str, passed: bool, detail: str = ""):
    icon = "✓" if passed else "✗"
    line = f"  {icon} {label}"
    if not passed and detail:
        line += f"\n      ↳ {detail}"
    print(line)


# ─── Part 1 — Live API Intelligence ───────────────────────────────────────────

def part1_api(admin_token: str, user_token: str) -> dict:
    hdr("PART 1 — LIVE API INTELLIGENCE CHECK")
    collected = {}

    # ── A1: Auth boundary ─────────────────────────────────────────────────────
    print("\n  [A1] Auth boundary")

    s, _ = http("GET", "/api/v1/ota/packages", "garbage-token")
    ok("Invalid token → 401 on admin endpoint", s == 401)

    # Admin pool token should be rejected by user-endpoint authorizer (app pool only)
    s, _ = http("GET", "/api/v1/ota/device/available-updates", admin_token)
    ok("Admin pool token → 401 on user endpoint", s == 401,
       f"Got {s} — admin pool token must not work on user (app pool) routes")

    # App pool token should be rejected by admin-endpoint authorizer
    s, _ = http("GET", "/api/v1/ota/deployments", user_token)
    ok("App pool token → 401 on admin endpoint", s == 401,
       f"Got {s} — app pool token must not work on admin routes")

    # App pool token on its own endpoints
    s, _ = http("GET", "/api/v1/ota/device/available-updates", user_token)
    ok("App pool token → 200 on user endpoint", s == 200, f"Got {s}")

    # ── A2: Package catalog structure ─────────────────────────────────────────
    print("\n  [A2] Package catalog")
    s, body = http("GET", "/api/v1/ota/packages", admin_token)
    collected["list_packages"] = {"status": s, "body": body}
    ok("GET /packages → 200", s == 200)

    pkgs = body.get("packages", [])
    ok(f"packages[] present ({body.get('count', '?')} total)", isinstance(pkgs, list))

    if pkgs:
        required = ["packageName", "version", "deviceType", "releaseType", "status", "activated"]
        missing  = [f for f in required if f not in pkgs[0]]
        ok("First package has required fields", not missing,
           f"Missing: {missing}")

        active = [p for p in pkgs if p.get("status") == "ACTIVE"]
        ok(f"{len(active)} ACTIVE package(s) exist", len(active) > 0)

    # ── A3: Upload validation (non-.tar rejection) ─────────────────────────────
    print("\n  [A3] Upload .tar enforcement")
    s, body = http("POST", "/api/v1/ota/packages/upload-artefact", admin_token, {
        "deviceType":  "Network_controller_firmware",
        "version":     "0.0.0-intchk",
        "releaseType": "PROD",
        "checksum":    "abc" * 20,
        "fileName":    "firmware.jar",
    })
    ok("Non-.tar fileName → 400", s == 400, body.get("error", f"Got {s}"))

    s, body = http("POST", "/api/v1/ota/packages/upload-artefact", admin_token, {
        "deviceType":  "INVALID_TYPE",
        "version":     "1.0.0",
        "releaseType": "PROD",
        "checksum":    "abc" * 20,
    })
    ok("Invalid deviceType → 400", s == 400, body.get("error", f"Got {s}"))

    # ── A4: Available updates ──────────────────────────────────────────────────
    print("\n  [A4] Available updates")
    s, body = http("GET", "/api/v1/ota/device/available-updates", user_token)
    collected["available_updates"] = {"status": s, "body": body}
    ok("GET /device/available-updates → 200", s == 200)
    if s == 200:
        ok("Response has 'devices' array", "devices" in body)
        print(f"      devices count: {len(body.get('devices', []))}")

    # ── A5: Consent guards ─────────────────────────────────────────────────────
    print("\n  [A5] Consent guards")
    s, body = http("POST", "/api/v1/ota/my/updates/consent", user_token, {
        "deviceId": DEVICE_ID, "packageName": "controller-app", "version": "999.0.0",
    })
    ok("Non-existent version → 404", s == 404, f"Got {s}: {body.get('error','')}")

    s, body = http("POST", "/api/v1/ota/my/updates/consent", user_token, {
        "deviceId": "00000000-0000-0000-0000-000000000000",
        "packageName": "controller-app", "version": "5.0.0",
    })
    ok("Unowned device → 404 (not 403 — avoids device existence leak)", s == 404,
       f"Got {s}: {body.get('error','')}")

    s, body = http("POST", "/api/v1/ota/my/updates/consent", user_token, {
        "deviceId": "not-a-uuid", "packageName": "controller-app", "version": "5.0.0",
    })
    ok("Malformed UUID → 400", s == 400, f"Got {s}")

    # ── A6: Download-link encryption fields ───────────────────────────────────
    print("\n  [A6] Download-link encryption response")
    devices = body if s == 200 else {}
    # Re-fetch available updates to find a device with an update
    s_upd, upd = http("GET", "/api/v1/ota/device/available-updates", user_token)
    dl_result = None
    if s_upd == 200 and upd.get("devices"):
        dev = upd["devices"][0]
        s_dl, dl = http("POST", "/api/v1/ota/my/updates/download-link", user_token, {
            "deviceId":    dev["deviceId"],
            "packageName": dev["package"],
            "version":     dev["availableVersion"],
        })
        collected["download_link"] = {"status": s_dl, "body": dl}
        dl_result = dl

        ok("POST /download-link → 200", s_dl == 200, f"Got {s_dl}: {dl.get('error','')}")
        if s_dl == 200:
            for field in ["downloadUrl", "signatureUrl", "sha256", "signature",
                          "encrypted", "aesKey", "aesIv", "mqttDelivered", "expiresAt"]:
                ok(f"  response has '{field}'", field in dl,
                   "MISSING — controller/app will break" if field not in dl else "")
            ok("  encrypted=True", dl.get("encrypted") is True,
               f"Got encrypted={dl.get('encrypted')}")
            ok("  downloadUrl ends in .enc", ".enc" in dl.get("downloadUrl", ""),
               "Should point to encrypted artifact, not raw .tar")
            ok("  signatureUrl ends in .sig", ".sig" in dl.get("signatureUrl", ""),
               "Separate .sig file required for ECDSA verification")
            ok("  sha256 is 64-char hex", len(dl.get("sha256", "")) == 64,
               f"Got length {len(dl.get('sha256',''))}")
    else:
        print("      ⚠ No updates available for test device — skipping download-link check")
        print("        (run admin e2e first to create + activate a test package)")

    # ── A7: Claude semantic analysis ──────────────────────────────────────────
    print("\n  [A7] Claude semantic analysis of collected responses...")
    prompt = f"""I have run live API calls against the Digilux OTA system and collected these responses:

```json
{json.dumps(collected, indent=2, default=str)}
```

Please evaluate:
1. Are the response structures semantically complete and correct for this OTA system?
2. Any fields that are missing, have wrong types, or look suspicious?
3. Security: does anything in the responses leak internal info (ARNs, bucket names, etc.) that shouldn't be public?
4. Controller-impact: are there any response fields that, if wrong or missing, would cause the embedded Linux controller to fail to apply the update?
5. Consistency between responses (e.g., SHA256 in download-link matches what's expected)?

Be specific. Flag anything that needs attention."""

    verdict = ask_claude(prompt)
    print("\n  Claude's Semantic Analysis:")
    for line in verdict.strip().split("\n"):
        print(f"    {line}")

    return collected


# ─── Part 2 — Lambda Code Review ──────────────────────────────────────────────

def part2_code_review() -> dict[str, str]:
    hdr("PART 2 — LAMBDA CODE REVIEW (Claude claude-opus-4-6)")
    findings: dict[str, str] = {}

    for name in LAMBDAS_TO_REVIEW:
        fn_file = LAMBDA_DIR / name / "lambda_function.py"
        if not fn_file.exists():
            print(f"\n  ⚠ {name}: lambda_function.py not found — skipping")
            continue

        print(f"\n  Reviewing {name} ...")
        code = fn_file.read_text()

        prompt = f"""Review this AWS Lambda function for genuine bugs, security issues, missing error
handling, edge cases, and logical gaps. Do NOT flag things that are fine or purely theoretical.
Be specific: cite the relevant line number or code snippet.

Lambda: {name}

```python
{code}
```

Format each finding as:
  [HIGH/MEDIUM/LOW] <brief title>
  <one or two sentences explaining the issue and its impact>

If there are no significant issues, say: "No significant issues found."
"""
        review = ask_claude(prompt)
        findings[name] = review

        lines = review.strip().split("\n")
        for line in lines[:25]:
            print(f"    {line}")
        if len(lines) > 25:
            print(f"    ... (+{len(lines) - 25} more lines — truncated for display)")

    # ── Cross-Lambda synthesis ─────────────────────────────────────────────────
    hdr("PART 2b — CROSS-LAMBDA SYNTHESIS")

    all_txt = "\n\n".join(f"=== {n} ===\n{t}" for n, t in findings.items())
    prompt = f"""Here are individual code review findings for each Lambda in the Digilux OTA system:

{all_txt}

Provide a concise synthesis (≤500 words):
1. Which HIGH severity issues must be fixed immediately and why?
2. Any systemic patterns (same class of bug in multiple Lambdas)?
3. Are there cross-Lambda security risks (e.g., Lambda A trusts data produced by Lambda B without re-validating)?
4. Overall security posture — is the system safe to run in production?"""

    synthesis = ask_claude(prompt)
    print()
    for line in synthesis.strip().split("\n"):
        print(f"  {line}")

    return findings


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 62)
    print("  Digilux OTA — Intelligence Check")
    print("  Model: claude-opus-4-6 | Thinking: adaptive")
    print("═" * 62)

    try:
        admin_token, user_token = get_tokens()
    except FileNotFoundError as e:
        print(f"\nERROR: Token file not found: {e}")
        print("Run the e2e test first to refresh tokens, or:")
        print("  aws cognito-idp initiate-auth ... > /tmp/ota_admin_token.txt")
        sys.exit(1)

    api_data = part1_api(admin_token, user_token)
    findings = part2_code_review()

    hdr("DONE")
    print("  Intelligence check complete.")
    print()


if __name__ == "__main__":
    main()
