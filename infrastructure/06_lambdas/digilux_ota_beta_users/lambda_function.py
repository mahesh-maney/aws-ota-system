"""
digilux_ota_beta_users
Manages the list of beta users for OTA BETA-stage deployments.

Admin-only (ota-admin Cognito group required).

GET    /api/v1/ota/beta-users          → list all beta users
POST   /api/v1/ota/beta-users          → add user by email (resolves email → userId → deviceId)
DELETE /api/v1/ota/beta-users/{email}  → remove beta user
"""
import datetime
import json
import logging
import os
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION            = os.environ["REGION"]
USER_POOL_ID      = os.environ["COGNITO_POOL_ID"]       # ap-south-1_h1o8s7257
DEVICE_DATA_TABLE = os.environ["DEVICE_DATA_TABLE"]     # digilux_device_data
BETA_USERS_TABLE  = os.environ.get("BETA_USERS_TABLE", "digilux_ota_beta_users")

dynamo  = boto3.resource("dynamodb", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


def _require_admin(event: dict):
    """Returns (caller_email, None) or (None, error_response)."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    if "ota-admin" not in claims.get("cognito:groups", ""):
        return None, _response(403, {"error": "Admin access required"})
    return claims.get("email", claims.get("sub", "unknown")), None


def lambda_handler(event, context):
    method      = event.get("httpMethod", "GET").upper()
    path_params = event.get("pathParameters") or {}
    email_param = path_params.get("email")
    if email_param:
        email_param = urllib.parse.unquote(email_param)

    log.info(json.dumps({"method": method, "email_param": email_param}))

    caller, err = _require_admin(event)
    if err:
        return err

    try:
        if method == "GET":
            return _list_beta_users()
        if method == "POST":
            body = json.loads(event.get("body") or "{}")
            return _add_beta_user(body, caller)
        if method == "DELETE" and email_param:
            return _remove_beta_user(email_param, caller)
        return _response(405, {"error": "Method not allowed"})
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        log.error(f"AWS error: {msg}")
        return _response(500, {"error": f"AWS error: {msg}"})
    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        return _response(500, {"error": "Internal server error"})


def _list_beta_users() -> dict:
    result = dynamo.Table(BETA_USERS_TABLE).scan()
    users  = result.get("Items", [])
    users.sort(key=lambda u: u.get("addedAt", ""))
    log.info(f"Listed {len(users)} beta users")
    return _response(200, {"users": users, "count": len(users)})


def _add_beta_user(body: dict, caller: str) -> dict:
    email = (body.get("email") or "").strip().lower()
    if not email:
        return _response(400, {"error": "Missing required field: email"})

    # Check if already in the list
    existing = dynamo.Table(BETA_USERS_TABLE).get_item(Key={"email": email}).get("Item")
    if existing:
        return _response(409, {"error": f"{email} is already a beta user"})

    # Step 1: Cognito — email → userId (sub)
    log.info(f"Looking up Cognito user: {email}")
    try:
        cog_user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
    except cognito.exceptions.UserNotFoundException:
        return _response(404, {"error": f"No Digilux account found for {email}"})

    user_attrs = {a["Name"]: a["Value"] for a in cog_user.get("UserAttributes", [])}
    user_id    = user_attrs.get("sub")
    if not user_id:
        return _response(404, {"error": f"Could not resolve userId for {email}"})
    log.info(f"Cognito resolved: {email} → userId={user_id}")

    # Step 2: digilux_device_data (userId-index) → deviceId + thingName
    log.info(f"Querying device_data for userId={user_id}")
    resp = dynamo.Table(DEVICE_DATA_TABLE).query(
        IndexName="userId-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("userId").eq(user_id),
    )
    items = resp.get("Items", [])
    if not items:
        return _response(404, {"error": f"No device registered for {email}. User must complete onboarding first."})

    device     = items[0]
    device_id  = device.get("deviceId")
    thing_name = device.get("thingName")

    if not device_id:
        return _response(404, {"error": f"Device record for {email} is missing deviceId"})
    if not thing_name:
        # thingName == deviceId directly — derive it if not yet set by OTA agent
        thing_name = device_id
        log.info(f"thingName not set for {email}, using deviceId as thingName: {thing_name}")

    log.info(f"Device resolved: userId={user_id} → deviceId={device_id}, thingName={thing_name}")

    # Step 3: Store in beta users table
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {
        "email":      email,
        "userId":     user_id,
        "deviceId":   device_id,
        "thingName":  thing_name,
        "addedAt":    now,
        "addedBy":    caller,
    }
    dynamo.Table(BETA_USERS_TABLE).put_item(Item=item)
    log.info(f"Beta user added: {email} (deviceId={device_id}) by {caller}")

    return _response(201, item)


def _remove_beta_user(email: str, caller: str) -> dict:
    email = email.strip().lower()
    existing = dynamo.Table(BETA_USERS_TABLE).get_item(Key={"email": email}).get("Item")
    if not existing:
        return _response(404, {"error": f"{email} is not in the beta users list"})

    dynamo.Table(BETA_USERS_TABLE).delete_item(Key={"email": email})
    log.info(f"Beta user removed: {email} by {caller}")
    return _response(200, {"message": f"{email} removed from beta users"})
