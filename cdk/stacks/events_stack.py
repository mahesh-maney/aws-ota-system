import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_, aws_s3 as s3, aws_s3_notifications as s3n
from constructs import Construct


class EventsStack(cdk.Stack):
    """
    Wires up S3 event-driven triggers without creating cross-stack dependency cycles.

    All ARNs are constructed from plain config strings — no CDK Tokens — so CDK
    sees no stack-level dependencies between this stack and StorageStack/LambdaStack.

      S3 OBJECT_CREATED → artifact_processor Lambda
        Triggered whenever a firmware artifact is uploaded to the OTA bucket.
    """

    def __init__(
        self, scope: Construct, construct_id: str,
        config: dict, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = config["namePrefix"]
        region = config["region"]
        account = config["account"]

        # Import bucket and Lambda by concrete ARN (no CDK Tokens — no cycle)
        bucket = s3.Bucket.from_bucket_name(
            self, "ArtifactBucket", config["artifactBucketName"]
        )
        fn_arn = f"arn:aws:lambda:{region}:{account}:function:{prefix}_ota_artifact_processor"
        artifact_proc = lambda_.Function.from_function_arn(self, "ArtifactProc", fn_arn)

        # S3 event: one notification per deviceType prefix → artifact_processor
        # Prefixes match the DEVICE_TYPE_MAP keys in upload_url lambda.
        device_type_prefixes = [
            "Network_controller_firmware/",
            "Network_controller_zigbee_firmware/",
            "Network_controller_Z2M_Firmware/",
            "Network_controller_Miscellaneous/",
        ]
        for i, pfx in enumerate(device_type_prefixes):
            bucket.add_event_notification(
                s3.EventType.OBJECT_CREATED,
                s3n.LambdaDestination(artifact_proc),
                s3.NotificationKeyFilter(prefix=pfx),
            )
