"""NextOrchestrationStack — the Monday state machine.

Monday used to be four independent EventBridge rules ordered only by wall
clock: the QC pass at 09:00, the daily site build also at 09:00, the
week-ahead link post at 10:30, the newsletter at 11:00. Nothing waited for
anything, and at the time the site build trigger *dropped* work when a build
was already running, so the collisions were not harmless:

* QC writes overlays onto EVENT# items, which is a rebuild trigger. Landing
  those writes inside the 09:00 scheduled build meant the trigger skipped, and
  nothing queued another build. (The trigger no longer skips — see
  next_dctech_events-lux — but the ordering below is still what makes the
  week-ahead post count a clean calendar.)
* The week-ahead post freezes a count read from the published events.json. If
  no build had run since QC, that count included the events QC had just
  hidden.

The order actually required is a chain, and a chain is what a state machine
expresses:

    RefreshFeeds        so QC and the post both see the weekend's imports
        │
    RunQualityControl   waits on a task token the agent releases itself
        │               (failure is caught: QC is best-effort)
    BuildSite           events.json now reflects QC's hides
        │
    PublishWeekAhead    freezes a count taken from the clean events.json
        │
    BuildSite           the post is live
        │
    SendNewsletter      last, so it never links a post that is not up yet

`codebuild:startBuild.sync` is the load-bearing integration: it *waits* for
the build, so the chain never races ahead of the site it just published.
The stream-fed trigger still fires during a run, and now starts a build rather
than skipping; the project's concurrent_build_limit of 1 makes CodeBuild queue
it behind this machine's. That is why BUILD_TIMEOUT allows for a wait.

Referenced by function name rather than by cross-stack import, matching the
reasoning NextUpdatesStack already applies to the social secrets: this stack
touches five others, and a CloudFormation dependency on each would mean a
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

# A pass over ~30 events with browser lookups runs well past Lambda's ceiling;
# an hour is generous rather than tight. The point of the timeout is that a
# crashed agent that never releases its token cannot stall Monday forever —
# the agent reports its own failures, so this only covers a hard death.
QC_TIMEOUT = cdk.Duration.hours(1)

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
        qa_trigger = lambda_.Function.from_function_name(
            self, "QaTriggerRef", f"{config.PREFIX}-qa-trigger"
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

        run_qc = tasks.LambdaInvoke(
            self,
            "RunQualityControl",
            lambda_function=qa_trigger,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            # The trigger forwards this into the agent's payload; the agent
            # calls SendTaskSuccess when the pass is genuinely over. Releasing
            # it here would defeat the point.
            #
            # This depends on the trigger returning promptly, which it only
            # does because it abandons the blocking InvokeAgentRuntime response
            # on a short read timeout. Before that fix the trigger always died
            # at its own 60s timeout, which this step would have read as a task
            # failure and caught straight past — skipping QC every Monday while
            # the agent ran on regardless and released its token into a dead
            # execution. If the trigger ever goes back to waiting, this breaks
            # silently in exactly that way.
            payload=sfn.TaskInput.from_object({
                "run_id": sfn.JsonPath.string_at("$$.Execution.Name"),
                "task_token": sfn.JsonPath.task_token,
            }),
            task_timeout=sfn.Timeout.duration(QC_TIMEOUT),
            result_path=sfn.JsonPath.DISCARD,
        )

        build_after_qc = tasks.CodeBuildStartBuild(
            self,
            "BuildSiteAfterQc",
            project=site_generator,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            task_timeout=sfn.Timeout.duration(BUILD_TIMEOUT),
            result_path=sfn.JsonPath.DISCARD,
            comment="Publish QC's hides so the next step counts the real calendar",
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

        for step, attempts in ((refresh_feeds, 2), (build_after_qc, 1),
                               (publish_week_ahead, 2), (build_after_post, 1),
                               (send_newsletter, 1)):
            step.add_retry(
                errors=["States.TaskFailed", "Lambda.ServiceException",
                        "Lambda.TooManyRequestsException"],
                max_attempts=attempts,
                interval=cdk.Duration.seconds(30),
                backoff_rate=2.0,
            )

        # QC is the one step allowed to fail without stopping Monday. It
        # improves the calendar; it does not produce it. A crashed agent must
        # not cost the week its post and its newsletter, so both the task
        # failure the agent reports and the timeout on a hard death fall
        # through to the build.
        run_qc.add_catch(
            build_after_qc,
            errors=["States.ALL"],
            result_path=sfn.JsonPath.DISCARD,
        )

        definition = refresh_feeds.next(
            run_qc.next(
                build_after_qc.next(
                    publish_week_ahead.next(
                        build_after_post.next(send_newsletter)
                    )
                )
            )
        )

        self.state_machine = sfn.StateMachine(
            self,
            "NextMondayStateMachine",
            state_machine_name=f"{config.PREFIX}-monday",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            # Longer than QC_TIMEOUT plus both builds, so the execution
            # timeout is a backstop and never the thing that fires first.
            timeout=cdk.Duration.hours(2),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "NextMondayStateMachineLogGroup",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=cdk.RemovalPolicy.DESTROY,
                ),
                level=sfn.LogLevel.ALL,
                # The QC step's payload carries a task token. Execution data
                # is what would put it in CloudWatch.
                include_execution_data=False,
            ),
            tracing_enabled=True,
        )

        events.Rule(
            self,
            "NextMondaySchedule",
            # 09:15 UTC, a quarter hour after the daily site build, so the
            # machine's first waited-on build is not queued behind it. The
            # chain then lands the newsletter around 10:00-10:30 UTC, earlier
            # than the 11:00 it used to go out at, because it no longer has to
            # leave slack for a QC pass whose finish nothing could observe.
            schedule=events.Schedule.expression("cron(15 9 ? * MON *)"),
            targets=[targets.SfnStateMachine(self.state_machine)],
            description="Monday: refresh feeds, QC, build, week-ahead post, newsletter",
        )

        cdk.CfnOutput(
            self,
            "NextMondayStateMachineArn",
            value=self.state_machine.state_machine_arn,
            description=(
                "Start an execution to run Monday's chain by hand; the "
                "execution name becomes the QC run id"
            ),
        )
