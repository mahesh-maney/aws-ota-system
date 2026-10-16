#!/usr/bin/env python3
"""
OTA API Gateway — Idempotent Setup Script
==========================================
Creates / verifies all OTA API Gateway resources, methods, integrations,
CORS preflight (OPTIONS), and Lambda invoke permissions.

Safe to re-run at any time:
  - Existing correct resources/methods are skipped.
  - Existing OPTIONS with WRONG Allow-Methods headers are detected and fixed.
  - Legacy {jobId} path parameter is detected and reused (no duplicate created).

Run on AWS CloudShell (recommended) or any machine with boto3 + AWS credentials.
See infrastructure/API_Gateway.md for the full endpoint reference.
"""

import sys
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
print("   OTA API Gateway — Idempotent Setup Script")
print("=" * 62)
print()
print("  Press Enter to accept [default] where shown.")
print()

API_ID     = prompt("API Gateway REST API ID")
REGION     = prompt("AWS Region","us-east-1")
STAGE      = prompt("Stage name","api")
ACCOUNT_ID = prompt("AWS Account ID")
ADMIN_AUTH = prompt("Admin Cognito Authorizer ID","zisf8r")
USER_AUTH  = prompt("User Cognito Authorizer ID","noe20u")

if not all([API_ID, REGION, STAGE, ACCOUNT_ID, ADMIN_AUTH, USER_AUTH]):
    print("\n  ERROR: All inputs are required.")
    sys.exit(1)

print()
print("  Summary:")
print(f"    API Gateway ID : {API_ID}")
print(f"    Region         : {REGION}")
print(f"    Stage          : {STAGE}")
print(f"    Account ID     : {ACCOUNT_ID}")
print(f"    Admin Auth     : {ADMIN_AUTH}")
print(f"    User Auth      : {USER_AUTH}")
print()
if input("  Proceed? [y/N]: ").strip().lower() != "y":
    print("  Aborted.")
    sys.exit(0)

print()

# ─────────────────────────────────────────────────────────────────────────────
# AWS clients
# ─────────────────────────────────────────────────────────────────────────────

apigw = boto3.client("apigateway", region_name=REGION)
lmbda = boto3.client("lambda",     region_name=REGION)

# ─────────────────────────────────────────────────────────────────────────────
# Resource cache
# ─────────────────────────────────────────────────────────────────────────────

_resource_cache = None


def _load_resources(force=False):
    global _resource_cache
    if _resource_cache is None or force:
        resp = apigw.get_resources(restApiId=API_ID, limit=500)
        _resource_cache = resp.get("items", [])
    return _resource_cache


def get_or_create_resource(parent_id, path_part):
    """Find existing child resource by pathPart, or create it. Returns resource ID."""
    for r in _load_resources():
        if r.get("parentId") == parent_id and r.get("pathPart") == path_part:
            print(f"    [SKIP]   /{path_part} already exists")
            return r["id"]
    print(f"    [CREATE] /{path_part}")
    resp = apigw.create_resource(restApiId=API_ID, parentId=parent_id, pathPart=path_part)
    _load_resources(force=True)
    return resp["id"]


def get_or_create_deployment_id_resource(deploy_res_id):
    """
    Special handler for /{deploymentId}.
    Reuses existing {jobId} resource (Digilux legacy) if found —
    does NOT create a duplicate. Creates {deploymentId} fresh only when
    neither name exists.
    """
    for r in _load_resources():
        if r.get("parentId") == deploy_res_id and r.get("pathPart") == "{deploymentId}":
            print(f"    [SKIP]   /{{deploymentId}} already exists")
            return r["id"]
    for r in _load_resources():
        if r.get("parentId") == deploy_res_id and r.get("pathPart") == "{jobId}":
            print(f"    [NOTE]   Found legacy {{jobId}} resource — reusing it (Lambda handles both)")
            return r["id"]
    print(f"    [CREATE] /{{deploymentId}}")
    resp = apigw.create_resource(restApiId=API_ID, parentId=deploy_res_id, pathPart="{deploymentId}")
    _load_resources(force=True)
    return resp["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Method helpers
# ─────────────────────────────────────────────────────────────────────────────

def _lambda_uri(func_name):
    return (
        f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31"
        f"/functions/arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{func_name}/invocations"
    )


def _method_exists(resource_id, http_method):
    try:
        apigw.get_method(restApiId=API_ID, resourceId=resource_id, httpMethod=http_method)
        return True
    except (apigw.exceptions.NotFoundException, ClientError):
        return False


def _add_lambda_permission(func_name, resource_id, http_method):
    stmt_id = f"{func_name}-apigw-{resource_id}-{http_method}"
    try:
        lmbda.remove_permission(FunctionName=func_name, StatementId=stmt_id)
    except ClientError:
        pass
    try:
        lmbda.add_permission(
            FunctionName=func_name,
            StatementId=stmt_id,
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{API_ID}/*/{http_method}/*"
        )
        print(f"      [OK]     Lambda invoke permission granted")
    except ClientError as e:
        print(f"      [WARN]   Lambda permission: {e}")


def add_method_cognito(resource_id, http_method, func_name, authorizer_id):
    """Add method with COGNITO_USER_POOLS auth. Skips if already exists."""
    if _method_exists(resource_id, http_method):
        print(f"    [SKIP]   {http_method} already exists → {func_name}")
        return
    print(f"    [CREATE] {http_method} → {func_name}  (Cognito)")
    apigw.put_method(
        restApiId=API_ID,
        resourceId=resource_id,
        httpMethod=http_method,
        authorizationType="COGNITO_USER_POOLS",
        authorizerId=authorizer_id
    )
    apigw.put_integration(
        restApiId=API_ID,
        resourceId=resource_id,
        httpMethod=http_method,
        type="AWS_PROXY",
        integrationHttpMethod="POST",
        uri=_lambda_uri(func_name)
    )
    _add_lambda_permission(func_name, resource_id, http_method)


def add_method_iam(resource_id, http_method, func_name):
    """Add method with AWS_IAM auth (device SigV4 endpoints). Skips if already exists."""
    if _method_exists(resource_id, http_method):
        print(f"    [SKIP]   {http_method} (IAM) already exists → {func_name}")
        return
    print(f"    [CREATE] {http_method} → {func_name}  (AWS_IAM)")
    apigw.put_method(
        restApiId=API_ID,
        resourceId=resource_id,
        httpMethod=http_method,
        authorizationType="AWS_IAM"
    )
    apigw.put_integration(
        restApiId=API_ID,
        resourceId=resource_id,
        httpMethod=http_method,
        type="AWS_PROXY",
        integrationHttpMethod="POST",
        uri=_lambda_uri(func_name)
    )
    _add_lambda_permission(func_name, resource_id, http_method)


# ─────────────────────────────────────────────────────────────────────────────
# CORS helper — checks existing OPTIONS for correct methods; fixes if wrong
# ─────────────────────────────────────────────────────────────────────────────

def add_cors(resource_id, allowed_methods):
    """
    Idempotent CORS OPTIONS setup.
    - If OPTIONS does not exist          → creates with correct headers.
    - If OPTIONS exists and is correct   → skips.
    - If OPTIONS exists but incomplete   → deletes and recreates.
    """
    method_set  = set(m.strip() for m in allowed_methods.split(",")) | {"OPTIONS"}
    methods_str = ",".join(sorted(method_set))

    if _method_exists(resource_id, "OPTIONS"):
        try:
            resp = apigw.get_integration_response(
                restApiId=API_ID,
                resourceId=resource_id,
                httpMethod="OPTIONS",
                statusCode="200"
            )
            existing_param = resp.get("responseParameters", {}).get(
                "method.response.header.Access-Control-Allow-Methods", ""
            )
            existing_methods = set(
                m.strip() for m in existing_param.strip("'").split(",") if m.strip()
            )
            if method_set.issubset(existing_methods):
                print(f"    [SKIP]   OPTIONS CORS correct  ({','.join(sorted(existing_methods))})")
                return
            else:
                print(f"    [FIX]    OPTIONS CORS incomplete — has: {','.join(sorted(existing_methods))}  need: {methods_str}")
                apigw.delete_method(restApiId=API_ID, resourceId=resource_id, httpMethod="OPTIONS")
        except ClientError:
            print(f"    [FIX]    OPTIONS exists but CORS unverifiable — recreating")
            try:
                apigw.delete_method(restApiId=API_ID, resourceId=resource_id, httpMethod="OPTIONS")
            except ClientError:
                pass

    print(f"    [CREATE] OPTIONS CORS  ({methods_str})")
    apigw.put_method(
        restApiId=API_ID, resourceId=resource_id,
        httpMethod="OPTIONS", authorizationType="NONE"
    )
    apigw.put_integration(
        restApiId=API_ID, resourceId=resource_id,
        httpMethod="OPTIONS", type="MOCK",
        requestTemplates={"application/json": '{"statusCode": 200}'}
    )
    try:
        apigw.put_method_response(
            restApiId=API_ID, resourceId=resource_id,
            httpMethod="OPTIONS", statusCode="200",
            responseParameters={
                "method.response.header.Access-Control-Allow-Headers": False,
                "method.response.header.Access-Control-Allow-Methods": False,
                "method.response.header.Access-Control-Allow-Origin":  False,
            }
        )
    except ClientError:
        pass
    try:
        apigw.put_integration_response(
            restApiId=API_ID, resourceId=resource_id,
            httpMethod="OPTIONS", statusCode="200",
            responseParameters={
                "method.response.header.Access-Control-Allow-Headers": "'Content-Type,Authorization'",
                "method.response.header.Access-Control-Allow-Methods": f"'{methods_str}'",
                "method.response.header.Access-Control-Allow-Origin":  "'*'",
            }
        )
    except ClientError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Gateway responses — CORS on 4xx / 5xx errors
# ─────────────────────────────────────────────────────────────────────────────

def setup_gateway_responses():
    print("\n[Step 9/10] Gateway Responses (CORS on error responses)")
    for resp_type in ["DEFAULT_4XX", "DEFAULT_5XX", "UNAUTHORIZED", "ACCESS_DENIED", "EXPIRED_TOKEN"]:
        try:
            apigw.put_gateway_response(
                restApiId=API_ID,
                responseType=resp_type,
                responseParameters={
                    "gatewayresponse.header.Access-Control-Allow-Origin":  "'*'",
                    "gatewayresponse.header.Access-Control-Allow-Headers": "'Content-Type,Authorization'",
                }
            )
            print(f"    [OK]     {resp_type}")
        except ClientError as e:
            print(f"    [WARN]   {resp_type}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Find /api/v1 root
# ─────────────────────────────────────────────────────────────────────────────

def find_api_root():
    for r in _load_resources():
        if r.get("path") == "/api/v1":
            return r["id"]
    print("  ERROR: /api/v1 resource not found in this API Gateway.")
    print("  The base /api/v1 path must already exist before OTA routes can be added.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():

    # Step 1 — Resolve root
    print("[Step 1/10] Resolving /api/v1 root resource")
    api_root = find_api_root()
    print(f"    [OK]     /api/v1 → {api_root}")

    # ── Step 2 — /api/v1/ota ─────────────────────────────────────────────────
    print("\n[Step 2/10] /api/v1/ota")
    ota = get_or_create_resource(api_root, "ota")

    # ── Step 3 — Packages ────────────────────────────────────────────────────
    print("\n[Step 3/10] Package endpoints")

    print("\n  /api/v1/ota/packages")
    pkgs = get_or_create_resource(ota, "packages")
    add_method_cognito(pkgs, "GET", "ota_upload_url", ADMIN_AUTH)
    add_cors(pkgs, "GET,OPTIONS")

    print("\n  /api/v1/ota/packages/upload-artefact")
    upload = get_or_create_resource(pkgs, "upload-artefact")
    add_method_cognito(upload, "POST", "ota_upload_url", ADMIN_AUTH)
    add_cors(upload, "POST,OPTIONS")

    print("\n  /api/v1/ota/packages/upload-artefact/complete")
    upload_complete = get_or_create_resource(upload, "complete")
    add_method_cognito(upload_complete, "POST", "ota_upload_complete", ADMIN_AUTH)
    add_cors(upload_complete, "POST,OPTIONS")

    print("\n  /api/v1/ota/packages/{packageName}/{version}")
    pkg_name = get_or_create_resource(pkgs, "{packageName}")
    pkg_ver  = get_or_create_resource(pkg_name, "{version}")
    add_method_cognito(pkg_ver, "GET",    "ota_package_status", ADMIN_AUTH)
    add_method_cognito(pkg_ver, "DELETE", "ota_package_status", ADMIN_AUTH)
    add_cors(pkg_ver, "GET,DELETE,OPTIONS")

    print("\n  /api/v1/ota/packages/{packageName}/{version}/activate")
    pkg_act = get_or_create_resource(pkg_ver, "activate")
    add_method_cognito(pkg_act, "PATCH", "ota_package_activate", ADMIN_AUTH)
    add_cors(pkg_act, "PATCH,OPTIONS")

    # ── Step 4 — Beta users ───────────────────────────────────────────────────
    print("\n[Step 4/10] Beta user endpoints")

    print("\n  /api/v1/ota/beta-users")
    beta = get_or_create_resource(ota, "beta-users")
    add_method_cognito(beta, "GET",  "ota_beta_users", ADMIN_AUTH)
    add_method_cognito(beta, "POST", "ota_beta_users", ADMIN_AUTH)
    add_cors(beta, "GET,POST,OPTIONS")

    print("\n  /api/v1/ota/beta-users/{userId}")
    beta_user = get_or_create_resource(beta, "{userId}")
    add_method_cognito(beta_user, "DELETE", "ota_beta_users", ADMIN_AUTH)
    add_cors(beta_user, "DELETE,OPTIONS")

    # ── Step 5 — Deployments ─────────────────────────────────────────────────
    print("\n[Step 5/10] Deployment endpoints")

    print("\n  /api/v1/ota/deployments")
    deploy = get_or_create_resource(ota, "deployments")
    add_method_cognito(deploy, "GET",  "ota_job_create", ADMIN_AUTH)
    add_method_cognito(deploy, "POST", "ota_job_create", ADMIN_AUTH)
    add_cors(deploy, "GET,POST,OPTIONS")

    print("\n  /api/v1/ota/deployments/{deploymentId}  (reuses {jobId} if legacy)")
    deploy_id = get_or_create_deployment_id_resource(deploy)
    add_method_cognito(deploy_id, "GET", "ota_job_create", ADMIN_AUTH)
    add_cors(deploy_id, "GET,OPTIONS")

    print("\n  /api/v1/ota/deployments/{deploymentId}/abort")
    deploy_abort = get_or_create_resource(deploy_id, "abort")
    add_method_cognito(deploy_abort, "POST", "ota_job_create", ADMIN_AUTH)
    add_cors(deploy_abort, "POST,OPTIONS")

    # ── Step 6 — Device endpoints ─────────────────────────────────────────────
    print("\n[Step 6/10] Device endpoints")

    ota_device = get_or_create_resource(ota, "device")

    print("\n  /api/v1/ota/device/available-updates")
    avail = get_or_create_resource(ota_device, "available-updates")
    add_method_cognito(avail, "GET", "ota_user_check_updates", USER_AUTH)
    add_cors(avail, "GET,OPTIONS")

    print("\n  /api/v1/ota/device/artifact-key  (AWS_IAM — device SigV4)")
    art_key = get_or_create_resource(ota_device, "artifact-key")
    add_method_iam(art_key, "POST", "ota_artifact_key")
    add_cors(art_key, "POST,OPTIONS")

    # ── Step 7 — User consent / download / status ─────────────────────────────
    print("\n[Step 7/10] User update endpoints")

    my      = get_or_create_resource(ota, "my")
    updates = get_or_create_resource(my, "updates")

    print("\n  /api/v1/ota/my/updates/consent")
    consent = get_or_create_resource(updates, "consent")
    add_method_cognito(consent, "POST", "ota_user_consent", USER_AUTH)
    add_cors(consent, "POST,OPTIONS")

    print("\n  /api/v1/ota/my/updates/download-link")
    download = get_or_create_resource(updates, "download-link")
    add_method_cognito(download, "POST", "ota_user_get_download_link", USER_AUTH)
    add_cors(download, "POST,OPTIONS")

    print("\n  /api/v1/ota/my/updates/{jobId}/status")
    job_param  = get_or_create_resource(updates, "{jobId}")
    status_res = get_or_create_resource(job_param, "status")
    add_method_cognito(status_res, "GET", "ota_user_update_status", USER_AUTH)
    add_cors(status_res, "GET,OPTIONS")

    # ── Step 8 — Compatibility check ──────────────────────────────────────────
    print("\n[Step 8/10] Compatibility check endpoint")

    print("\n  /api/v1/controllers/{deviceId}/updates/available")
    controllers  = get_or_create_resource(api_root, "controllers")
    ctrl_device  = get_or_create_resource(controllers, "{deviceId}")
    ctrl_updates = get_or_create_resource(ctrl_device, "updates")
    ctrl_avail   = get_or_create_resource(ctrl_updates, "available")
    add_method_cognito(ctrl_avail, "GET", "ota_compatibility_check", ADMIN_AUTH)
    add_cors(ctrl_avail, "GET,OPTIONS")

    # ── Step 9 — Gateway responses ────────────────────────────────────────────
    setup_gateway_responses()

    # ── Step 10 — Deploy stage ────────────────────────────────────────────────
    print(f"\n[Step 10/10] Deploying stage: {STAGE}")
    resp = apigw.create_deployment(
        restApiId=API_ID,
        stageName=STAGE,
        description="OTA routes — deployed by setup_api_gateway.py"
    )
    print(f"    [OK]     Stage '{STAGE}' deployed  (deployment ID: {resp['id']})")

    base = f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/{STAGE}"
    print(f"\n  Base URL : {base}")
    print()
    print("  All OTA API Gateway routes are in place.")
    print()


if __name__ == "__main__":
    main()
