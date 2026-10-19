#!/usr/bin/env python3
"""
extract_ota_roles.py
--------------------
Extracts all OTA-related IAM roles and IoT Core policies from the current
AWS account and writes them to both JSON and Markdown files.

Use this to compare what is LIVE in AWS against what deploy scripts
(05_iam_roles.sh, 03_iot_setup.sh) are expected to create — and find gaps.

Usage:
    python3.9 extract_ota_roles.py
    python3.9 extract_ota_roles.py --region us-east-1
    python3.9 extract_ota_roles.py --region us-east-1 --output-dir /tmp
    python3.9 extract_ota_roles.py --filter ota --output-dir ./audit

Output:
    ota_roles_<timestamp>.json   — full raw data (machine-readable)
    ota_roles_<timestamp>.md     — human-readable report for gap analysis
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_policy(doc):
    """Policy documents from IAM are URL-encoded strings. Decode to dict."""
    if isinstance(doc, str):
        return json.loads(urllib.parse.unquote(doc))
    return doc


def _fmt_actions(actions):
    if isinstance(actions, str):
        actions = [actions]
    return ", ".join(f"`{a}`" for a in sorted(actions))


def _fmt_resources(resources):
    if isinstance(resources, str):
        resources = [resources]
    return "\n".join(f"  - `{r}`" for r in resources)


# ── IAM extraction ────────────────────────────────────────────────────────────

def _is_ota_match(name, filter_str):
    """
    Match only when filter_str appears as a whole word segment in the name.
    Segments are delimited by hyphens, underscores, or string boundaries.
    e.g. 'ota' matches 'ota-lambda-role', 'digilux-ota-lambda-role',
         but NOT 'key-rotation-role' or 'ServiceQuotas'.
    """
    pattern = r'(^|[-_])' + re.escape(filter_str.lower()) + r'([-_]|$)'
    return bool(re.search(pattern, name.lower()))


def list_ota_roles(iam, filter_str):
    roles = []
    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page["Roles"]:
            if _is_ota_match(role["RoleName"], filter_str):
                roles.append(role)
    return roles


def extract_role(iam, role_name):
    role_resp = iam.get_role(RoleName=role_name)["Role"]

    # ── Inline policies
    inline = {}
    pag = iam.get_paginator("list_role_policies")
    for page in pag.paginate(RoleName=role_name):
        for policy_name in page["PolicyNames"]:
            resp = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
            inline[policy_name] = _decode_policy(resp["PolicyDocument"])

    # ── Attached managed policies
    attached = {}
    pag = iam.get_paginator("list_attached_role_policies")
    for page in pag.paginate(RoleName=role_name):
        for p in page["AttachedPolicies"]:
            try:
                meta    = iam.get_policy(PolicyArn=p["PolicyArn"])["Policy"]
                version = iam.get_policy_version(
                    PolicyArn=p["PolicyArn"],
                    VersionId=meta["DefaultVersionId"]
                )["PolicyVersion"]
                attached[p["PolicyName"]] = {
                    "arn":      p["PolicyArn"],
                    "document": _decode_policy(version["Document"]),
                }
            except ClientError as e:
                attached[p["PolicyName"]] = {"arn": p["PolicyArn"], "error": str(e)}

    return {
        "roleName":       role_resp["RoleName"],
        "arn":            role_resp["Arn"],
        "description":    role_resp.get("Description", ""),
        "createdAt":      role_resp["CreateDate"].isoformat(),
        "trustPolicy":    _decode_policy(role_resp["AssumeRolePolicyDocument"]),
        "inlinePolicies": inline,
        "attachedPolicies": attached,
    }


# ── IoT policy extraction ─────────────────────────────────────────────────────

def list_ota_iot_policies(iot, filter_str):
    policies = []
    paginator = iot.get_paginator("list_policies")
    for page in paginator.paginate():
        for p in page["policies"]:
            if _is_ota_match(p["policyName"], filter_str):
                try:
                    full = iot.get_policy(policyName=p["policyName"])
                    policies.append({
                        "name":             full["policyName"],
                        "arn":              full["policyArn"],
                        "defaultVersionId": full.get("defaultVersionId", "1"),
                        "document":         json.loads(full["policyDocument"]),
                    })
                except ClientError as e:
                    policies.append({
                        "name":  p["policyName"],
                        "arn":   p["policyArn"],
                        "error": str(e),
                    })
    return policies


# ── JSON builder ──────────────────────────────────────────────────────────────

def build_json(account_id, region, roles, iot_policies):
    return {
        "metadata": {
            "account":     account_id,
            "region":      region,
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "iamRoleCount":    len(roles),
            "iotPolicyCount":  len(iot_policies),
        },
        "iamRoles":    {r["roleName"]: r for r in roles},
        "iotPolicies": iot_policies,
    }


# ── Markdown builder ──────────────────────────────────────────────────────────

def _statements_to_md(statements):
    lines = []
    for stmt in statements:
        sid      = stmt.get("Sid", "")
        effect   = stmt.get("Effect", "Allow")
        actions  = stmt.get("Action", [])
        resources = stmt.get("Resource", [])

        heading = f"**{sid}**" if sid else "*(no Sid)*"
        lines.append(f"{heading} — Effect: `{effect}`")
        lines.append(f"- Actions: {_fmt_actions(actions)}")
        lines.append(f"- Resources:\n{_fmt_resources(resources)}")
        lines.append("")
    return lines


def _trust_principals(trust_policy):
    principals = []
    for stmt in trust_policy.get("Statement", []):
        principal = stmt.get("Principal", {})
        if isinstance(principal, str):
            principals.append(principal)
        elif isinstance(principal, dict):
            for v in principal.values():
                if isinstance(v, list):
                    principals.extend(v)
                else:
                    principals.append(v)
    return principals


def build_markdown(account_id, region, roles, iot_policies):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    lines += [
        "# OTA Roles & Policies — Live Audit",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Account | `{account_id}` |",
        f"| Region | `{region}` |",
        f"| Extracted | {now} |",
        f"| IAM Roles found | {len(roles)} |",
        f"| IoT Policies found | {len(iot_policies)} |",
        "",
        "> Use this file to compare against `05_iam_roles.sh` and `03_iot_setup.sh` "
        "to identify missing or extra permissions.",
        "",
        "---",
        "",
    ]

    # ── IAM Roles ─────────────────────────────────────────────────────────────
    lines.append("## IAM Roles")
    lines.append("")

    if not roles:
        lines.append("_No OTA IAM roles found._")
        lines.append("")
    else:
        for role in sorted(roles, key=lambda r: r["roleName"]):
            name = role["roleName"]
            lines += [
                f"### {name}",
                "",
                f"**ARN:** `{role['arn']}`  ",
            ]
            if role.get("description"):
                lines.append(f"**Description:** {role['description']}  ")
            lines += [
                f"**Created:** {role['createdAt']}",
                "",
            ]

            # Trust policy
            lines.append("#### Trust Policy")
            lines.append("")
            principals = _trust_principals(role["trustPolicy"])
            if principals:
                for p in principals:
                    lines.append(f"- `{p}`")
            else:
                lines.append("_(empty)_")
            lines.append("")

            # Inline policies
            lines.append("#### Inline Policies")
            lines.append("")
            if role["inlinePolicies"]:
                for pname, doc in sorted(role["inlinePolicies"].items()):
                    lines.append(f"##### `{pname}`")
                    lines.append("")
                    stmts = doc.get("Statement", [])
                    lines += _statements_to_md(stmts)
            else:
                lines.append("_(none)_")
                lines.append("")

            # Attached managed policies
            lines.append("#### Attached Managed Policies")
            lines.append("")
            if role["attachedPolicies"]:
                for pname, info in sorted(role["attachedPolicies"].items()):
                    lines.append(f"- **{pname}** — `{info.get('arn', '')}`")
                    if "document" in info:
                        for stmt in info["document"].get("Statement", []):
                            sid = stmt.get("Sid", "")
                            actions = stmt.get("Action", [])
                            lines.append(f"  - {sid or '(no Sid)'}: {_fmt_actions(actions)}")
            else:
                lines.append("_(none)_")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── IoT Core Policies ─────────────────────────────────────────────────────
    lines.append("## IoT Core Policies")
    lines.append("")

    if not iot_policies:
        lines.append("_No OTA IoT policies found._")
        lines.append("")
    else:
        for policy in sorted(iot_policies, key=lambda p: p["name"]):
            name = policy["name"]
            lines += [
                f"### {name}",
                "",
                f"**ARN:** `{policy['arn']}`  ",
                f"**Default Version:** {policy.get('defaultVersionId', 'N/A')}",
                "",
            ]
            if "error" in policy:
                lines.append(f"> ERROR fetching policy: {policy['error']}")
                lines.append("")
            elif "document" in policy:
                lines.append("#### Statements")
                lines.append("")
                lines += _statements_to_md(policy["document"].get("Statement", []))
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract OTA IAM roles and IoT policies for gap analysis."
    )
    parser.add_argument("--region",     default=None, help="AWS region (default: from AWS config)")
    parser.add_argument("--filter",     default="ota", help="Name filter (default: ota)")
    parser.add_argument("--output-dir", default=".",   help="Output directory (default: current dir)")
    args = parser.parse_args()

    session    = boto3.session.Session()
    region     = args.region or session.region_name or "ap-south-1"
    sts        = boto3.client("sts",  region_name=region)
    iam        = boto3.client("iam",  region_name=region)
    iot        = boto3.client("iot",  region_name=region)

    try:
        account_id = sts.get_caller_identity()["Account"]
    except ClientError as e:
        print(f"ERROR: Cannot authenticate with AWS — {e}")
        sys.exit(1)

    print(f"Account : {account_id}")
    print(f"Region  : {region}")
    print(f"Filter  : '{args.filter}'")
    print("")

    # ── IAM roles
    print("Scanning IAM roles...")
    raw_roles = list_ota_roles(iam, args.filter)
    print(f"  Found: {len(raw_roles)}")
    roles = []
    for r in raw_roles:
        name = r["RoleName"]
        print(f"  Extracting: {name}")
        roles.append(extract_role(iam, name))

    # ── IoT policies
    print("")
    print("Scanning IoT Core policies...")
    iot_policies = list_ota_iot_policies(iot, args.filter)
    print(f"  Found: {len(iot_policies)}")
    for p in iot_policies:
        print(f"  Extracted: {p['name']}")

    # ── Write output
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = os.path.join(args.output_dir, f"ota_roles_{timestamp}.json")
    md_path   = os.path.join(args.output_dir, f"ota_roles_{timestamp}.md")

    data = build_json(account_id, region, roles, iot_policies)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    md = build_markdown(account_id, region, roles, iot_policies)
    with open(md_path, "w") as f:
        f.write(md)

    print("")
    print("════════════════════════════════════════")
    print(f"  JSON → {json_path}")
    print(f"  MD   → {md_path}")
    print(f"  IAM roles    : {len(roles)}")
    print(f"  IoT policies : {len(iot_policies)}")
    print("════════════════════════════════════════")


if __name__ == "__main__":
    main()
