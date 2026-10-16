#!/usr/bin/env python3
"""
Smoke-test all Digilux OTA API endpoints.

Tests read-only / safe endpoints only — no state mutations.
Mutating endpoints (upload, create deployment, consent, abort) are explicitly skipped.

Run:
  python3 infrastructure/test_endpoints.py
"""

import json
import sys
import time
import boto3

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip install requests")

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_BASE  = "https://iot.digilux.co.in/smarthome"
USER_BASE   = "https://iot.digilux.co.in"

ADMIN_COGNITO_CLIENT = "2qmig1uh220ttntbl0gfvcde4f"
ADMIN_USER           = "mahesh.maney@gmail.com"
ADMIN_PASS           = "DigiluxAdmin@2026"

PKCE_SCRIPT = __file__.replace("test_endpoints.py", "get_pkce_token.py")

TEST_DEVICE_ID   = "edb39bba-baf1-4700-968c-a42228e53aa0"
TEST_PACKAGE     = "HomeAssistantUtility"
TEST_VERSION     = "4.5.0"

REGION = "ap-south-1"

# ── Colours ───────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ── Helpers ───────────────────────────────────────────────────────────────────

results = []

def _pass(name, detail=""):
    tag = f"{GREEN}PASS{RESET}"
    print(f"  {tag}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(("PASS", name))

def _fail(name, detail=""):
    tag = f"{RED}FAIL{RESET}"
    print(f"  {tag}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(("FAIL", name))

def _skip(name, reason=""):
    tag = f"{YELLOW}SKIP{RESET}"
    print(f"  {tag}  {name}" + (f"  — {reason}" if reason else ""))
    results.append(("SKIP", name))

def _section(title):
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

def check(name, resp, expected_status=200, check_fn=None):
    """Assert HTTP status and optionally run a custom check on the JSON body."""
    try:
        if resp.status_code != expected_status:
            _fail(name, f"HTTP {resp.status_code} (expected {expected_status}): {resp.text[:200]}")
            return None
        body = resp.json() if resp.text else {}
        if check_fn:
            msg = check_fn(body)
            if msg:
                _fail(name, msg)
                return None
        _pass(name, f"HTTP {resp.status_code}")
        return body
    except Exception as e:
        _fail(name, str(e))
        return None

# ── Step 1: Get tokens ────────────────────────────────────────────────────────

def get_admin_token():
    print(f"\n  Fetching admin token via USER_PASSWORD_AUTH …")
    client = boto3.client("cognito-idp", region_name=REGION)
    resp = client.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=ADMIN_COGNITO_CLIENT,
        AuthParameters={"USERNAME": ADMIN_USER, "PASSWORD": ADMIN_PASS},
    )
    token = resp["AuthenticationResult"]["IdToken"]
    print(f"  Admin token: {token[:12]}…{token[-8:]}")
    return token

def get_user_token():
    print(f"\n  Fetching user PKCE token …")
    # Import get_token from sibling script
    import importlib.util
    spec = importlib.util.spec_from_file_location("get_pkce_token", PKCE_SCRIPT)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    token = mod.get_token("demotesthw5@yopmail.com", "DigiluxTest@9900")
    print(f"  User token:  {token[:12]}…{token[-8:]}")
    return token

# ── Test groups ───────────────────────────────────────────────────────────────

def test_packages(admin_hdrs):
    _section("Packages (admin)")

    # 1. List all packages
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages", headers=admin_hdrs)
    body = check("List Packages (no filter)", r, 200,
                 lambda b: None if "packages" in b else "missing 'packages' key")

    # 2. List ACTIVE only — and pick a real version for subsequent tests
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages?status=ACTIVE", headers=admin_hdrs)
    active_body = check("List Packages (status=ACTIVE)", r, 200,
                        lambda b: None if "packages" in b else "missing 'packages' key")

    # Resolve a real (packageName, version) pair from the active list
    pkg_name = TEST_PACKAGE
    pkg_ver  = TEST_VERSION
    if active_body:
        pkgs = active_body.get("packages", [])
        # prefer HomeAssistantUtility; fall back to first available
        match = next((p for p in pkgs if p.get("packageName") == TEST_PACKAGE), None)
        if not match and pkgs:
            match = pkgs[0]
        if match:
            pkg_name = match["packageName"]
            pkg_ver  = match["version"]

    # 3. Get specific package version (dynamically resolved)
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages/{pkg_name}/{pkg_ver}",
                     headers=admin_hdrs)
    check(f"Get Package Status ({pkg_name}/{pkg_ver})", r, 200,
          lambda b: None if "packageName" in b or "status" in b else "missing status fields")

    # 4. List packages filtered by packageName
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages?packageName={TEST_PACKAGE}",
                     headers=admin_hdrs)
    check(f"List Packages (packageName={TEST_PACKAGE})", r, 200,
          lambda b: None if "packages" in b else "missing 'packages' key")

    # Skipped (mutating)
    _skip("Get Upload URL",         "mutating — creates PENDING record")
    _skip("Upload Artifact to S3",  "mutating — uploads binary")
    _skip("Complete Multipart",     "mutating — finalises upload")
    _skip("Activate / Withdraw",    "mutating — changes package state")
    _skip("Delete Package",         "destructive")


def test_device_admin(admin_hdrs):
    _section("Device (Admin)")

    r = requests.get(
        f"{ADMIN_BASE}/api/v1/controllers/{TEST_DEVICE_ID}/updates/available",
        headers=admin_hdrs,
    )
    # 200 = device registered; 404 = device not in inventory — both are valid
    if r.status_code == 200:
        check("Check Device Updates (Admin)", r, 200,
              lambda b: None if "deviceId" in b else "missing deviceId")
    elif r.status_code == 404:
        _pass("Check Device Updates (Admin)", "HTTP 404 — device not in inventory (expected for test device)")
    else:
        _fail("Check Device Updates (Admin)", f"HTTP {r.status_code}: {r.text[:200]}")


def test_deployments(admin_hdrs):
    _section("Deployments (admin)")

    # 1. List deployments
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/deployments?limit=20", headers=admin_hdrs)
    body = check("List Deployments", r, 200,
                 lambda b: None if ("jobs" in b or "deployments" in b) else "missing jobs/deployments key")

    # 2. Get latest deployment status (if any exist)
    if body:
        items = body.get("jobs") or body.get("deployments") or []
        if items:
            job_id = items[0].get("jobId") or items[0].get("deploymentId")
            if job_id:
                r2 = requests.get(f"{ADMIN_BASE}/api/v1/ota/deployments/{job_id}",
                                  headers=admin_hdrs)
                check(f"Get Deployment Status ({job_id[:30]}…)", r2, 200,
                      lambda b: None if "status" in b else "missing status")
            else:
                _skip("Get Deployment Status", "no jobId in first deployment")
        else:
            _skip("Get Deployment Status", "no deployments exist yet")

    _skip("Create Deployment", "mutating — creates IoT Job")
    _skip("Abort Deployment",  "destructive")


def test_beta_users(admin_hdrs):
    _section("Beta Users (admin)")

    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/beta-users", headers=admin_hdrs)
    check("List Beta Users", r, 200,
          lambda b: None if "users" in b else "missing 'users' key")

    _skip("Add Beta User",    "mutating")
    _skip("Remove Beta User", "destructive")
    _skip("Create BETA Deployment", "mutating")


def test_user_endpoints(user_hdrs):
    _section("End User Updates (PKCE token)")

    # Check Available Updates
    r = requests.get(f"{USER_BASE}/api/v1/ota/device/available-updates", headers=user_hdrs)
    body = check("Check Available Updates", r, 200,
                 lambda b: None if "devices" in b else "missing 'devices' key")
    if body:
        devices = body.get("devices", [])
        print(f"         devices={len(devices)}, totalUpdates={body.get('totalUpdates', 0)}, "
              f"message={repr(body.get('message',''))}")
        for d in devices:
            ota = d.get("otaStatus", "?")
            av  = d.get("availableVersion", "—")
            ins = d.get("installedVersion", "—")
            did = d.get("deviceId", "?")[:20]
            print(f"           device={did}… status={ota} installed={ins} available={av}")

    # 401 check — bad token
    r = requests.get(f"{USER_BASE}/api/v1/ota/device/available-updates",
                     headers={"Authorization": "Bearer bad_token_xyz"})
    if r.status_code == 401:
        _pass("Check Available Updates (bad token → 401)", f"HTTP {r.status_code}")
    else:
        _fail("Check Available Updates (bad token → 401)", f"Expected 401, got {r.status_code}")

    _skip("Give Update Consent",         "mutating — creates IoT Job")
    _skip("Get Update Status",           "needs a live jobId")
    _skip("Get Download Link (MQTT)",    "mutating — triggers MQTT publish")


def test_auth_endpoints(admin_hdrs):
    _section("Auth (token validation)")

    # Verify admin token works on a protected endpoint
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages", headers=admin_hdrs)
    if r.status_code == 200:
        _pass("Admin token accepted on List Packages")
    else:
        _fail("Admin token accepted on List Packages", f"HTTP {r.status_code}: {r.text[:150]}")

    # Verify unauthenticated request is rejected
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages")
    if r.status_code in (401, 403):
        _pass("Unauthenticated request rejected", f"HTTP {r.status_code}")
    else:
        _fail("Unauthenticated request rejected", f"Expected 401/403, got {r.status_code}")

    # Verify wrong token on admin endpoint
    r = requests.get(f"{ADMIN_BASE}/api/v1/ota/packages",
                     headers={"Authorization": "Bearer garbage_token"})
    if r.status_code in (401, 403):
        _pass("Invalid token rejected on admin endpoint", f"HTTP {r.status_code}")
    else:
        _fail("Invalid token rejected on admin endpoint", f"Expected 401/403, got {r.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}Digilux OTA API — Endpoint Smoke Tests{RESET}")
    print("=" * 55)

    # ── Tokens ────────────────────────────────────────────────────────────────
    _section("Token acquisition")

    admin_token = user_token = None

    try:
        admin_token = get_admin_token()
        _pass("Get Admin Token (Cognito USER_PASSWORD_AUTH)")
    except Exception as e:
        _fail("Get Admin Token", str(e))

    try:
        user_token = get_user_token()
        _pass("Get User Token (PKCE)")
    except Exception as e:
        _fail("Get User Token (PKCE)", str(e))

    if not admin_token:
        print(f"\n{RED}Cannot continue — admin token fetch failed.{RESET}")
        sys.exit(1)

    admin_hdrs = {"Authorization": admin_token}
    user_hdrs  = {"Authorization": f"Bearer {user_token}"} if user_token else {}

    # ── Run groups ─────────────────────────────────────────────────────────────
    test_auth_endpoints(admin_hdrs)
    test_packages(admin_hdrs)
    test_device_admin(admin_hdrs)
    test_deployments(admin_hdrs)
    test_beta_users(admin_hdrs)

    if user_token:
        test_user_endpoints(user_hdrs)
    else:
        _section("End User Updates (PKCE token)")
        _skip("All user endpoints", "user token unavailable")

    # ── Summary ────────────────────────────────────────────────────────────────
    passed = sum(1 for s, _ in results if s == "PASS")
    failed = sum(1 for s, _ in results if s == "FAIL")
    skipped = sum(1 for s, _ in results if s == "SKIP")

    print(f"\n{'=' * 55}")
    print(f"{BOLD}Results:{RESET}  "
          f"{GREEN}{passed} passed{RESET}  "
          f"{RED}{failed} failed{RESET}  "
          f"{YELLOW}{skipped} skipped{RESET}")

    if failed:
        print(f"\n{RED}Failed tests:{RESET}")
        for s, name in results:
            if s == "FAIL":
                print(f"  • {name}")
        sys.exit(1)
    else:
        print(f"\n{GREEN}All tests passed.{RESET}")


if __name__ == "__main__":
    main()
