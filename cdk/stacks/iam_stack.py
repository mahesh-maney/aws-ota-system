import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from .storage_stack import StorageStack


class IamStack(cdk.Stack):
    """
    Creates three IAM roles:
      admin_role    — execution role for admin OTA Lambda functions
      user_role     — execution role for user-facing OTA Lambda functions
      iot_rule_role — execution role for IoT topic rules (invokes Lambdas)

    All policies follow least-privilege: each role has only the permissions
    needed for its specific Lambda functions.
    """

    def __init__(
        self, scope: Construct, construct_id: str,
        config: dict, storage: StorageStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = config["region"]
        account = config["account"]
        prefix = config["namePrefix"]
        cognito_pool_arns = [
            f"arn:aws:cognito-idp:{region}:{account}:userpool/{config['cognitoUserPoolId']}",
            f"arn:aws:cognito-idp:{region}:{account}:userpool/{config['otaAdminUserPoolId']}",
        ]
        lambda_trust = iam.ServicePrincipal("lambda.amazonaws.com")

        # ── Admin Lambda Role ─────────────────────────────────────────────────
        self.admin_role = iam.Role(
            self, "AdminLambdaRole",
            role_name=f"{prefix}-ota-lambda-role",
            assumed_by=lambda_trust,
            description=f"Execution role for {prefix} OTA admin Lambda functions",
            inline_policies={
                f"{prefix}-ota-admin-policy": iam.PolicyDocument(statements=[
                    # CloudWatch Logs
                    iam.PolicyStatement(
                        sid="Logs",
                        actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                        resources=[
                            f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{prefix}_ota_*"
                        ],
                    ),
                    # DynamoDB — all OTA tables + pre-existing device_data
                    iam.PolicyStatement(
                        sid="DynamoDB",
                        actions=[
                            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                            "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
                        ],
                        resources=[
                            f"arn:aws:dynamodb:{region}:{account}:table/{prefix}_ota_*",
                            f"arn:aws:dynamodb:{region}:{account}:table/{prefix}_ota_*/index/*",
                            storage.device_data_table.table_arn,
                            f"{storage.device_data_table.table_arn}/index/*",
                        ],
                    ),
                    # S3 — artifact bucket
                    iam.PolicyStatement(
                        sid="S3",
                        actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
                        resources=[
                            storage.artifact_bucket.bucket_arn,
                            f"{storage.artifact_bucket.bucket_arn}/*",
                        ],
                    ),
                    # IoT — job management + thing operations
                    iam.PolicyStatement(
                        sid="IoT",
                        actions=[
                            "iot:CreateJob", "iot:DescribeJob", "iot:ListJobs", "iot:CancelJob",
                            "iot:ListJobExecutionsForJob", "iot:ListJobExecutionsForThing",
                            "iot:DescribeJobExecution", "iot:DescribeThing", "iot:ListThings",
                            "iot:AddThingToThingGroup", "iot:RemoveThingFromThingGroup",
                            "iot:DescribeThingGroup", "iot:GetThingShadow", "iot:UpdateThingShadow",
                            "iot:SearchIndex", "iot:TagResource",
                        ],
                        resources=["*"],
                    ),
                    # Secrets Manager — signing key + CloudFront key
                    iam.PolicyStatement(
                        sid="Secrets",
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}-ota-*"
                        ],
                    ),
                    # Cognito — admin group check
                    iam.PolicyStatement(
                        sid="Cognito",
                        actions=[
                            "cognito-idp:GetUser",
                            "cognito-idp:AdminGetUser",
                            "cognito-idp:AdminListGroupsForUser",
                        ],
                        resources=cognito_pool_arns,
                    ),
                    # X-Ray
                    iam.PolicyStatement(
                        sid="XRay",
                        actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                        resources=["*"],
                    ),
                ]),
            },

        # ── User Lambda Role ──────────────────────────────────────────────────
        self.user_role = iam.Role(
            self, "UserLambdaRole",
            role_name=f"{prefix}-ota-user-lambda-role",
            assumed_by=lambda_trust,
            description=f"Execution role for {prefix} OTA user-facing Lambda functions",
            inline_policies={
                f"{prefix}-ota-user-policy": iam.PolicyDocument(statements=[
                    # Logs
                    iam.PolicyStatement(
                        sid="Logs",
                        actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                        resources=[
                            f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{prefix}_ota_user_*:*"
                        ],
                    ),
                    # device_data — read + write pendingJobId
                    iam.PolicyStatement(
                        sid="DeviceDataReadWrite",
                        actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
                        resources=[
                            storage.device_data_table.table_arn,
                            f"{storage.device_data_table.table_arn}/index/*",
                        ],
                    ),
                    # packages — read only
                    iam.PolicyStatement(
                        sid="PackagesRead",
                        actions=["dynamodb:GetItem", "dynamodb:Query"],
                        resources=[storage.packages_table.table_arn],
                    ),
                    # jobs — read + write
                    iam.PolicyStatement(
                        sid="JobsReadWrite",
                        actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"],
                        resources=[
                            storage.jobs_table.table_arn,
                            f"{storage.jobs_table.table_arn}/index/*",
                        ],
                    ),
                    # consents — read + write
                    iam.PolicyStatement(
                        sid="ConsentsReadWrite",
                        actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"],
                        resources=[
                            storage.consents_table.table_arn,
                            f"{storage.consents_table.table_arn}/index/*",
                        ],
                    ),
                    # IoT — create job + check canary group membership
                    iam.PolicyStatement(
                        sid="IoTJobs",
                        actions=[
                            "iot:CreateJob", "iot:DescribeJob", "iot:TagResource",
                            "iot:ListThingGroupsForThing",
                        ],
                        resources=[
                            f"arn:aws:iot:{region}:{account}:job/{prefix}-ota-*",
                            f"arn:aws:iot:{region}:{account}:thing/*",
                        ],
                    ),
                    # IoT — publish download notification to device
                    iam.PolicyStatement(
                        sid="IoTPublish",
                        actions=["iot:Publish"],
                        resources=[
                            f"arn:aws:iot:{region}:{account}:topic/iot/device/*/ota"
                        ],
                    ),
                    # S3 — presign download URLs
                    iam.PolicyStatement(
                        sid="S3Presign",
                        actions=["s3:GetObject"],
                        resources=[f"{storage.artifact_bucket.bucket_arn}/*"],
                    ),
                    # Secrets — CloudFront private key
                    iam.PolicyStatement(
                        sid="CloudFrontKey",
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}-ota-cloudfront-key*"
                        ],
                    ),
                    # X-Ray
                    iam.PolicyStatement(
                        sid="XRay",
                        actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                        resources=["*"],
                    ),
                ]),
            },

        # ── IoT Rule Role ─────────────────────────────────────────────────────
        # Used by IoT topic rules to invoke Lambda functions and write error logs
        self.iot_rule_role = iam.Role(
            self, "IotRuleRole",
            role_name=f"{prefix}-ota-iot-rule-role",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com"),
            description=f"Role for IoT topic rules to invoke {prefix} OTA Lambda functions",
            inline_policies={
                f"{prefix}-ota-iot-rule-policy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        sid="InvokeLambda",
                        actions=["lambda:InvokeFunction"],
                        resources=[
                            f"arn:aws:lambda:{region}:{account}:function:{prefix}_ota_status_handler",
                            f"arn:aws:lambda:{region}:{account}:function:{prefix}_ota_device_register",
                        ],
                    ),
                    iam.PolicyStatement(
                        sid="ErrorLogs",
                        actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                        resources=[
                            f"arn:aws:logs:{region}:{account}:log-group:/{prefix}/ota/rule-errors*"
                        ],
                    ),
                ]),
            },
