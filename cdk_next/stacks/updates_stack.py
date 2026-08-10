"""NextUpdatesStack — the weekly /updates post publisher.

Replaces the external Bear Blog at updates.dctech.events. A Monday-morning
Lambda snapshots the current ISO week's events into a single UPDATE#{week_id}
item; the events table's stream then fires the existing debounced rebuild, so
the post is live on /updates/ within a couple of minutes with no extra
build wiring here.

That rebuild only happens because UPDATE# is listed in the site generator
trigger's RELEVANT_PREFIXES (lambda_src/site_generator/trigger/handler.py).
Drop it from that allowlist and posts go invisible until the daily
safety-net build.

The snapshot is the whole point. calgen's pipeline and get_events() both drop
events dated before today, so a post rendered from live data would empty out
as its week receded into the past — freezing the listing at publish time is
what gives the archive permanent content.

The same stream also feeds a social publisher that cross-posts the permalink
to Mastodon and Bluesky. It lives here rather than inside the weekly
publisher so that free-form POST# announcements written in /edit are
syndicated by the same code — see lambda_src/social_publisher/app.py.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
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

        # ── social cross-posting ────────────────────────────────────
        # Referenced by name, not by cross-stack import: NextSocialStack
        # exists so credentials can be rotated or re-populated without an
        # application deploy, and a CloudFormation dependency between the
        # two would put that back.
        mastodon_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "NextMastodonSecretRef", f"{config.PREFIX}/mastodon"
        )
        bluesky_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "NextBlueskySecretRef", f"{config.PREFIX}/bluesky"
        )

        self.social_function = lambda_.Function(
            self,
            "NextSocialPublisher",
            function_name=f"{config.PREFIX}-social-publisher",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="app.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "social_publisher")
            ),
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
                "SITE_BASE_URL": config.BASE_URL,
                "MASTODON_SECRET_NAME": f"{config.PREFIX}/mastodon",
                "BLUESKY_SECRET_NAME": f"{config.PREFIX}/bluesky",
            },
            log_group=logs.LogGroup(
                self,
                "NextSocialPublisherLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        # Reads the post it is announcing, reads and writes the SOCIAL#
        # dedupe record that keeps stream retries from double-posting.
        table.grant_read_write_data(self.social_function)
        mastodon_secret.grant_read(self.social_function)
        bluesky_secret.grant_read(self.social_function)

        self.social_function.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=100,
                max_batching_window=cdk.Duration.seconds(30),
                # Two filters OR together. Without them the iCal aggregator's
                # EVENT# churn would invoke this function thousands of times a
                # day just to have it decide there is nothing to post.
                filters=[
                    lambda_.FilterCriteria.filter({
                        "dynamodb": {"Keys": {"PK": {
                            "S": lambda_.FilterRule.begins_with("UPDATE#")
                        }}}
                    }),
                    lambda_.FilterCriteria.filter({
                        "dynamodb": {"Keys": {"PK": {
                            "S": lambda_.FilterRule.begins_with("POST#")
                        }}}
                    }),
                ],
                retry_attempts=2,
            )
        )

        cdk.CfnOutput(
            self,
            "NextSocialPublisherFunction",
            value=self.social_function.function_name,
            description=(
                "Invoke with {\"pk\": \"UPDATE#YYYY-Www\"} to backfill a post "
                "(add \"dry_run\": true to preview, \"force\": true to repost)"
            ),
        )
