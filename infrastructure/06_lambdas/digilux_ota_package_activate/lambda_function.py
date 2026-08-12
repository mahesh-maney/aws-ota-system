"""
digilux_ota_package_activate
Toggles the activated flag on a firmware package.

PATCH /api/v1/ota/packages/{packageName}/{version}/activate
Admin-only.

A package must be status=ACTIVE (i.e. binary uploaded and processed by
artifact_processor) before it can be activated. activated=True makes the
package visible to end users via GET /api/v1/ota/device/available-updates.

BETA packages are only visible to devices in the canary IoT group.
PROD packages are visible to all devices.

Request body:
{
  "activated": true   # true to publish to users, false to withdraw
}

Response:
{
  "packageName": "ZigbeeFirmware",
  "version":     "4.2.2",
  "activated":   true,
  "releaseType": "BETA",
  "updatedBy":   "admin@example.com"
}
"""
import datetime
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION         = os.environ["REGION"]
PACKAGES_TABLE = os.environ.get("PACKAGES_TABLE", "digilux_ota_packages")

dynamo = boto3.resource("dynamodb", region_name=REGION)


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
    log.info(json.dumps({"msg": "request_received", "path": event.get("path")}))

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

        # ── Body ──────────────────────────────────────────────────────────────
        body = json.loads(event.get("body") or "{}")
        if "activated" not in body:
            return _resp(400, {"error": "Missing required field: activated (true or false)"})

        activated = bool(body["activated"])

        # ── Verify package exists and is ACTIVE (binary processed) ────────────
        table = dynamo.Table(PACKAGES_TABLE)
        item  = table.get_item(
            Key={"packageName": package_name, "version": version}
        ).get("Item")

        if not item:
            return _resp(404, {"error": f"Package {package_name} v{version} not found"})

        if item.get("status") != "ACTIVE":
            return _resp(409, {
                "error": f"Package {package_name} v{version} is not yet ACTIVE (status={item.get('status')}). "
                         "Upload the binary first."
            })

        # ── Toggle activated flag ─────────────────────────────────────────────
        now_ms = int(__import__("time").time() * 1000)
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


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body),
    }
