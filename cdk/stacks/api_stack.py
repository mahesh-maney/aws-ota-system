import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw, aws_cognito as cognito
from constructs import Construct

from .lambda_stack import LambdaStack


class ApiStack(cdk.Stack):
    """
    Creates a REST API Gateway with all OTA endpoints wired to Lambda functions.
    All endpoints are protected by a Cognito User Pool authorizer.

    Admin endpoints:
      GET  /api/v1/ota/packages                              List packages
      POST /api/v1/ota/packages/upload-url                   Get pre-signed S3 upload URL
      POST /api/v1/ota/deployments                           Create OTA job
      GET  /api/v1/ota/deployments                           List jobs
      GET  /api/v1/ota/deployments/{jobId}                   Job status + device progress
      POST /api/v1/ota/deployments/{jobId}/abort             Abort job
      GET  /api/v1/controllers/{deviceId}/updates/available  Check device compatibility

    User endpoints:
      GET  /api/v1/ota/device/available-updates              Check pending updates for device
      POST /api/v1/ota/my/updates/consent                    Consent to update → creates IoT job
      POST /api/v1/ota/my/updates/download-link              Get CloudFront signed download URL
      GET  /api/v1/ota/my/updates/{jobId}/status             Track update progress
    """

    def __init__(
        self, scope: Construct, construct_id: str,
        config: dict, lambdas: LambdaStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = config["namePrefix"]
        stage = config.get("apiStageName", "prod")

        # ── REST API ──────────────────────────────────────────────────────────
        api = apigw.RestApi(
            self, "OtaApi",
            rest_api_name=f"{prefix}-ota-api",
            description=f"{prefix.capitalize()} OTA REST API",
            deploy_options=apigw.StageOptions(
                stage_name=stage,
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                logging_level=apigw.MethodLoggingLevel.ERROR,
                metrics_enabled=True,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # ── Cognito Authorizer ────────────────────────────────────────────────
        user_pool = cognito.UserPool.from_user_pool_id(
            self, "UserPool", config["cognitoUserPoolId"]
        )
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "CognitoAuthorizer",
            cognito_user_pools=[user_pool],
            authorizer_name=f"{prefix}-ota-authorizer",
            identity_source="method.request.header.Authorization",
        )

        def add_method(resource: apigw.Resource, method: str, fn) -> None:
            resource.add_method(
                method,
                apigw.LambdaIntegration(fn, proxy=True),
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=authorizer,
            )

        # ── Resource tree ─────────────────────────────────────────────────────
        # /api/v1
        api_res = api.root.add_resource("api")
        v1 = api_res.add_resource("v1")

        # /api/v1/ota
        ota = v1.add_resource("ota")

        # ── Admin: /api/v1/ota/packages ───────────────────────────────────────
        packages = ota.add_resource("packages")
        add_method(packages, "GET", lambdas.upload_url)

        upload_url = packages.add_resource("upload-url")
        add_method(upload_url, "POST", lambdas.upload_url)

        # ── Admin: /api/v1/ota/deployments ────────────────────────────────────
        deployments = ota.add_resource("deployments")
        add_method(deployments, "POST", lambdas.job_create)
        add_method(deployments, "GET", lambdas.job_create)

        deployment_job = deployments.add_resource("{jobId}")
        add_method(deployment_job, "GET", lambdas.job_create)

        abort = deployment_job.add_resource("abort")
        add_method(abort, "POST", lambdas.job_create)

        # ── Admin: /api/v1/controllers/{deviceId}/updates/available ───────────
        controllers = v1.add_resource("controllers")
        controller_device = controllers.add_resource("{deviceId}")
        controller_updates = controller_device.add_resource("updates")
        controller_avail = controller_updates.add_resource("available")
        add_method(controller_avail, "GET", lambdas.compat_check)

        # ── User: /api/v1/ota/device/available-updates ────────────────────────
        device = ota.add_resource("device")
        available_updates = device.add_resource("available-updates")
        add_method(available_updates, "GET", lambdas.check_updates)

        # ── User: /api/v1/ota/my/* ────────────────────────────────────────────
        my = ota.add_resource("my")
        my_updates = my.add_resource("updates")

        # POST /api/v1/ota/my/updates/consent
        consent_res = my_updates.add_resource("consent")
        add_method(consent_res, "POST", lambdas.consent)

        # POST /api/v1/ota/my/updates/download-link
        download_link = my_updates.add_resource("download-link")
        add_method(download_link, "POST", lambdas.download_link)

        # GET /api/v1/ota/my/updates/{jobId}/status
        # NOTE: {jobId} path param coexists with literal siblings (consent, download-link)
        # API Gateway resolves literal paths before path parameters — no conflict.
        my_job = my_updates.add_resource("{jobId}")
        my_status = my_job.add_resource("status")
        add_method(my_status, "GET", lambdas.update_status)

        # ── Outputs ───────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "ApiUrl",
                      value=api.url,
                      description="OTA REST API base URL")
        cdk.CfnOutput(self, "ApiId", value=api.rest_api_id)
        cdk.CfnOutput(self, "ApiStage", value=stage)
