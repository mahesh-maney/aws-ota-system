import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb, aws_s3 as s3
from constructs import Construct


class StorageStack(cdk.Stack):
    """
    Creates all stateful storage resources:
      - S3 artifact bucket (versioned, encrypted, private)
      - DynamoDB OTA tables with PITR enabled
      - Imports pre-existing device_data table by name
    All resources use RETAIN removal policy — cdk destroy will NOT delete data.
    """

    def __init__(self, scope: Construct, construct_id: str, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = config["namePrefix"]

        # ── S3 Artifact Bucket ────────────────────────────────────────────────
        self.artifact_bucket = s3.Bucket(
            self, "ArtifactBucket",
            bucket_name=config["artifactBucketName"],
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-multipart",
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(7),
                )
            ],
        )

        # ── DynamoDB: packages ────────────────────────────────────────────────
        # PK: packageName (S), SK: version (S)
        self.packages_table = dynamodb.Table(
            self, "PackagesTable",
            table_name=f"{prefix}_ota_packages",
            partition_key=dynamodb.Attribute(name="packageName", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="version", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ── DynamoDB: jobs ────────────────────────────────────────────────────
        # PK: jobId (S), GSI: jobId + createdAt for chronological listing
        self.jobs_table = dynamodb.Table(
            self, "JobsTable",
            table_name=f"{prefix}_ota_jobs",
            partition_key=dynamodb.Attribute(name="jobId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.jobs_table.add_global_secondary_index(
            index_name="createdAt-index",
            partition_key=dynamodb.Attribute(name="jobId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="createdAt", type=dynamodb.AttributeType.NUMBER),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ── DynamoDB: compatibility ───────────────────────────────────────────
        # PK: packageName (S), SK: version (S)
        self.compat_table = dynamodb.Table(
            self, "CompatTable",
            table_name=f"{prefix}_ota_compatibility",
            partition_key=dynamodb.Attribute(name="packageName", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="version", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ── DynamoDB: user_consents ───────────────────────────────────────────
        # PK: consentId (S)
        # GSI1: userId + deviceId  (rate limit queries)
        # GSI2: jobId              (status lookups by job)
        # TTL: ttl attribute (24-hour audit retention)
        self.consents_table = dynamodb.Table(
            self, "ConsentsTable",
            table_name=f"{prefix}_ota_user_consents",
            partition_key=dynamodb.Attribute(name="consentId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.consents_table.add_global_secondary_index(
            index_name="userId-deviceId-index",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="deviceId", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.consents_table.add_global_secondary_index(
            index_name="jobId-index",
            partition_key=dynamodb.Attribute(name="jobId", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ── device_data: pre-existing, imported by name ───────────────────────
        # Managed by the device platform team; OTA fields are attributes on this table.
        # CDK does not create or destroy it — only references it for IAM policies.
        self.device_data_table = dynamodb.Table.from_table_name(
            self, "DeviceDataTable", config["deviceDataTableName"]
        )

        # ── CloudFormation Outputs ────────────────────────────────────────────
        cdk.CfnOutput(self, "ArtifactBucketName", value=self.artifact_bucket.bucket_name,
                      description="S3 bucket for OTA firmware artifacts")
        cdk.CfnOutput(self, "PackagesTableName", value=self.packages_table.table_name,
                      description="DynamoDB table for OTA package metadata")
        cdk.CfnOutput(self, "JobsTableName", value=self.jobs_table.table_name,
                      description="DynamoDB table for OTA job tracking")
        cdk.CfnOutput(self, "ConsentsTableName", value=self.consents_table.table_name,
                      description="DynamoDB table for user OTA consents")
