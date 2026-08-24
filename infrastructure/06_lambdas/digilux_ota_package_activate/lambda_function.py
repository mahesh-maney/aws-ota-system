"""
digilux_ota_package_activate
Manages package state transitions.

PATCH /api/v1/ota/packages/{packageName}/{version}/activate
Admin-only.

Request body variants:

1. Publish / withdraw (activated flag):
   {"activated": true}   — make visible to devices
   {"activated": false}  — hide from devices
   Package must be status=ACTIVE.

2. Recall:
   {"recalled": true, "recallReason": "..."}
   Marks ACTIVE package as RECALLED — removed from all device update checks.

3. Promote BETA → PROD:
   {"promote": true}
   Package must be releaseType=BETA and status=ACTIVE.
   Changes releaseType to PROD. Supersedes lower PROD versions of same package.
   PROD is terminal — cannot be downgraded.

4. Restore SUPERSEDED → ACTIVE (rollback):
   {"restore": true}
   Package must be status=SUPERSEDED and releaseType PROD or BETA.
   Promotes it back to ACTIVE. Any currently ACTIVE version of same
   package+releaseType is superseded (explicit admin override — semver not checked).

DELETE /api/v1/ota/packages/{packageName}/{version}
Admin-only.
Permanently deletes the package record from DynamoDB and the artifact from S3.
ACTIVE packages cannot be deleted — recall or supersede first.
"""
import datetime
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION          = os.environ["REGION"]
PACKAGES_TABLE  = os.environ.get("PACKAGES_TABLE",  "digilux_ota_packages")
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "digilux-ota-artifacts")
OTA_JOBS_TABLE  = os.environ.get("OTA_JOBS_TABLE",  "digilux_ota_jobs")
COOLING_OFF_DAYS = int(os.environ.get("COOLING_OFF_DAYS", "7"))

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3", region_name=REGION)


def _audit(event: str, actor: str, resource: dict, result: str, **extra) -> None:
    print(json.dumps({
        "audit":    True,
        "event":    event,
        "ts":       datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "actor":    actor,
        "resource": resource,
        "result":   result,
        **extra,
    }))


def lambda_handler(event, context):
    log.info(json.dumps({"msg": "request_received", "path": event.get("path"),
                         "method": event.get("httpMethod")}))

    try:
        # ── Auth: admin only ──────────────────────────────────────────────────
        claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
        if "ota-admin" not in claims.get("cognito:groups", ""):
            return _resp(403, {"error": "Admin access required"})

        caller = claims.get("email", claims.get("sub", "unknown"))

        # ── Path parameters ───────────────────────────────────────────────────
        path_params  = event.get("pathParameters") or {}
        package_name = path_params.get("packageName", "").strip()
        version      = path_params.get("version", "").strip()

        if not package_name or not version:
            return _resp(400, {"error": "Missing path parameters: packageName and version required"})

        # ── DELETE ────────────────────────────────────────────────────────────
        if event.get("httpMethod") == "DELETE":
            delete_body = json.loads(event.get("body") or "{}")
            return _delete_package(package_name, version, caller, delete_body)

        # ── Body ──────────────────────────────────────────────────────────────
        body     = json.loads(event.get("body") or "{}")
        recalled = bool(body.get("recalled", False))
        promote  = bool(body.get("promote",  False))
        restore  = bool(body.get("restore",  False))

        if not recalled and not promote and not restore and "activated" not in body:
            return _resp(400, {"error": "Missing required field: 'activated', 'recalled', 'promote', or 'restore'"})

        # ── Verify package exists ─────────────────────────────────────────────
        table = dynamo.Table(PACKAGES_TABLE)
        item  = table.get_item(
            Key={"packageName": package_name, "version": version}
        ).get("Item")

        if not item:
            return _resp(404, {"error": f"Package {package_name} v{version} not found"})

        now_ms = int(__import__("time").time() * 1000)

        # ── RECALL ────────────────────────────────────────────────────────────
        if recalled:
            if item.get("status") not in ("ACTIVE",):
                return _resp(409, {
                    "error": f"Package {package_name} v{version} cannot be recalled "
                             f"(current status={item.get('status')}). Only ACTIVE packages can be recalled."
                })

            recall_reason = str(body.get("recallReason", "")).strip()

            table.update_item(
                Key={"packageName": package_name, "version": version},
                UpdateExpression=(
                    "SET #s = :s, activated = :a, "
                    "recalledBy = :by, recalledAt = :ts, recallReason = :reason"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s":      "RECALLED",
                    ":a":      False,
                    ":by":     caller,
                    ":ts":     now_ms,
                    ":reason": recall_reason,
                },
            )

            log.info(json.dumps({
                "msg": "package_recalled",
                "packageName": package_name, "version": version,
                "actor": caller, "recallReason": recall_reason,
            }))
            _audit("PACKAGE_RECALLED", caller,
                   {"packageName": package_name, "version": version},
                   "SUCCESS", recallReason=recall_reason, releaseType=item.get("releaseType"))

            return _resp(200, {
                "packageName":  package_name,
                "version":      version,
                "status":       "RECALLED",
                "activated":    False,
                "recalledBy":   caller,
                "recallReason": recall_reason,
                "updatedBy":    caller,
                "message":      f"Package {package_name} v{version} recalled. "
                                "No longer visible to end users and flagged in audit logs.",
            })

        # ── PROMOTE (BETA → PROD) ─────────────────────────────────────────────
        if promote:
            if item.get("releaseType") != "BETA":
                return _resp(409, {
                    "error": (
                        f"Package {package_name} v{version} cannot be promoted: "
                        f"only BETA packages can be promoted to PROD "
                        f"(current releaseType={item.get('releaseType')}). "
                        "PROD is terminal and cannot be changed."
                    )
                })
            if item.get("status") != "ACTIVE":
                return _resp(409, {
                    "error": (
                        f"Package {package_name} v{version} cannot be promoted: "
                        f"status must be ACTIVE (current={item.get('status')})"
                    )
                })

            table.update_item(
                Key={"packageName": package_name, "version": version},
                UpdateExpression="SET releaseType = :rt, promotedBy = :by, promotedAt = :ts",
                ExpressionAttributeValues={":rt": "PROD", ":by": caller, ":ts": now_ms},
            )

            # Supersede lower PROD versions
            _supersede_lower_versions(table, package_name, version, "PROD", now_ms)

            log.info(json.dumps({
                "msg": "package_promoted", "packageName": package_name,
                "version": version, "actor": caller,
            }))
            _audit("PACKAGE_PROMOTED", caller,
                   {"packageName": package_name, "version": version},
                   "SUCCESS", fromReleaseType="BETA", toReleaseType="PROD")

            return _resp(200, {
                "packageName": package_name,
                "version":     version,
                "releaseType": "PROD",
                "status":      "ACTIVE",
                "promotedBy":  caller,
                "message":     (
                    f"Package {package_name} v{version} promoted from BETA to PROD. "
                    "Lower PROD versions have been superseded."
                ),
            })

        # ── RESTORE (SUPERSEDED → ACTIVE rollback) ────────────────────────────
        if restore:
            if item.get("status") != "SUPERSEDED":
                return _resp(409, {
                    "error": (
                        f"Package {package_name} v{version} cannot be restored: "
                        f"only SUPERSEDED packages can be restored "
                        f"(current status={item.get('status')})"
                    )
                })
            release_type = item.get("releaseType", "")
            if release_type not in ("PROD", "BETA"):
                return _resp(409, {
                    "error": f"Only PROD or BETA packages can be restored (current releaseType={release_type})"
                })

            # Supersede any currently ACTIVE version of same package+releaseType
            # This is an explicit admin rollback — semver order is not enforced here
            _supersede_active_for_restore(table, package_name, version, release_type, now_ms)

            table.update_item(
                Key={"packageName": package_name, "version": version},
                UpdateExpression=(
                    "SET #s = :active, restoredBy = :by, restoredAt = :ts "
                    "REMOVE supersededAt, supersededBy"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":active": "ACTIVE", ":by": caller, ":ts": now_ms},
            )

            log.info(json.dumps({
                "msg": "package_restored", "packageName": package_name,
                "version": version, "actor": caller, "releaseType": release_type,
            }))
            _audit("PACKAGE_RESTORED", caller,
                   {"packageName": package_name, "version": version},
                   "SUCCESS", releaseType=release_type)

            return _resp(200, {
                "packageName": package_name,
                "version":     version,
                "releaseType": release_type,
                "status":      "ACTIVE",
                "restoredBy":  caller,
                "message":     (
                    f"Package {package_name} v{version} restored to ACTIVE. "
                    "Any previously active version of the same type has been superseded."
                ),
            })

        # ── ACTIVATE / DEACTIVATE ─────────────────────────────────────────────
        if item.get("status") not in ("ACTIVE",):
            return _resp(409, {
                "error": f"Package {package_name} v{version} is not yet ACTIVE (status={item.get('status')}). "
                         "Upload the binary first."
            })

        activated = bool(body["activated"])

        table.update_item(
            Key={"packageName": package_name, "version": version},
            UpdateExpression="SET activated = :a, activatedBy = :by, activatedAt = :ts",
            ExpressionAttributeValues={
                ":a":  activated,
                ":by": caller,
                ":ts": now_ms,
            },
        )

        action = "ACTIVATED" if activated else "DEACTIVATED"
        log.info(json.dumps({
            "msg": "package_activation_toggled",
            "packageName": package_name, "version": version,
            "activated": activated, "actor": caller,
        }))
        _audit(f"PACKAGE_{action}", caller,
               {"packageName": package_name, "version": version},
               "SUCCESS", releaseType=item.get("releaseType"))

        return _resp(200, {
            "packageName": package_name,
            "version":     version,
            "activated":   activated,
            "releaseType": item.get("releaseType"),
            "deviceType":  item.get("deviceType"),
            "updatedBy":   caller,
            "message":     f"Package {action.lower()}. "
                           + ("End users can now see this update." if activated
                              else "Package is no longer visible to end users."),
        })

    except ClientError as e:
        log.error(json.dumps({
            "msg":   "aws_client_error",
            "code":  e.response["Error"]["Code"],
            "error": e.response["Error"]["Message"],
        }))
        return _resp(500, {"error": "Internal server error"})
    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _resp(500, {"error": "Internal server error"})


def _delete_package(package_name: str, version: str, caller: str, body: dict) -> dict:
    """
    DELETE /api/v1/ota/packages/{packageName}/{version}

    Validations (in order):
    1. Package must exist
    2. ACTIVE packages blocked — recall or supersede first
    3. Require non-empty deletion reason
    4. Block if any QUEUED/IN_PROGRESS jobs reference this package version
    5. Cooling-off: SUPERSEDED packages < COOLING_OFF_DAYS old require force=true
    6. RECALLED → soft delete (delete S3 artifact, mark DynamoDB record DELETED for forensics)
       All others  → hard delete (delete S3 + DynamoDB record)
    """
    import time as _time
    from boto3.dynamodb.conditions import Attr

    table = dynamo.Table(PACKAGES_TABLE)
    item  = table.get_item(Key={"packageName": package_name, "version": version}).get("Item")

    if not item:
        return _resp(404, {"error": f"Package {package_name} v{version} not found"})

    status = item.get("status", "")

    # ── 1. Block ACTIVE ───────────────────────────────────────────────────────
    if status == "ACTIVE":
        return _resp(409, {
            "error": (
                f"Package {package_name} v{version} is ACTIVE and cannot be deleted. "
                "Recall or supersede it first."
            )
        })

    # ── 2. Require deletion reason ────────────────────────────────────────────
    reason = str(body.get("reason", "")).strip()
    if not reason:
        return _resp(400, {"error": "A deletion reason is required."})

    # ── 3. Block if active deployments reference this version ─────────────────
    jobs_table = dynamo.Table(OTA_JOBS_TABLE)
    active_jobs = jobs_table.scan(
        FilterExpression=(
            Attr("packageName").eq(package_name) &
            Attr("version").eq(version) &
            Attr("status").is_in(["QUEUED", "IN_PROGRESS"])
        )
    ).get("Items", [])
    if active_jobs:
        job_ids = [j.get("jobId") for j in active_jobs]
        return _resp(409, {
            "error": (
                f"Cannot delete: {len(active_jobs)} active deployment(s) are currently "
                "using this package version. Cancel them first."
            ),
            "activeJobs": job_ids,
        })

    # ── 4. Cooling-off for recently SUPERSEDED packages ───────────────────────
    force = bool(body.get("force", False))
    if status == "SUPERSEDED" and not force:
        superseded_at_ms = item.get("supersededAt")
        if superseded_at_ms:
            age_days = (_time.time() * 1000 - int(superseded_at_ms)) / (1000 * 86400)
            if age_days < COOLING_OFF_DAYS:
                return _resp(409, {
                    "error": (
                        f"Package was superseded {age_days:.1f} day(s) ago. "
                        f"Cooling-off period is {COOLING_OFF_DAYS} days — offline devices may "
                        "still need this version to download. Set force=true to override."
                    ),
                    "coolingOff":        True,
                    "supersededDaysAgo": round(age_days, 1),
                    "coolingOffDays":    COOLING_OFF_DAYS,
                })

    # ── 5. Delete S3 artifact ─────────────────────────────────────────────────
    s3_key    = item.get("s3Key")
    s3_bucket = item.get("s3Bucket", ARTIFACT_BUCKET)
    s3_deleted = False
    if s3_key:
        try:
            s3.delete_object(Bucket=s3_bucket, Key=s3_key)
            s3_deleted = True
            log.info(f"Deleted S3 artifact: s3://{s3_bucket}/{s3_key}")
        except ClientError as e:
            log.warning(f"Could not delete S3 artifact s3://{s3_bucket}/{s3_key}: {e}")

    now_ms = int(_time.time() * 1000)

    # ── 6. RECALLED → soft delete; everything else → hard delete ─────────────
    if status == "RECALLED":
        # Retain the DynamoDB record for forensics — only remove the S3 artifact
        table.update_item(
            Key={"packageName": package_name, "version": version},
            UpdateExpression=(
                "SET #s = :s, deletedBy = :by, deletedAt = :ts, "
                "deleteReason = :r, s3Deleted = :s3d"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":   "DELETED",
                ":by":  caller,
                ":ts":  now_ms,
                ":r":   reason,
                ":s3d": s3_deleted,
            },
        )
        log.info(json.dumps({
            "msg": "package_soft_deleted", "packageName": package_name,
            "version": version, "actor": caller, "s3Deleted": s3_deleted,
        }))
        _audit("PACKAGE_DELETED", caller,
               {"packageName": package_name, "version": version},
               "SUCCESS",
               deleteType="SOFT", previousStatus=status,
               releaseType=item.get("releaseType"),
               reason=reason, s3Deleted=s3_deleted)
        return _resp(200, {
            "packageName":    package_name,
            "version":        version,
            "deletedBy":      caller,
            "s3Deleted":      s3_deleted,
            "recordRetained": True,
            "message": (
                f"Package {package_name} v{version} artifact deleted. "
                "The audit record has been retained for forensic purposes."
            ),
        })

    # Hard delete — PENDING, CORRUPTED, SUPERSEDED
    table.delete_item(Key={"packageName": package_name, "version": version})
    log.info(json.dumps({
        "msg": "package_hard_deleted", "packageName": package_name,
        "version": version, "actor": caller, "s3Deleted": s3_deleted,
        "previousStatus": status,
    }))
    _audit("PACKAGE_DELETED", caller,
           {"packageName": package_name, "version": version},
           "SUCCESS",
           deleteType="HARD", previousStatus=status,
           releaseType=item.get("releaseType"),
           reason=reason, s3Deleted=s3_deleted)
    return _resp(200, {
        "packageName":    package_name,
        "version":        version,
        "deletedBy":      caller,
        "s3Deleted":      s3_deleted,
        "recordRetained": False,
        "message":        f"Package {package_name} v{version} permanently deleted.",
    })


def _semver_tuple(version: str) -> tuple:
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _supersede_lower_versions(table, pkg_name: str, current_version: str,
                               release_type: str, now_ms: int) -> None:
    """Supersede all ACTIVE versions of same package+releaseType with lower semver."""
    from boto3.dynamodb.conditions import Key, Attr
    result = table.query(
        KeyConditionExpression=Key("packageName").eq(pkg_name),
        FilterExpression=Attr("status").eq("ACTIVE") & Attr("releaseType").eq(release_type),
    )
    current_semver = _semver_tuple(current_version)
    for old_item in result.get("Items", []):
        if old_item["version"] == current_version:
            continue
        if _semver_tuple(old_item["version"]) >= current_semver:
            log.info(f"Skipping supersede of {pkg_name}@{old_item['version']} — not older than {current_version}")
            continue
        try:
            table.update_item(
                Key={"packageName": pkg_name, "version": old_item["version"]},
                UpdateExpression="SET #st = :sup, supersededAt = :ts, supersededBy = :v",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":sup": "SUPERSEDED", ":ts": now_ms, ":v": current_version,
                },
                ConditionExpression=Attr("status").eq("ACTIVE"),
            )
            log.info(f"Superseded {pkg_name}@{old_item['version']} → promoted {current_version} is PROD")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                log.error(f"Failed to supersede {pkg_name}@{old_item['version']}: {e}")


def _supersede_active_for_restore(table, pkg_name: str, restoring_version: str,
                                   release_type: str, now_ms: int) -> None:
    """Supersede the currently ACTIVE version when restoring a previous one (admin rollback).
    Semver order is not enforced — this is an explicit admin override.
    """
    from boto3.dynamodb.conditions import Key, Attr
    result = table.query(
        KeyConditionExpression=Key("packageName").eq(pkg_name),
        FilterExpression=Attr("status").eq("ACTIVE") & Attr("releaseType").eq(release_type),
    )
    for old_item in result.get("Items", []):
        if old_item["version"] == restoring_version:
            continue
        try:
            table.update_item(
                Key={"packageName": pkg_name, "version": old_item["version"]},
                UpdateExpression="SET #st = :sup, supersededAt = :ts, supersededBy = :v",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":sup": "SUPERSEDED", ":ts": now_ms, ":v": restoring_version,
                },
                ConditionExpression=Attr("status").eq("ACTIVE"),
            )
            log.info(f"Superseded {pkg_name}@{old_item['version']} (rollback: {restoring_version} restored)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                log.error(f"Failed to supersede {pkg_name}@{old_item['version']}: {e}")


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
