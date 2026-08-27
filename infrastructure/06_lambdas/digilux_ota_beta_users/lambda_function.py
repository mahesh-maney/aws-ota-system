"""
digilux_ota_beta_users
Manages the list of beta users for OTA BETA-stage deployments.

Admin-only (ota-admin Cognito group required).

Table PK: userId (Cognito sub) — email is never stored; resolved from Cognito on GET.

GET    /api/v1/ota/beta-users          → list all beta users (email fetched live from Cognito)
POST   /api/v1/ota/beta-users          → add user by email (resolves email → userId → deviceId)
DELETE /api/v1/ota/beta-users/{userId} → remove beta user by userId
"""
import datetime
import json
import logging
import os
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


def _get_cognito_email(user_id: str) -> str:
    """Look up email from Cognito by userId (sub). Returns userId as fallback."""
    try:
        resp = cognito.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'sub = "{user_id}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if users:
            attrs = {a["Name"]: a["Value"] for a in users[0].get("Attributes", [])}
            return attrs.get("email", user_id)
    except Exception as e:
        log.warning(f"Cognito email lookup failed for userId={user_id}: {e}")
    return user_id  # fallback: show userId if Cognito is unreachable


def lambda_handler(event, context):
    method      = event.get("httpMethod", "GET").upper()
    path_params = event.get("pathParameters") or {}
    # API GW path param is named 'email' but now carries userId
    user_id_param = path_params.get("email")
    if user_id_param:
        user_id_param = urllib.parse.unquote(user_id_param)

    log.info(json.dumps({"method": method, "user_id_param": user_id_param}))

    caller, err = _require_admin(event)
    if err:
        return err

    try:
        if method == "GET":
            return _list_beta_users()
        if method == "POST":
            body = json.loads(event.get("body") or "{}")
            return _add_beta_user(body, caller)
        if method == "DELETE" and user_id_param:
            return _remove_beta_user(user_id_param, caller)
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
    items  = result.get("Items", [])
    # Enrich each record with email looked up live from Cognito
    for item in items:
        item["email"] = _get_cognito_email(item["userId"])
    items.sort(key=lambda u: u.get("addedAt", ""))
    log.info(f"Listed {len(items)} beta users")
    return _response(200, {"users": items, "count": len(items)})


def _add_beta_user(body: dict, caller: str) -> dict:
    email = (body.get("email") or "").strip().lower()
    if not email:
        return _response(400, {"error": "Missing required field: email"})

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

    # Check if already in the list (keyed by userId — email is not stored)
    existing = dynamo.Table(BETA_USERS_TABLE).get_item(Key={"userId": user_id}).get("Item")
    if existing:
        return _response(409, {"error": f"{email} is already a beta user"})

    # Step 2: digilux_device_data (userId-index) → deviceId + thingName
    log.info(f"Querying device_data for userId={user_id}")
    resp = dynamo.Table(DEVICE_DATA_TABLE).query(
        IndexName="userId-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("userId").eq(user_id),
    )
    device_items = resp.get("Items", [])
    if not device_items:
        return _response(404, {"error": f"No device registered for {email}. User must complete onboarding first."})

    device     = device_items[0]
    device_id  = device.get("deviceId")
    thing_name = device.get("thingName") or device_id

    if not device_id:
        return _response(404, {"error": f"Device record for {email} is missing deviceId"})

    log.info(f"Device resolved: userId={user_id} → deviceId={device_id}, thingName={thing_name}")

    # Step 3: Store in beta users table — userId as PK, email NOT stored
    now  = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {
        "userId":    user_id,
        "deviceId":  device_id,
        "thingName": thing_name,
        "addedAt":   now,
        "addedBy":   caller,
    }
    dynamo.Table(BETA_USERS_TABLE).put_item(Item=item)
    log.info(f"Beta user added: {email} (userId={user_id}, deviceId={device_id}) by {caller}")

    # Return with email for immediate UI display (sourced from Cognito, not stored)
    return _response(201, {**item, "email": email})


def _remove_beta_user(user_id: str, caller: str) -> dict:
    user_id  = user_id.strip()
    existing = dynamo.Table(BETA_USERS_TABLE).get_item(Key={"userId": user_id}).get("Item")
    if not existing:
        return _response(404, {"error": "User not found in beta users list"})

    dynamo.Table(BETA_USERS_TABLE).delete_item(Key={"userId": user_id})
    log.info(f"Beta user removed: userId={user_id} by {caller}")
    return _response(200, {"message": "Beta user removed"})
