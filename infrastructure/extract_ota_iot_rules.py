#!/usr/bin/env python3
"""
extract_ota_iot_rules.py
------------------------
Extracts all OTA-related IoT Rules from the current AWS account and writes
them to both JSON and Markdown files.

Use this to compare what is LIVE in AWS against what 08_iot_rules.sh is
expected to create — and find gaps (missing rules, wrong topics, wrong Lambda
ARNs, disabled rules, missing error actions).

Usage:
    python3.9 extract_ota_iot_rules.py
    python3.9 extract_ota_iot_rules.py --region us-east-1
    python3.9 extract_ota_iot_rules.py --region us-east-1 --output-dir /tmp
    python3.9 extract_ota_iot_rules.py --filter ota --output-dir ./audit

Output:
    ota_iot_rules_<timestamp>.json   — full raw data (machine-readable)
    ota_iot_rules_<timestamp>.md     — human-readable report for gap analysis
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
    e.g. 'ota' matches 'ota_status_ingest', 'digilux_ota_status'
         but NOT 'quota_rule' or 'rotation_rule'.
    """
    pattern = r'(^|[-_])' + re.escape(filter_str.lower()) + r'([-_]|$)'
    return bool(re.search(pattern, name.lower()))


# ── IoT Rule extraction ───────────────────────────────────────────────────────

def list_ota_rules(iot, filter_str):
    """List all IoT topic rules whose name matches the OTA filter."""
    rules = []
    paginator = iot.get_paginator("list_topic_rules")
    for page in paginator.paginate():
        for rule in page.get("rules", []):
            if _is_ota_match(rule["ruleName"], filter_str):
                rules.append(rule)
    return rules


def extract_rule(iot, rule_name):
    """Get full details for a single IoT rule."""
    resp      = iot.get_topic_rule(ruleName=rule_name)
    rule      = resp["rule"]
    rule_meta = resp.get("ruleArn", "")

    actions = []
    for action in rule.get("actions", []):
        actions.append(_parse_action(action))

    error_action = None
    if rule.get("errorAction"):
        error_action = _parse_action(rule["errorAction"])

    return {
        "ruleName":          rule["ruleName"],
        "ruleArn":           rule_meta,
        "description":       rule.get("description", ""),
        "enabled":           not rule.get("ruleDisabled", False),
        "createdAt":         rule.get("createdAt", "").isoformat() if hasattr(rule.get("createdAt", ""), "isoformat") else str(rule.get("createdAt", "")),
        "awsIotSqlVersion":  rule.get("awsIotSqlVersion", ""),
        "sql":               rule.get("sql", ""),
        "actions":           actions,
        "errorAction":       error_action,
    }


def _parse_action(action_dict):
    """
    Flatten an IoT rule action dict into a readable structure.
    Supports the action types used in OTA: lambda, cloudwatchLogs, sqs, sns, firehose.
    Unknown types are stored as-is.
    """
    if "lambda" in action_dict:
        return {
            "type":        "Lambda",
            "functionArn": action_dict["lambda"].get("functionArn", ""),
        }
    if "cloudwatchLogs" in action_dict:
        return {
            "type":         "CloudWatchLogs",
            "logGroupName": action_dict["cloudwatchLogs"].get("logGroupName", ""),
            "roleArn":      action_dict["cloudwatchLogs"].get("roleArn", ""),
        }
    if "sqs" in action_dict:
        return {
            "type":     "SQS",
            "queueUrl": action_dict["sqs"].get("queueUrl", ""),
            "roleArn":  action_dict["sqs"].get("roleArn", ""),
        }
    if "sns" in action_dict:
        return {
            "type":       "SNS",
            "targetArn":  action_dict["sns"].get("targetArn", ""),
            "roleArn":    action_dict["sns"].get("roleArn", ""),
        }
    if "republish" in action_dict:
        return {
            "type":    "Republish",
            "topic":   action_dict["republish"].get("topic", ""),
            "roleArn": action_dict["republish"].get("roleArn", ""),
        }
    # Unknown action type — store raw
    return {"type": "Unknown", "raw": action_dict}


# ── EventBridge rules extraction ──────────────────────────────────────────────

def list_ota_eventbridge_rules(events, filter_str):
    """List EventBridge scheduled rules related to OTA (e.g. ota-job-sync-daily)."""
    rules = []
    try:
        paginator = events.get_paginator("list_rules")
        for page in paginator.paginate():
            for rule in page.get("Rules", []):
                if _is_ota_match(rule["Name"], filter_str):
                    # Get targets for this rule
                    targets_resp = events.list_targets_by_rule(Rule=rule["Name"])
                    targets = targets_resp.get("Targets", [])
                    rules.append({
                        "name":               rule["Name"],
                        "arn":                rule.get("Arn", ""),
                        "state":              rule.get("State", ""),
                        "description":        rule.get("Description", ""),
                        "scheduleExpression": rule.get("ScheduleExpression", ""),
                        "eventPattern":       rule.get("EventPattern", ""),
                        "targets":            [
                            {"id": t.get("Id"), "arn": t.get("Arn")}
                            for t in targets
                        ],
                    })
    except ClientError as e:
        print(f"  WARNING: Could not list EventBridge rules — {e}")
    return rules


# ── JSON builder ──────────────────────────────────────────────────────────────

def build_json(account_id, region, rules, eb_rules):
    return {
        "metadata": {
            "account":            account_id,
            "region":             region,
            "extractedAt":        datetime.now(timezone.utc).isoformat(),
            "iotRuleCount":       len(rules),
            "eventBridgeRuleCount": len(eb_rules),
        },
        "iotRules":        {r["ruleName"]: r for r in rules},
        "eventBridgeRules": {r["name"]: r for r in eb_rules},
    }


# ── Markdown builder ──────────────────────────────────────────────────────────

def _action_to_md(action, indent=""):
    lines = []
    atype = action.get("type", "Unknown")
    if atype == "Lambda":
        lines.append(f"{indent}- **Type:** Lambda")
        lines.append(f"{indent}- **Function ARN:** `{action.get('functionArn', '')}`")
    elif atype == "CloudWatchLogs":
        lines.append(f"{indent}- **Type:** CloudWatch Logs")
        lines.append(f"{indent}- **Log Group:** `{action.get('logGroupName', '')}`")
        lines.append(f"{indent}- **Role ARN:** `{action.get('roleArn', '')}`")
    elif atype == "SQS":
        lines.append(f"{indent}- **Type:** SQS")
        lines.append(f"{indent}- **Queue URL:** `{action.get('queueUrl', '')}`")
    elif atype == "SNS":
        lines.append(f"{indent}- **Type:** SNS")
        lines.append(f"{indent}- **Target ARN:** `{action.get('targetArn', '')}`")
    elif atype == "Republish":
        lines.append(f"{indent}- **Type:** Republish")
        lines.append(f"{indent}- **Topic:** `{action.get('topic', '')}`")
    else:
        lines.append(f"{indent}- **Type:** {atype}")
        lines.append(f"{indent}- **Raw:** `{json.dumps(action.get('raw', {}))}`")
    return lines


def build_markdown(account_id, region, rules, eb_rules):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    lines += [
        "# OTA IoT Rules — Live Audit",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Account | `{account_id}` |",
        f"| Region | `{region}` |",
        f"| Extracted | {now} |",
        f"| IoT Rules found | {len(rules)} |",
        f"| EventBridge Rules found | {len(eb_rules)} |",
        "",
        "> Use this file to compare against `08_iot_rules.sh` to identify "
        "missing rules, wrong topics, wrong Lambda ARNs, or disabled rules.",
        "",
        "---",
        "",
    ]

    # ── Expected rules reference ───────────────────────────────────────────
    lines += [
        "## Expected Rules (from 08_iot_rules.sh)",
        "",
        "| Rule Name | Topic | Action |",
        "|---|---|---|",
        "| `ota_status_ingest` | `iot/device/+/ota/status` | Lambda: `ota_status_handler` |",
        "| `ota_device_register_rule` | `iot/device/+/ota/register` | Lambda: `ota_device_register` |",
        "| `ota_job_lifecycle_sync` | `$aws/events/job/+/+` | Lambda: `ota_job_sync` |",
        "| `ota_artifact_key_request` | `iot/device/+/ota/handshake/request` | Lambda: `ota_artifact_key` |",
        "| `ota-job-sync-daily` _(EventBridge)_ | `rate(1 day)` | Lambda: `ota_job_sync` |",
        "",
        "---",
        "",
    ]

    # ── IoT Rules ──────────────────────────────────────────────────────────
    lines.append("## IoT Topic Rules (Live)")
    lines.append("")

    if not rules:
        lines.append("_No OTA IoT rules found._")
        lines.append("")
    else:
        for rule in sorted(rules, key=lambda r: r["ruleName"]):
            status_icon = "✅" if rule["enabled"] else "❌ DISABLED"
            lines += [
                f"### {rule['ruleName']}  {status_icon}",
                "",
            ]
            if rule.get("description"):
                lines.append(f"**Description:** {rule['description']}  ")
            if rule.get("createdAt"):
                lines.append(f"**Created:** {rule['createdAt']}  ")
            lines.append(f"**SQL Version:** {rule.get('awsIotSqlVersion', 'N/A')}")
            lines.append("")

            lines.append("#### SQL (Topic + Data Extraction)")
            lines.append("```sql")
            lines.append(rule.get("sql", ""))
            lines.append("```")
            lines.append("")

            lines.append("#### Actions")
            lines.append("")
            if rule["actions"]:
                for i, action in enumerate(rule["actions"], 1):
                    lines.append(f"**Action {i}:**")
                    lines += _action_to_md(action)
                    lines.append("")
            else:
                lines.append("_(no actions defined)_")
                lines.append("")

            lines.append("#### Error Action")
            lines.append("")
            if rule["errorAction"]:
                lines += _action_to_md(rule["errorAction"])
            else:
                lines.append("_(no error action defined — failures are silently dropped)_")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── EventBridge Rules ──────────────────────────────────────────────────
    lines.append("## EventBridge Rules (Live)")
    lines.append("")

    if not eb_rules:
        lines.append("_No OTA EventBridge rules found._")
        lines.append("")
    else:
        for rule in sorted(eb_rules, key=lambda r: r["name"]):
            status_icon = "✅" if rule.get("state") == "ENABLED" else f"❌ {rule.get('state', 'UNKNOWN')}"
            lines += [
                f"### {rule['name']}  {status_icon}",
                "",
            ]
            if rule.get("description"):
                lines.append(f"**Description:** {rule['description']}  ")
            if rule.get("scheduleExpression"):
                lines.append(f"**Schedule:** `{rule['scheduleExpression']}`  ")
            if rule.get("eventPattern"):
                lines.append(f"**Event Pattern:** `{rule['eventPattern']}`  ")
            lines.append("")

            lines.append("#### Targets")
            lines.append("")
            if rule["targets"]:
                for t in rule["targets"]:
                    lines.append(f"- **ID:** `{t['id']}`  **ARN:** `{t['arn']}`")
            else:
                lines.append("_(no targets)_")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract OTA IoT Rules and EventBridge rules for gap analysis."
    )
    parser.add_argument("--region",     default=None,  help="AWS region (default: from AWS config)")
    parser.add_argument("--filter",     default="ota", help="Name filter (default: ota)")
    parser.add_argument("--output-dir", default=".",   help="Output directory (default: current dir)")
    args = parser.parse_args()

    session    = boto3.session.Session()
    region     = args.region or session.region_name or "ap-south-1"
    sts        = boto3.client("sts",    region_name=region)
    iot        = boto3.client("iot",    region_name=region)
    events     = boto3.client("events", region_name=region)

    try:
        account_id = sts.get_caller_identity()["Account"]
    except ClientError as e:
        print(f"ERROR: Cannot authenticate with AWS — {e}")
        sys.exit(1)

    print(f"Account : {account_id}")
    print(f"Region  : {region}")
    print(f"Filter  : '{args.filter}'")
    print("")

    # ── IoT Rules
    print("Scanning IoT Rules...")
    raw_rules = list_ota_rules(iot, args.filter)
    print(f"  Found: {len(raw_rules)}")
    rules = []
    for r in raw_rules:
        name = r["ruleName"]
        print(f"  Extracting: {name}")
        rules.append(extract_rule(iot, name))

    # ── EventBridge Rules
    print("")
    print("Scanning EventBridge Rules...")
    eb_rules = list_ota_eventbridge_rules(events, args.filter)
    print(f"  Found: {len(eb_rules)}")
    for r in eb_rules:
        print(f"  Extracted: {r['name']}")

    # ── Write output
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = os.path.join(args.output_dir, f"ota_iot_rules_{timestamp}.json")
    md_path   = os.path.join(args.output_dir, f"ota_iot_rules_{timestamp}.md")

    data = build_json(account_id, region, rules, eb_rules)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    md = build_markdown(account_id, region, rules, eb_rules)
    with open(md_path, "w") as f:
        f.write(md)

    print("")
    print("════════════════════════════════════════")
    print(f"  JSON → {json_path}")
    print(f"  MD   → {md_path}")
    print(f"  IoT Rules       : {len(rules)}")
    print(f"  EventBridge Rules: {len(eb_rules)}")
    print("════════════════════════════════════════")


if __name__ == "__main__":
    main()
