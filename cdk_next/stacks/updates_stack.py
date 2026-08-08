"""NextUpdatesStack — the weekly /updates post publisher.

Replaces the external Bear Blog at updates.dctech.events. A Monday-morning
Lambda snapshots the current ISO week's events into a single UPDATE#{week_id}
item; the events table's stream then fires the existing debounced rebuild, so
the post is live on /updates/ within a couple of minutes with no extra
build wiring here.

The snapshot is the whole point. calgen's pipeline and get_events() both drop
events dated before today, so a post rendered from live data would empty out
as its week receded into the past — freezing the listing at publish time is
what gives the archive permanent content.
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


class NextUpdatesStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.publisher_function = lambda_.Function(
            self,
            "NextUpdatesPublisher",
            function_name=f"{config.PREFIX}-updates-publisher",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="app.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "updates_publisher")
            ),
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
                # The site's own published feed, so a post can never disagree
                # with the calendar it is summarizing.
                "EVENTS_URL": f"{config.BASE_URL}/events.json",
            },
            log_group=logs.LogGroup(
                self,
                "NextUpdatesPublisherLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_write_data(self.publisher_function)

        events.Rule(
            self,
            "NextUpdatesPublishSchedule",
            # Monday 11:00 UTC — 7 AM EDT / 6 AM EST, so the post is up before
            # the workday and ahead of the 13:30 UTC daily ops mail.
            schedule=events.Schedule.expression("cron(0 11 ? * MON *)"),
            targets=[targets.LambdaFunction(self.publisher_function)],
            description="Publish the weekly dctech.events /updates post",
        )

        cdk.CfnOutput(
            self,
            "NextUpdatesPublisherFunction",
            value=self.publisher_function.function_name,
            description="Invoke with {\"week_of\": \"YYYY-MM-DD\"} to backfill a week",
        )
