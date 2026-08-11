import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cwa,
    aws_lambda as lambda_,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

from .lambda_stack import LambdaStack


class AlarmsStack(cdk.Stack):
    """
    Creates CloudWatch alarms for all 11 OTA Lambda functions.

    Per function:
      - Errors    — alerts on ≥1 error in any 60-second window
      - Throttles — alerts on ≥1 throttle in any 60-second window
      - Duration  — alerts if p99 latency exceeds 24 000ms (80% of 30s timeout)

    All alarms route to an SNS topic. If alertEmail is set in config, an email
    subscription is created automatically (requires confirmation on first deploy).
    """

    def __init__(
        self, scope: Construct, construct_id: str,
        config: dict, lambdas: LambdaStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = config["namePrefix"]

        # ── SNS Alert Topic ───────────────────────────────────────────────────
        topic = sns.Topic(
            self, "AlertsTopic",
            topic_name=f"{prefix}-ota-alerts",
            display_name=f"{prefix.capitalize()} OTA Alerts",
        )
        if email := config.get("alertEmail"):
            topic.add_subscription(subs.EmailSubscription(email))

        sns_action = cwa.SnsAction(topic)

        # ── Alarms per Lambda ─────────────────────────────────────────────────
        for fn in lambdas.all_functions:
            self._add_alarms(fn, sns_action)

        cdk.CfnOutput(self, "AlertsTopicArn", value=topic.topic_arn,
                      description="SNS topic receiving all OTA Lambda alarms")

    def _add_alarms(self, fn: lambda_.Function, action: cwa.SnsAction) -> None:
        name = fn.function_name
        # Construct ID must be a plain string (no CDK tokens).
        # fn.node.id is the static ID set when the Function was created (e.g. "UploadUrl").
        safe = fn.node.id

        period = cdk.Duration.minutes(1)

        # ── Errors ────────────────────────────────────────────────────────────
        errors = cw.Alarm(
            self, f"{safe}Errors",
            alarm_name=f"{name}-errors",
            alarm_description=f"OTA Lambda errors: {name}",
            metric=fn.metric_errors(period=period, statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        errors.add_alarm_action(action)
        errors.add_ok_action(action)

        # ── Throttles ─────────────────────────────────────────────────────────
        throttles = cw.Alarm(
            self, f"{safe}Throttles",
            alarm_name=f"{name}-throttles",
            alarm_description=f"OTA Lambda throttles: {name}",
            metric=fn.metric_throttles(period=period, statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        throttles.add_alarm_action(action)
        throttles.add_ok_action(action)

        # ── Duration p99 > 24s ────────────────────────────────────────────────
        duration_metric = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Duration",
            dimensions_map={"FunctionName": name},
            statistic="p99",
            period=period,
        )
        duration = cw.Alarm(
            self, f"{safe}Duration",
            alarm_name=f"{name}-duration",
            alarm_description=f"OTA Lambda p99 duration > 24s: {name}",
            metric=duration_metric,
            threshold=24_000,  # ms — 80% of 30s timeout
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        duration.add_alarm_action(action)
        duration.add_ok_action(action)
