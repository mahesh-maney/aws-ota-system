#!/usr/bin/env python3
"""
extract_ota_lambdas.py
----------------------
Extracts all OTA-related Lambda functions from the current AWS account and
writes them to both JSON and Markdown files.

For each Lambda it captures:
  - Function name, ARN, runtime, handler, memory, timeout, last modified
  - ALL environment variables and their values
  - Aliases (e.g. prod) and which version they point to
  - Layers attached

Use this to audit deployed Lambdas against what deploy scripts
(06_lambdas/, 07_deploy_lambdas.sh) are expected to create — and find
missing functions, wrong env vars, or stale values.

Usage:
    python3.9 extract_ota_lambdas.py
    python3.9 extract_ota_lambdas.py --region us-east-1
    python3.9 extract_ota_lambdas.py --region us-east-1 --output-dir /tmp
    python3.9 extract_ota_lambdas.py --filter ota --output-dir ./audit
    python3.9 extract_ota_lambdas.py --show-secrets --output-dir ./audit

Output:
    ota_lambdas_<timestamp>.json   — full raw data including secret values
    ota_lambdas_<timestamp>.md     — human-readable report (secrets redacted by default)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


# ── Filter helper ─────────────────────────────────────────────────────────────

def _is_ota_match(name, filter_str):
    """
    Match only when filter_str appears as a whole word segment in the name.
    Delimiters: hyphen, underscore, or string boundary.
    e.g. 'ota' matches 'ota_status_handler', 'digilux_ota_status'
         but NOT 'quota_function' or 'annotation_helper'.
    """
    pattern = r'(^|[-_])' + re.escape(filter_str.lower()) + r'([-_]|$)'
    return bool(re.search(pattern, name.lower()))


# ── Lambda extraction ─────────────────────────────────────────────────────────

def list_ota_functions(lam, filter_str):
    """List all Lambda functions whose name matches the OTA filter."""
    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            if _is_ota_match(fn["FunctionName"], filter_str):
                functions.append(fn)
    return functions


def extract_function(lam, fn):
    """Enrich a function summary with aliases and layers."""
    name = fn["FunctionName"]

    # ── Aliases
    aliases = []
    try:
        pag = lam.get_paginator("list_aliases")
        for page in pag.paginate(FunctionName=name):
            for alias in page.get("Aliases", []):
                aliases.append({
                    "name":            alias["Name"],
                    "functionVersion": alias["FunctionVersion"],
                    "description":     alias.get("Description", ""),
                })
    except ClientError as e:
        aliases = [{"error": str(e)}]

    # ── Environment variables
    env_vars = fn.get("Environment", {}).get("Variables", {})

    # ── Layers
    layers = [
        {"arn": layer.get("Arn", ""), "codeSize": layer.get("CodeSize", 0)}
        for layer in fn.get("Layers", [])
    ]

    return {
        "functionName":  name,
        "functionArn":   fn["FunctionArn"],
        "runtime":       fn.get("Runtime", "N/A"),
        "handler":       fn.get("Handler", "N/A"),
        "memoryMB":      fn.get("MemorySize", 0),
        "timeoutSec":    fn.get("Timeout", 0),
        "codeSize":      fn.get("CodeSize", 0),
        "lastModified":  fn.get("LastModified", ""),
        "description":   fn.get("Description", ""),
        "role":          fn.get("Role", ""),
        "envVars":       env_vars,
        "aliases":       aliases,
        "layers":        layers,
    }


# ── JSON builder ──────────────────────────────────────────────────────────────

def build_json(account_id, region, functions):
    return {
        "metadata": {
            "account":       account_id,
            "region":        region,
            "extractedAt":   datetime.now(timezone.utc).isoformat(),
            "functionCount": len(functions),
        },
        "functions": {f["functionName"]: f for f in functions},
    }


# ── Markdown builder ──────────────────────────────────────────────────────────

# Expected OTA Lambdas (from 07_deploy_lambdas.sh / 06_lambdas/)
EXPECTED_LAMBDAS = [
    ("ota_artifact_processor",  "Processes uploads; signs + encrypts artifact; PENDING → ACTIVE/CORRUPTED"),
    ("ota_upload_url",          "Generates presigned S3 upload URLs for admin uploads"),
    ("ota_package_register",    "Registers packages; maps deviceType → packageName"),
    ("ota_device_register",     "Registers devices; assigns to IoT thing group leaf"),
    ("ota_job_create",          "Creates AWAITING_CONSENT deployment + per-device consent records"),
    ("ota_user_consent",        "YES/NO consent; YES creates per-device IoT Job"),
    ("ota_user_check_updates",  "Returns available OTA updates for a user's devices"),
    ("ota_status_handler",      "Syncs controller MQTT status to DynamoDB + IoT Core"),
    ("ota_job_sync",            "Staleness check + IoT job lifecycle event handler"),
    ("ota_artifact_key",        "MQTT + HTTP key delivery: returns encrypted data key for artifact"),
    ("ota_list_packages",       "Admin: list packages"),
    ("ota_list_deployments",    "Admin: list deployments"),
]

# Env var name fragments that indicate sensitive values — redacted in Markdown
_SENSITIVE_FRAGMENTS = {"SECRET", "KEY", "PASSWORD", "TOKEN", "PRIVATE", "PWD", "SIGNING"}


def _is_sensitive(key):
    return any(s in key.upper() for s in _SENSITIVE_FRAGMENTS)


def build_markdown(account_id, region, functions, show_secrets=False):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    lines += [
        "# OTA Lambda Functions — Live Audit",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Account | `{account_id}` |",
        f"| Region | `{region}` |",
        f"| Extracted | {now} |",
        f"| Lambda Functions found | {len(functions)} |",
        "",
        "> Use this file to compare against `07_deploy_lambdas.sh` / `06_lambdas/` to identify "
        "missing functions, wrong env vars, or stale configuration.  ",
        "> Sensitive env var values are redacted in this file. Use the JSON output for full values.",
        "",
        "---",
        "",
    ]

    # ── Expected functions reference ──────────────────────────────────────────
    lines += [
        "## Expected Lambda Functions",
        "",
        "| Function Name | Purpose |",
        "|---|---|",
    ]
    for fn_name, purpose in EXPECTED_LAMBDAS:
        lines.append(f"| `{fn_name}` | {purpose} |")
    lines += ["", "---", ""]

    # ── Live functions ─────────────────────────────────────────────────────────
    lines.append("## Lambda Functions (Live)")
    lines.append("")

    if not functions:
        lines.append("_No OTA Lambda functions found._")
        lines.append("")
    else:
        for fn in sorted(functions, key=lambda f: f["functionName"]):
            # Alias summary (e.g. prod → v7)
            alias_summary = ""
            if fn["aliases"]:
                parts = [
                    f"`{a['name']}` → v{a['functionVersion']}"
                    for a in fn["aliases"] if "name" in a
                ]
                if parts:
                    alias_summary = "  (" + ", ".join(parts) + ")"

            lines += [f"### {fn['functionName']}{alias_summary}", ""]

            if fn.get("description"):
                lines.append(f"**Description:** {fn['description']}  ")

            lines += [
                f"**ARN:** `{fn['functionArn']}`  ",
                f"**Runtime:** `{fn['runtime']}`  "
                f"**Handler:** `{fn['handler']}`  "
                f"**Memory:** {fn['memoryMB']} MB  "
                f"**Timeout:** {fn['timeoutSec']}s",
                f"**Last Modified:** {fn['lastModified']}",
                f"**Role:** `{fn['role']}`",
                "",
            ]

            # ── Environment variables
            lines.append("#### Environment Variables")
            lines.append("")
            if fn["envVars"]:
                lines.append("| Variable | Value |")
                lines.append("|---|---|")
                for k, v in sorted(fn["envVars"].items()):
                    if not show_secrets and _is_sensitive(k):
                        display_v = "**`[REDACTED — use JSON for value]`**"
                    else:
                        display_v = f"`{v}`"
                    lines.append(f"| `{k}` | {display_v} |")
            else:
                lines.append("_(no environment variables set)_")
            lines.append("")

            # ── Layers
            if fn["layers"]:
                lines.append("#### Layers")
                lines.append("")
                for layer in fn["layers"]:
                    lines.append(f"- `{layer['arn']}`")
                lines.append("")

            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract OTA Lambda functions and environment variables for gap analysis."
    )
    parser.add_argument("--region",       default=None,  help="AWS region (default: from AWS config)")
    parser.add_argument("--filter",       default="ota", help="Name filter (default: ota)")
    parser.add_argument("--output-dir",   default=".",   help="Output directory (default: current dir)")
    parser.add_argument("--show-secrets", action="store_true",
                        help="Show sensitive env var values in Markdown (default: redacted)")
    args = parser.parse_args()

    session    = boto3.session.Session()
    region     = args.region or session.region_name or "ap-south-1"
    sts        = boto3.client("sts",    region_name=region)
    lam        = boto3.client("lambda", region_name=region)

    try:
        account_id = sts.get_caller_identity()["Account"]
    except ClientError as e:
        print(f"ERROR: Cannot authenticate with AWS — {e}")
        sys.exit(1)

    print(f"Account : {account_id}")
    print(f"Region  : {region}")
    print(f"Filter  : '{args.filter}'")
    print("")

    print("Scanning Lambda functions...")
    raw_functions = list_ota_functions(lam, args.filter)
    print(f"  Found: {len(raw_functions)}")

    functions = []
    for fn in raw_functions:
        name = fn["FunctionName"]
        print(f"  Extracting: {name}")
        functions.append(extract_function(lam, fn))

    # ── Write output
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = os.path.join(args.output_dir, f"ota_lambdas_{timestamp}.json")
    md_path   = os.path.join(args.output_dir, f"ota_lambdas_{timestamp}.md")

    data = build_json(account_id, region, functions)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    md = build_markdown(account_id, region, functions, show_secrets=args.show_secrets)
    with open(md_path, "w") as f:
        f.write(md)

    print("")
    print("════════════════════════════════════════")
    print(f"  JSON → {json_path}")
    print(f"  MD   → {md_path}")
    print(f"  Lambda Functions: {len(functions)}")
    print("════════════════════════════════════════")


if __name__ == "__main__":
    main()
