"""NextOrchestrationStack — the Monday state machine.

Monday used to be four independent EventBridge rules ordered only by wall
clock: the QC pass at 09:00, the daily site build also at 09:00, the
week-ahead link post at 10:30, the newsletter at 11:00. Nothing waited for
anything, and at the time the site build trigger *dropped* work when a build
was already running, so the collisions were not harmless:

* QC writes overlays onto EVENT# items, which is a rebuild trigger. Landing
  those writes inside the 09:00 scheduled build meant the trigger skipped, and
  nothing queued another build. (The trigger no longer skips — see
  next_dctech_events-lux.)
* The week-ahead post freezes a count read from the published events.json. If
  no build had run since QC, that count included events QC had just hidden.

QC used to be a synchronous step in this chain (RunQualityControl, waiting on
a Step Functions task token the AgentCore agent released itself) specifically
so the week-ahead count was always taken after QC's fixes landed. As of
2026-09-19 QC runs as a Claude Scheduled Task, nightly, independent of this
chain — see the retired NextQaAgentStack and NEXT_MCP_AGENT_MIGRATION.md. A
cron-based, pull-only scheduled task has no way to receive a task token and
signal completion back synchronously, so that guarantee is now approximate
instead of exact: nightly QC means at most one night's worth of newly
imported events can be uncorrected when this chain freezes Monday's count,
rather than a whole week's, which was judged an acceptable trade for
retiring the AgentCore/Bedrock cost surface entirely. Revisit if that
approximation ever visibly matters (e.g. a bad Sunday-night duplicate making
it into the week-ahead post).

The remaining order is still a chain, and a chain is what a state machine
expresses:

    RefreshFeeds        so the post sees the weekend's imports
        │
    BuildSite           events.json reflects the latest nightly QC pass
        │
    PublishWeekAhead    freezes a count taken from that events.json
        │
    BuildSite           the post is live
        │
    SendNewsletter      last, so it never links a post that is not up yet

`codebuild:startBuild.sync` is the load-bearing integration: it *waits* for
the build, so the chain never races ahead of the site it just published.
The stream-fed trigger still fires during a run and now attempts a build rather
than skipping. The project's concurrent_build_limit of 1 makes that attempt fail
rather than queue, so the trigger raises and its event source mapping re-drives
the batch once this machine's build is done.

Referenced by function name rather than by cross-stack import, matching the
reasoning NextUpdatesStack already applies to the social secrets: this stack
touches several others, and a CloudFormation dependency on each would mean a
change to any one of them could not deploy without this one.

The newsletter's own rule survives in NextNewsletterStack, disabled, so
re-enabling it is the fallback if this machine is ever removed. The daily
09:00 site build also survives, as the safety net for every other day.
"""
import aws_cdk as cdk
from aws_cdk import (
    aws_codebuild as codebuild,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

import config

# Two site builds, each waited on. A cold build of the whole site takes a
# couple of minutes; the ceiling is for a wedged one — and for the wait when
# the project's concurrent_build_limit of 1 has queued this build behind the
# stream-fed trigger's.
BUILD_TIMEOUT = cdk.Duration.minutes(30)


class NextOrchestrationStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        aggregator = lambda_.Function.from_function_name(
            self, "IcalAggregatorRef", f"{config.PREFIX}-ical-aggregator"
        )
        updates_publisher = lambda_.Function.from_function_name(
            self, "UpdatesPublisherRef", f"{config.PREFIX}-updates-publisher"
        )
        newsletter_sender = lambda_.Function.from_function_name(
            self, "NewsletterSenderRef", f"{config.PREFIX}-newsletter-sender"
        )
        site_generator = codebuild.Project.from_project_name(
            self, "SiteGeneratorRef", f"{config.PREFIX}-site-generator"
        )

        # ── steps ────────────────────────────────────────────────────
        refresh_feeds = tasks.LambdaInvoke(
            self,
            "RefreshFeeds",
            lambda_function=aggregator,
            payload_response_only=True,
            result_path=sfn.JsonPath.DISCARD,
            comment="Import the weekend's iCal updates before anything reads them",
        )

        build_after_refresh = tasks.CodeBuildStartBuild(
            self,
            "BuildSiteAfterRefresh",
            project=site_generator,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            task_timeout=sfn.Timeout.duration(BUILD_TIMEOUT),
            result_path=sfn.JsonPath.DISCARD,
            comment="Publish the latest feed imports (and nightly QC's fixes) so the next step counts the real calendar",
        )

        publish_week_ahead = tasks.LambdaInvoke(
            self,
            "PublishWeekAhead",
            lambda_function=updates_publisher,
            payload=sfn.TaskInput.from_object({"mode": "week_ahead"}),
            payload_response_only=True,
            result_path=sfn.JsonPath.DISCARD,
        )

        build_after_post = tasks.CodeBuildStartBuild(
            self,
            "BuildSiteAfterPost",
            project=site_generator,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            task_timeout=sfn.Timeout.duration(BUILD_TIMEOUT),
            result_path=sfn.JsonPath.DISCARD,
            comment="Make the week-ahead post live before the newsletter links it",
        )

        send_newsletter = tasks.LambdaInvoke(
            self,
            "SendNewsletter",
            lambda_function=newsletter_sender,
            payload_response_only=True,
        )

        for step, attempts in ((refresh_feeds, 2), (build_after_refresh, 1),
                               (publish_week_ahead, 2), (build_after_post, 1),
                               (send_newsletter, 1)):
            step.add_retry(
                errors=["States.TaskFailed", "Lambda.ServiceException",
                        "Lambda.TooManyRequestsException"],
                max_attempts=attempts,
                interval=cdk.Duration.seconds(30),
                backoff_rate=2.0,
            )

        definition = refresh_feeds.next(
            build_after_refresh.next(
                publish_week_ahead.next(
                    build_after_post.next(send_newsletter)
                )
            )
        )

        self.state_machine = sfn.StateMachine(
            self,
            "NextMondayStateMachine",
            state_machine_name=f"{config.PREFIX}-monday",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            # Longer than both builds combined, so the execution timeout is a
            # backstop and never the thing that fires first.
            timeout=cdk.Duration.hours(1),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "NextMondayStateMachineLogGroup",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=cdk.RemovalPolicy.DESTROY,
                ),
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            tracing_enabled=True,
        )

        events.Rule(
            self,
            "NextMondaySchedule",
            # 09:15 UTC, a quarter hour after the daily site build, so the
            # machine's first waited-on build is not queued behind it. QC no
            # longer runs inside this chain (nightly Scheduled Task instead —
            # see the module docstring), so the newsletter lands as soon as
            # both builds and the week-ahead post are done.
            schedule=events.Schedule.expression("cron(15 9 ? * MON *)"),
            targets=[targets.SfnStateMachine(self.state_machine)],
            description="Monday: refresh feeds, build, week-ahead post, newsletter",
        )

        cdk.CfnOutput(
            self,
            "NextMondayStateMachineArn",
            value=self.state_machine.state_machine_arn,
            description="Start an execution to run Monday's chain by hand",
        )
