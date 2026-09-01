"""NextIcalAggregatorStack — Lambda + EventBridge schedule for iCal fetching.

Every 4 hours (matching calgen's internal fetch-throttle window). Single
Lambda handles all groups; if the 15-minute cap becomes a problem with 150+
groups, revisit with an SQS fan-out.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")


class NextIcalAggregatorStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.function = lambda_.Function(
            self,
            "NextIcalAggregator",
            function_name=f"{config.PREFIX}-ical-aggregator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "ical_aggregator")),
            timeout=cdk.Duration.minutes(15),
            # CloudWatch Logs REPORT lines (queried via Logs Insights) show
            # max memory used holding steady at 130-136MB across 46
            # invocations over the last 8 days, run every 4 hours (the
            # highest-frequency compute in this stack, 180 runs/month) — 1GB
            # was ~7.5x more than ever used. 512MB keeps ~3.7x headroom above
            # the observed peak for feed-size growth over time, without
            # cutting it close enough to risk an OOM kill on an unattended
            # cron job nobody's watching in real time.
            memory_size=512,
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
            },
            log_group=logs.LogGroup(
                self,
                "NextIcalAggregatorLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(self.function)

        events.Rule(
            self,
            "NextIcalAggregatorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.hours(4)),
            targets=[targets.LambdaFunction(self.function)],
            description="Fetch iCal feeds for next.dctech.events every 4 hours",
        )

        cdk.CfnOutput(self, "NextIcalAggregatorFunction", value=self.function.function_name)
