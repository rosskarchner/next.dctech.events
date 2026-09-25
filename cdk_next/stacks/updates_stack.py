"""NextUpdatesStack — the /updates post publishers.

Replaces the external Bear Blog at updates.dctech.events. One Lambda on two
schedules writes UPDATE#{publish_date} items; the events table's stream then
fires the existing debounced rebuild, so a post is live on /updates/ within a
couple of minutes with no extra build wiring here.

* **Monday 10:30 UTC** (mode: week_ahead) posts a *link post* — an entry on
  /updates/ and in its feed whose link goes straight to /week/<week_id>/ for
  the week just starting. It builds no page of its own.
* **Wednesday 11:00 UTC** posts a *roundup* — a frozen snapshot of every event
  added in the previous seven days, plus the ARCHIVE# capture of next week that
  the following Monday's post will point at.

That rebuild only happens because UPDATE# is listed in the site generator
trigger's RELEVANT_PREFIXES (lambda_src/site_generator/trigger/handler.py).
Drop it from that allowlist and posts go invisible until the daily
safety-net build.

The roundup's snapshot is the whole point of that post's shape. calgen's
pipeline and get_events() both drop events dated before today, so a post
rendered from live data would empty out as the events it announced happened —
freezing the listing at publish time is what gives the archive permanent
content. The Monday post can get away with storing no listing — and with owning no page
at all — only because /week/ has an ARCHIVE# capture of its own.

Three post shapes render, not two: posts published before 2026-08-12 are keyed
UPDATE#{iso_week} and list the events *happening* that week, which is what the
post used to be. All of them work, because each item carries its own title and
summary and calgen keys the rendering off post_kind.

The same stream also feeds a social publisher that cross-posts to Mastodon and
Bluesky — every UPDATE# kind, and free-form POST# announcements written in
/edit, all through the same code. See lambda_src/social_publisher/app.py.

A second, independent pair of Lambdas cross-posts individual newly-added
events (not /updates posts): social_queue_enqueue watches the same stream for
INSERTed EVENT# rows and drops a SOCIALQUEUE# placeholder for each one;
social_queue_worker drains that queue one item per hour, so a burst of new
events (a freshly-added iCal feed, a run of approved submissions) trickles
out instead of flooding followers' timelines. See
lambda_src/social_queue_enqueue/handler.py and
lambda_src/social_queue_worker/app.py.
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
        # Reads as well as writes: which events are new is answered by
        # scanning EVENT# createdAt, which only the table records.
        table.grant_read_write_data(self.publisher_function)

        # Wednesday 11:00 UTC is 7 AM EDT / 6 AM EST — up before the workday
        # and ahead of the 13:30 UTC daily ops mail. The roundup keeps its own
        # rule because nothing has to happen before it.
        events.Rule(
            self,
            "NextUpdatesPublishSchedule",
            schedule=events.Schedule.expression("cron(0 11 ? * WED *)"),
            targets=[targets.LambdaFunction(self.publisher_function)],
            description="Publish the Wednesday dctech.events /updates roundup",
        )

        # The Monday "week ahead" link post has no rule of its own: it is a
        # step in NextOrchestrationStack's Monday state machine, which invokes
        # this same function with {"mode": "week_ahead"}. It has to run *after*
        # the QC pass and the rebuild that follows it, or the count it freezes
        # into the post counts events QC has just hidden.

        cdk.CfnOutput(
            self,
            "NextUpdatesPublisherFunction",
            value=self.publisher_function.function_name,
            description=(
                "Invoke with {\"published_on\": \"YYYY-MM-DD\"} to republish a "
                "roundup, or add \"mode\": \"week_ahead\" for the Monday link "
                "post (\"dry_run\": true previews, \"force\": true overwrites)"
            ),
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

        # ── new-event social queue ──────────────────────────────────
        # Independent of the publisher above: it announces individual EVENT#
        # rows, not /updates posts, and drains one per hour instead of
        # posting immediately, so a burst of new events doesn't flood
        # followers' timelines. Shares the same Mastodon/Bluesky secrets.
        self.social_queue_enqueue_function = lambda_.Function(
            self,
            "NextSocialQueueEnqueue",
            function_name=f"{config.PREFIX}-social-queue-enqueue",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "social_queue_enqueue")
            ),
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
            },
            log_group=logs.LogGroup(
                self,
                "NextSocialQueueEnqueueLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(self.social_queue_enqueue_function)

        self.social_queue_enqueue_function.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=100,
                max_batching_window=cdk.Duration.seconds(30),
                # A PutItem on an existing key streams as MODIFY, so this
                # already excludes the iCal aggregator's routine re-sync of
                # an event it has seen before, and correction overlays
                # (UpdateItem) — no db.put_event() changes needed to tell
                # "genuinely new" from "touched again".
                filters=[
                    lambda_.FilterCriteria.filter({
                        "eventName": lambda_.FilterRule.is_equal("INSERT"),
                        "dynamodb": {"Keys": {"PK": {
                            "S": lambda_.FilterRule.begins_with("EVENT#")
                        }}},
                    }),
                ],
                retry_attempts=2,
            )
        )

        self.social_queue_worker_function = lambda_.Function(
            self,
            "NextSocialQueueWorker",
            function_name=f"{config.PREFIX}-social-queue-worker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="app.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "social_queue_worker")
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
                "NextSocialQueueWorkerLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(self.social_queue_worker_function)
        mastodon_secret.grant_read(self.social_queue_worker_function)
        bluesky_secret.grant_read(self.social_queue_worker_function)

        # Hourly, but only ~6am-midnight Eastern. Fixed UTC offset (EDT):
        # the window drifts by an hour across the DST transition, which is
        # acceptable rather than pulling in EventBridge Scheduler (unused
        # elsewhere in this codebase) for timezone-aware cron.
        events.Rule(
            self,
            "NextSocialQueueWorkerSchedule",
            schedule=events.Schedule.expression("cron(0 10-23,0-3 * * ? *)"),
            targets=[targets.LambdaFunction(self.social_queue_worker_function)],
            description=(
                "Post one queued new-event announcement per hour, "
                "~6am-midnight Eastern (fixed UTC offset; drifts across DST)"
            ),
        )

        cdk.CfnOutput(
            self,
            "NextSocialQueueWorkerFunction",
            value=self.social_queue_worker_function.function_name,
            description="Invoke with {} to drain the oldest queued event now",
        )
