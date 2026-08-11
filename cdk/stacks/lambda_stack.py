import os
import shutil
import subprocess

import aws_cdk as cdk
import jsii
from aws_cdk import (
    aws_iam as iam,
    aws_iot as iot,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
)
from constructs import Construct

from .iam_stack import IamStack
from .storage_stack import StorageStack

# Lambda source root — relative to this file: ../../infrastructure/06_lambdas/
_LAMBDA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "infrastructure", "06_lambdas")
)


@jsii.implements(cdk.ILocalBundling)
class _LocalPipBundler:
    """
    Bundles a Lambda function locally using pip with --platform manylinux2014_x86_64.
    This avoids the Docker dependency while still producing Linux-compatible binaries
    for native packages like cryptography.

    CDK calls try_bundle() first; if it returns False, CDK falls back to Docker.
    """

    def __init__(self, src_dir: str) -> None:
        self._src = src_dir

    def try_bundle(self, output_dir: str, options: cdk.BundlingOptions) -> bool:
        try:
            shutil.copy(os.path.join(self._src, "lambda_function.py"), output_dir)
            req = os.path.join(self._src, "requirements.txt")
            if os.path.exists(req):
                result = subprocess.run(
                    [
                        "pip", "install",
                        "-r", req,
                        "-t", output_dir,
                        "--platform", "manylinux2014_x86_64",
                        "--python-version", "3.11",
                        "--only-binary=:all:",
                        "--upgrade",
                        "-q",
                    ],
                    check=False,
                )
                return result.returncode == 0
            return True
        except Exception:
            return False


def _make_code(name: str) -> lambda_.Code:
    """Return Lambda Code asset for a given function name, with bundling if needed."""
    src = os.path.join(_LAMBDA_DIR, name)
    req = os.path.join(src, "requirements.txt")

    if os.path.exists(req):
        # Bundling needed (e.g. cryptography package)
        return lambda_.Code.from_asset(
            src,
            bundling=cdk.BundlingOptions(
                # Local bundler runs first — no Docker required
                local=_LocalPipBundler(src),
                # Docker fallback (used in CI or if local pip not available)
                image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                command=[
                    "bash", "-c",
                    "pip install -r /asset-input/requirements.txt -t /asset-output "
                    "--platform manylinux2014_x86_64 --python-version 3.11 --only-binary=:all: -q "
                    "&& cp /asset-input/lambda_function.py /asset-output/",
                ],
            ),
        )

    # No dependencies — zip the directory as-is
    return lambda_.Code.from_asset(src)


class LambdaStack(cdk.Stack):
    """
    Deploys all 11 OTA Lambda functions (5 admin + 4 user + 2 pipeline).
    Also creates:
      - IoT topic rules to trigger status_handler and device_register
      - S3 event notification to trigger artifact_processor on upload
      - CloudWatch log groups with 30-day retention
    """

    def __init__(
        self, scope: Construct, construct_id: str,
        config: dict, storage: StorageStack, iam_stack: IamStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = config["namePrefix"]
        region = config["region"]
        account = config["account"]
        tiers = config["presignExpiryTiers"]

        common_env = {
            "REGION": region,
            "ACCOUNT_ID": account,
            "DEVICE_DATA_TABLE": config["deviceDataTableName"],
            "PACKAGES_TABLE": storage.packages_table.table_name,
            "OTA_JOBS_TABLE": storage.jobs_table.table_name,
            "COMPAT_TABLE": storage.compat_table.table_name,
            "CONSENTS_TABLE": storage.consents_table.table_name,
            "ARTIFACT_BUCKET": storage.artifact_bucket.bucket_name,
            "SIGNING_SECRET": config["signingSecretName"],
            "CLOUDFRONT_DOMAIN": config.get("cloudfrontDomain", ""),
            "CLOUDFRONT_KEY_PAIR_ID": config.get("cloudfrontKeyPairId", ""),
            "CLOUDFRONT_PRIVATE_KEY_SECRET": config.get("cloudfrontKeySecretName", f"{prefix}-ota-cloudfront-key"),
            "COGNITO_POOL_ID": config["cognitoUserPoolId"],
            "PRESIGN_EXPIRY_TIER1_MAX_MB": str(tiers["tier1MaxMb"]),
            "PRESIGN_EXPIRY_TIER1_SEC": str(tiers["tier1Sec"]),
            "PRESIGN_EXPIRY_TIER2_MAX_MB": str(tiers["tier2MaxMb"]),
            "PRESIGN_EXPIRY_TIER2_SEC": str(tiers["tier2Sec"]),
            "PRESIGN_EXPIRY_TIER3_MAX_MB": str(tiers["tier3MaxMb"]),
            "PRESIGN_EXPIRY_TIER3_SEC": str(tiers["tier3Sec"]),
            "PRESIGN_EXPIRY_TIER4_SEC": str(tiers["tier4Sec"]),
            "IOT_JOB_TIMEOUT_MINUTES": str(config.get("iotJobTimeoutMinutes", 1440)),
            "CANARY_GROUP": config.get("canaryGroup", f"{prefix.upper()}-Canary"),
            "CANARY_MAX": str(config.get("canaryMax", 5)),
            "DEVICE_DATA_USER_INDEX": "userId-index",
            "CONSENTS_USER_INDEX": "userId-deviceId-index",
            "CONSENTS_JOB_INDEX": "jobId-index",
            "LOG_LEVEL": "INFO",
        }

        # src_suffix: the tail of the source directory name (always digilux_ota_*)
        # deployed_name: the actual Lambda function name (uses configurable prefix)
        def make_fn(construct_id: str, src_suffix: str, role: iam.IRole) -> lambda_.Function:
            deployed_name = f"{prefix}_ota_{src_suffix}"
            src_dir_name  = f"digilux_ota_{src_suffix}"  # source always uses 'digilux' prefix
            log_group = logs.LogGroup(
                self, f"{construct_id}LogGroup",
                log_group_name=f"/aws/lambda/{deployed_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            fn = lambda_.Function(
                self, construct_id,
                function_name=deployed_name,
                runtime=lambda_.Runtime.PYTHON_3_11,
                handler="lambda_function.lambda_handler",
                code=_make_code(src_dir_name),
                role=role,
                timeout=cdk.Duration.seconds(30),
                memory_size=256,
                tracing=lambda_.Tracing.ACTIVE,
                environment=common_env,
                log_group=log_group,
                description=f"{prefix.capitalize()} OTA — {deployed_name}",
            )
            return fn

        # ── Admin Lambdas ─────────────────────────────────────────────────────
        self.upload_url      = make_fn("UploadUrl",      "upload_url",          iam_stack.admin_role)
        self.artifact_proc   = make_fn("ArtifactProc",   "artifact_processor",  iam_stack.admin_role)
        self.pkg_register    = make_fn("PkgRegister",    "package_register",    iam_stack.admin_role)
        self.compat_check    = make_fn("CompatCheck",    "compatibility_check", iam_stack.admin_role)
        self.job_create      = make_fn("JobCreate",      "job_create",          iam_stack.admin_role)
        self.status_handler  = make_fn("StatusHandler",  "status_handler",      iam_stack.admin_role)
        self.device_register = make_fn("DeviceRegister", "device_register",     iam_stack.admin_role)

        # ── User Lambdas ──────────────────────────────────────────────────────
        self.check_updates   = make_fn("CheckUpdates",   "user_check_updates",      iam_stack.user_role)
        self.consent         = make_fn("Consent",        "user_consent",             iam_stack.user_role)
        self.update_status   = make_fn("UpdateStatus",   "user_update_status",       iam_stack.user_role)
        self.download_link   = make_fn("DownloadLink",   "user_get_download_link",   iam_stack.user_role)

        # ── IoT Topic Rules ───────────────────────────────────────────────────
        rule_log_group = logs.LogGroup(
            self, "IotRuleErrorLog",
            log_group_name=f"/{prefix}/ota/rule-errors",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        def make_iot_rule(
            construct_id: str, rule_name: str, sql: str, target_fn: lambda_.Function
        ) -> iot.CfnTopicRule:
            # Grant IoT service permission to invoke the Lambda
            target_fn.add_permission(
                f"IotInvoke{construct_id}",
                principal=iam.ServicePrincipal("iot.amazonaws.com"),
                action="lambda:InvokeFunction",
                source_account=account,
            )
            return iot.CfnTopicRule(
                self, construct_id,
                rule_name=rule_name,
                topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                    sql=sql,
                    aws_iot_sql_version="2016-03-23",
                    rule_disabled=False,
                    actions=[
                        iot.CfnTopicRule.ActionProperty(
                            lambda_=iot.CfnTopicRule.LambdaActionProperty(
                                function_arn=target_fn.function_arn
                            )
                        )
                    ],
                    error_action=iot.CfnTopicRule.ActionProperty(
                        cloudwatch_logs=iot.CfnTopicRule.CloudwatchLogsActionProperty(
                            log_group_name=rule_log_group.log_group_name,
                            role_arn=iam_stack.iot_rule_role.role_arn,
                        )
                    ),
                ),
            )

        # Captures OTA status updates published by the device agent
        make_iot_rule(
            "StatusIngestRule",
            f"{prefix}_ota_status_ingest",
            "SELECT *, topic(3) AS deviceId FROM 'iot/device/+/ota/status'",
            self.status_handler,
        )

        # Triggered when the OTA agent starts on a controller for the first time
        make_iot_rule(
            "DeviceRegisterRule",
            f"{prefix}_ota_device_register_rule",
            "SELECT *, topic(3) AS deviceIdFromTopic FROM 'iot/device/+/ota/register'",
            self.device_register,
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "UploadUrlArn",    value=self.upload_url.function_arn)
        cdk.CfnOutput(self, "CheckUpdatesArn", value=self.check_updates.function_arn)
        cdk.CfnOutput(self, "ConsentArn",      value=self.consent.function_arn)

    @property
    def all_functions(self) -> list[lambda_.Function]:
        """All Lambda functions — used by AlarmsStack to create alarms."""
        return [
            self.upload_url, self.artifact_proc, self.pkg_register,
            self.compat_check, self.job_create, self.status_handler,
            self.device_register, self.check_updates, self.consent,
            self.update_status, self.download_link,
        ]
