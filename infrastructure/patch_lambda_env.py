#!/usr/bin/env python3
"""
OTA Lambda — Selective Environment Variable Patch Script
=========================================================
Idempotently patches specific env vars across targeted Lambda functions.
Only the 5 variables below are ever touched. All other existing env vars
on each Lambda are preserved exactly as-is.

Safe to re-run at any time:
  [OK]  — key exists with correct value     → no change
  [ADD] — key missing                       → adds key+value
  [FIX] — key exists with wrong value       → updates value
  [SKIP]— Lambda has no changes needed      → update-function-configuration not called

Run on AWS CloudShell or any machine with boto3 + AWS credentials.
See infrastructure/Env_Variables.md for full reference.
"""

import sys
import time
import boto3
from botocore.exceptions import ClientError


# ─────────────────────────────────────────────────────────────────────────────
# Input prompts
# ─────────────────────────────────────────────────────────────────────────────

def prompt(label, default=""):
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val or default


print()
print("=" * 62)
print("   OTA Lambda — Selective Env Var Patch Script")
print("=" * 62)
print()
print("  Press Enter to accept [default] where shown.")
print()

REGION = prompt("AWS Region", "us-east-1")

print()
print("  Target values to patch:")
COGNITO_POOL_ID      = prompt("COGNITO_POOL_ID",      "us-east-1_DuegVptxS")
COGNITO_USER_POOL_ID = prompt("COGNITO_USER_POOL_ID", "us-east-1_DuegVptxS")
SES_SENDER           = prompt("SES_SENDER",           "noreply@nucleusqa.honeywell.com")
PRODUCTION_GROUP     = prompt("PRODUCTION_GROUP",     "HON-PROD")
GATEWAYS_GROUP       = prompt("GATEWAYS_GROUP",       "HON-ZigbeeGateway")

if not all([REGION, COGNITO_POOL_ID, COGNITO_USER_POOL_ID, SES_SENDER, PRODUCTION_GROUP, GATEWAYS_GROUP]):
    print("\n  ERROR: All values are required.")
    sys.exit(1)

print()
print("  Summary of values to apply:")
print(f"    COGNITO_POOL_ID      = {COGNITO_POOL_ID}")
print(f"    COGNITO_USER_POOL_ID = {COGNITO_USER_POOL_ID}")
print(f"    SES_SENDER           = {SES_SENDER}")
print(f"    PRODUCTION_GROUP     = {PRODUCTION_GROUP}")
print(f"    GATEWAYS_GROUP       = {GATEWAYS_GROUP}")
print()
if input("  Proceed? [y/N]: ").strip().lower() != "y":
    print("  Aborted.")
    sys.exit(0)

print()

# ─────────────────────────────────────────────────────────────────────────────
# Patch targets — selective per variable
# ─────────────────────────────────────────────────────────────────────────────

ALL_LAMBDAS = [
    "ota_upload_url",
    "ota_upload_complete",
    "ota_package_status",
    "ota_package_activate",
    "ota_artifact_processor",
    "ota_artifact_key",
    "ota_package_register",
    "ota_compatibility_check",
    "ota_beta_users",
    "ota_job_create",
    "ota_job_sync",
    "ota_key_server",
    "ota_status_handler",
    "ota_device_register",
    "ota_user_check_updates",
    "ota_user_consent",
    "ota_user_update_status",
    "ota_user_get_download_link",
]

# Map: env_var_key → (required_value, list_of_lambda_names_to_patch)
#
# Targets are based strictly on source code analysis (grep os.environ):
#   COGNITO_POOL_ID      confirmed in: ota_beta_users, ota_package_register
#   COGNITO_USER_POOL_ID NOT found in any Lambda source — excluded
#   SES_SENDER           NOT found in any Lambda source — excluded
#   PRODUCTION_GROUP     confirmed in: ota_job_create, ota_package_activate, ota_device_register
#   GATEWAYS_GROUP       confirmed in: ota_device_register
#
# NOTE: COGNITO_USER_POOL_ID and SES_SENDER are set by 07_deploy_lambdas.sh
# on all Lambdas but are not read by any Lambda source file. They are excluded
# here to stay strictly selective. If a Lambda is found to read them in future,
# add it to the respective list below.
PATCH_MAP = {
    "COGNITO_POOL_ID": (
        COGNITO_POOL_ID,
        ["ota_beta_users", "ota_package_register"],
    ),
    "COGNITO_USER_POOL_ID": (
        COGNITO_USER_POOL_ID,
        [],  # no Lambda source confirmed — excluded
    ),
    "SES_SENDER": (
        SES_SENDER,
        [],  # no Lambda source confirmed — excluded
    ),
    "PRODUCTION_GROUP": (
        PRODUCTION_GROUP,
        ["ota_job_create", "ota_package_activate", "ota_device_register"],
    ),
    "GATEWAYS_GROUP": (
        GATEWAYS_GROUP,
        ["ota_device_register"],
    ),
}

# Build per-Lambda patch dict: { func_name: { key: required_value, ... } }
lambda_patches = {fn: {} for fn in ALL_LAMBDAS}

for key, (value, targets) in PATCH_MAP.items():
    for fn in targets:
        lambda_patches[fn][key] = value

# ─────────────────────────────────────────────────────────────────────────────
# AWS client
# ─────────────────────────────────────────────────────────────────────────────

lmbda = boto3.client("lambda", region_name=REGION)

# ─────────────────────────────────────────────────────────────────────────────
# Patch function
# ─────────────────────────────────────────────────────────────────────────────

def patch_lambda(func_name, required_vars):
    """
    Fetch current env vars and apply selective patch:
      - Keys in required_vars    → ADD if missing, FIX if wrong value, OK if correct.
      - Keys in PATCH_MAP but NOT in required_vars (i.e. not used by this Lambda)
        → REMOVE if they currently exist on this Lambda.
      - All other existing env vars → preserved untouched.
    """
    print(f"\n  Lambda: {func_name}")

    # All 5 managed keys — used to detect keys that exist but should be removed
    all_managed_keys = set(PATCH_MAP.keys())

    # Fetch current configuration
    try:
        resp = lmbda.get_function_configuration(FunctionName=func_name)
    except lmbda.exceptions.ResourceNotFoundException:
        print(f"    [WARN]  Lambda not found — skipping")
        return
    except ClientError as e:
        print(f"    [ERROR] Could not fetch config: {e}")
        return

    current_vars = resp.get("Environment", {}).get("Variables", {})

    changes  = {}   # keys to add or update
    removals = []   # keys to remove

    # 1. Add / fix keys this Lambda should have
    for key, required_value in required_vars.items():
        current_value = current_vars.get(key)
        if current_value is None:
            print(f"    [ADD]    {key} = {required_value}")
            changes[key] = required_value
        elif current_value != required_value:
            print(f"    [FIX]    {key}")
            print(f"             was : {current_value}")
            print(f"             now : {required_value}")
            changes[key] = required_value
        else:
            print(f"    [OK]     {key} = {current_value}")

    # 2. Remove managed keys that exist on this Lambda but are NOT in required_vars
    keys_to_remove = all_managed_keys - set(required_vars.keys())
    for key in sorted(keys_to_remove):
        if key in current_vars:
            print(f"    [REMOVE] {key} (not used by this Lambda)")
            removals.append(key)

    if not changes and not removals:
        print(f"    [SKIP]   No changes needed")
        return

    # Build final env: start from current, apply additions/fixes, drop removals
    merged_vars = {**current_vars, **changes}
    for key in removals:
        merged_vars.pop(key, None)

    try:
        lmbda.update_function_configuration(
            FunctionName=func_name,
            Environment={"Variables": merged_vars}
        )
        # Wait for update to complete before moving to next Lambda
        waiter = lmbda.get_waiter("function_updated")
        waiter.wait(FunctionName=func_name, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
        print(f"    [DONE]   {len(changes)} added/fixed, {len(removals)} removed")
    except ClientError as e:
        print(f"    [ERROR]  Update failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    total   = len(ALL_LAMBDAS)
    updated = 0
    skipped = 0
    failed  = 0

    print(f"Patching {total} Lambda functions in region {REGION}...")
    print("=" * 62)

    for i, func_name in enumerate(ALL_LAMBDAS, 1):
        required = lambda_patches[func_name]
        if not required:
            print(f"\n  Lambda: {func_name}")
            print(f"    [SKIP]  No target vars for this Lambda")
            skipped += 1
            continue
        try:
            patch_lambda(func_name, required)
            updated += 1
        except Exception as e:
            print(f"    [ERROR] Unexpected error: {e}")
            failed += 1

    print()
    print("=" * 62)
    print(f"  Done.  {total} Lambdas processed.")
    print(f"         {updated} patched / {skipped} skipped / {failed} failed")
    print()


if __name__ == "__main__":
    main()
