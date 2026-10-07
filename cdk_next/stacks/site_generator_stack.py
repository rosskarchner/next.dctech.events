"""NextSiteGeneratorStack — CodeBuild project + triggers for the static site.

CodeBuild (not Lambda) because calgen/Frozen-Flask needs a real writable
directory tree and produces thousands of files. Source is a CDK S3 asset
containing the site source (templates/static/config), the DynamoDB export
script, and a locally-built calgen wheel (from packages/calgen, calgen's only
maintained copy) — one shared rendering implementation, only its data source
changes.

Triggers: a DynamoDB-Streams-fed Lambda that rings RENDER#site.dirtyAt and
starts a build (near-real-time rebuilds after admin approvals and aggregator
runs), a follow-up Lambda that starts one more build when a finished build left
the site dirty, a nightly 05:05 UTC time-roll ring (past events drop off), a
daily 09:00 UTC safety-net build, and on-demand via POST /admin/rebuild / MCP
trigger_rebuild. See ideas/ripple.md (Phase 0) for why it is shaped this way.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudfront as cloudfront,
    aws_codebuild as codebuild,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")

# Stream records that can change a rendered page. Keep in sync with
# RELEVANT_PREFIXES in lambda_src/site_generator/trigger/handler.py (a test
# checks). Used as an event-source filter so records outside it (DRAFT#,
# SUBSCRIBER#, and the RENDER# state item this very trigger writes) never invoke
# the function at all; the handler re-checks, so the filter is an optimisation
# and not the only guard.
SITE_RELEVANT_PREFIXES = (
    "EVENT#", "GROUP#", "CATEGORY#", "RECURRING#", "ICAL#",
    "POST#", "UPDATE#", "ARCHIVE#",
)

# The one state item the trigger, the follow-up and the build share. IAM below is
# scoped to this partition key so none of them can write anything else.
RENDER_STATE_PK = "RENDER#site"

BUILDSPEC = {
    "version": "0.2",
    "phases": {
        "install": {
            "runtime-versions": {"python": "3.12"},
            "commands": [
                # [cards] pulls in Pillow for `calgen og-images` below — an
                # optional extra (see calgen's pyproject.toml) specifically
                # so the ical aggregator/newsletter Lambdas' installs (which
                # never touch image generation) don't have to resolve it
                # too; this is the one place that actually runs it, and a
                # real `pip install` here on a real matching machine, not a
                # local cross-platform `uv` install, so it has no trouble
                # finding a prebuilt wheel.
                'pip install --quiet "$(ls wheels/*.whl)[cards]" boto3',
            ],
        },
        "build": {
            "commands": [
                "cd site",
                # --stamp-export records when this build reads the table
                # (RENDER#site.exportAt) so the follow-up Lambda can tell which
                # stream rings the build covered.
                "python ../export_dynamo_to_calgen.py --table $TABLE_NAME --stamp-export",
                # Deliberately no `calgen refresh` — the iCal Aggregator owns
                # fetching; the export already materialized the cache files.
                "calgen pipeline --site-dir .",
                # Must run after pipeline (needs _data/all_events.json) and
                # before build (Frozen-Flask copies static/ into the frozen
                # output wholesale, so these need to already be there).
                # CALGEN_OG_FONT_DIR points at the card fonts (Noto Sans + Noto
                # Color Emoji) shipped in the source zip next to site/. They stay
                # out of the calgen wheel, which the Lambdas install too. An old
                # wheel ignores the variable, so the order of deploys is safe.
                "CALGEN_OG_FONT_DIR=../og-fonts calgen og-images --site-dir .",
                "calgen build --site-dir .",
                # Two passes, each independently --delete'd, so every object
                # gets an explicit Cache-Control instead of S3's default (no
                # header at all). calgen doesn't fingerprint static asset
                # filenames, so a changed CSS/JS file is not force-fetched by
                # a new URL — max-age is capped at a day (not a
                # year-long "immutable" TTL a hashed-asset pipeline could
                # safely use) so a real change still surfaces for a return
                # visitor within a bounded window; CloudFront's own edge
                # cache is fully invalidated below regardless. HTML gets
                # max-age=0 (revalidate every time) since a rebuild can
                # change any page's content or disappear it entirely.
                # --exclude/--include scope --delete to just that pass's
                # file type — AWS CLI's sync explicitly exempts filtered-out
                # files from deletion, so neither pass can touch the other's
                # files. Filter order matters: the CLI applies rules
                # left-to-right and the LAST matching rule wins. Pass 1 has
                # no --include, so its two --exclude flags simply stack. Pass
                # 2's "--include *.html" would otherwise re-include (and
                # therefore delete, since build/ has no edit/ directory)
                # every html file under edit/ — the manually-deployed
                # frontend (scripts/deploy_edit_ui.sh) this pipeline doesn't
                # own — unless "--exclude edit/*" is placed AFTER it so it's
                # the last, winning rule for anything under edit/. This bit
                # us in production once already: don't reorder these back.
                'aws s3 sync build/ "s3://$SITE_BUCKET/" --delete --exclude "edit/*" --exclude "*.html" --cache-control "public, max-age=86400"',
                'aws s3 sync build/ "s3://$SITE_BUCKET/" --delete --exclude "*" --include "*.html" --exclude "edit/*" --cache-control "public, max-age=0, must-revalidate"',
                'aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*"',
            ],
        },
    },
}


class NextSiteGeneratorStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        site_bucket: s3.IBucket,
        distribution: cloudfront.IDistribution,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        source_asset = s3_assets.Asset(
            self,
            "NextSiteSourceAsset",
            path=os.path.join(BUILD_DIR, "site_src"),
        )

        self.project = codebuild.Project(
            self,
            "NextSiteGenerator",
            project_name=f"{config.PREFIX}-site-generator",
            description="Builds next.dctech.events from DynamoDB via calgen",
            source=codebuild.Source.s3(
                bucket=source_asset.bucket,
                path=source_asset.s3_object_key,
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            # Runs daily plus on every content change plus on-demand, and
            # produces thousands of files per build — unlike every Lambda in
            # this stack (which all set explicit retention), this had no
            # logging= config at all, so its CloudWatch Logs defaulted to
            # never expire.
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=logs.LogGroup(
                        self,
                        "NextSiteGeneratorLogGroup",
                        retention=logs.RetentionDays.ONE_MONTH,
                        removal_policy=cdk.RemovalPolicy.DESTROY,
                    ),
                ),
            ),
            environment_variables={
                "TABLE_NAME": codebuild.BuildEnvironmentVariable(
                    value=table.table_name
                ),
                "SITE_BUCKET": codebuild.BuildEnvironmentVariable(
                    value=site_bucket.bucket_name
                ),
                "DISTRIBUTION_ID": codebuild.BuildEnvironmentVariable(
                    value=distribution.distribution_id
                ),
            },
            build_spec=codebuild.BuildSpec.from_object(BUILDSPEC),
            # One build at a time. Every build ends in `s3 sync --delete` over
            # the whole site bucket, so two overlapping builds can have an
            # older one deleting files a newer one just wrote.
            #
            # This does not do what its name suggests: a second StartBuild
            # *fails* with AccountLimitExceededException rather than being
            # queued. The stream-fed trigger therefore stamps
            # RENDER#site.dirtyAt first and treats a refusal as success; the
            # follow-up Lambda starts one more build when the running one
            # finishes. The Monday state machine's startBuild.sync steps still
            # retry through the wait themselves (next_dctech_events-lux).
            concurrent_build_limit=1,
            timeout=cdk.Duration.minutes(30),
        )

        table.grant_read_data(self.project)
        site_bucket.grant_read_write(self.project)
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObject"],
                resources=[site_bucket.arn_for_objects("*")],
            )
        )
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[
                    f"arn:aws:cloudfront::{self.account}:distribution/{distribution.distribution_id}"
                ],
            )
        )

        render_state_condition = {
            "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [RENDER_STATE_PK]}
        }
        # The build stamps exportAt on the state item (and nothing else).
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[table.table_arn],
                conditions=render_state_condition,
            )
        )

        # Streams-fed trigger for near-real-time rebuilds: rings dirtyAt, then
        # tries to start a build. A refused start is not an error; the follow-up
        # Lambda below re-drives it when the running build finishes.
        trigger_fn = lambda_.Function(
            self,
            "NextSiteBuildTrigger",
            function_name=f"{config.PREFIX}-site-build-trigger",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "site_generator_trigger")
            ),
            timeout=cdk.Duration.seconds(60),
            environment={
                "CODEBUILD_PROJECT_NAME": self.project.project_name,
                "TABLE_NAME": table.table_name,
            },
            log_group=logs.LogGroup(
                self,
                "NextSiteBuildTriggerLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        trigger_fn.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=1000,
                max_batching_window=cdk.Duration.seconds(90),
                retry_attempts=1,
                # One pattern, not one per prefix: a mapping allows five
                # filters and there are eight prefixes. Values in a field's
                # array OR together.
                filters=[
                    lambda_.FilterCriteria.filter({
                        "dynamodb": {"Keys": {"PK": {"S": [
                            rule
                            for prefix in SITE_RELEVANT_PREFIXES
                            for rule in lambda_.FilterRule.begins_with(prefix)
                        ]}}}
                    })
                ],
            )
        )
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild"],
                resources=[self.project.project_arn],
            )
        )
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[table.table_arn],
                conditions=render_state_condition,
            )
        )

        # When a build ends, start one more if the site went dirty after that
        # build read the table. Same code asset as the trigger.
        followup_fn = lambda_.Function(
            self,
            "NextSiteBuildFollowup",
            function_name=f"{config.PREFIX}-site-build-followup",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="followup.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(BUILD_DIR, "site_generator_trigger")
            ),
            timeout=cdk.Duration.seconds(60),
            environment={
                "CODEBUILD_PROJECT_NAME": self.project.project_name,
                "TABLE_NAME": table.table_name,
            },
            log_group=logs.LogGroup(
                self,
                "NextSiteBuildFollowupLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        followup_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[self.project.project_arn],
            )
        )
        followup_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[table.table_arn],
                conditions=render_state_condition,
            )
        )
        events.Rule(
            self,
            "NextSiteBuildFinished",
            event_pattern=events.EventPattern(
                source=["aws.codebuild"],
                detail_type=["CodeBuild Build State Change"],
                detail={
                    "project-name": [self.project.project_name],
                    "build-status": [
                        "SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT",
                    ],
                },
            ),
            targets=[targets.LambdaFunction(followup_fn)],
            description="Start a follow-up site build if the site went dirty during this one",
        )

        # Nightly time roll: nothing in the table changes at midnight, but
        # "today" does, and past events have to drop off every page that lists
        # them. Fixed 05:05 UTC rather than EventBridge Scheduler's
        # timezone-aware cron (unused elsewhere in this codebase, see
        # updates_stack.py): that is 00:05 EST and 01:05 EDT, so it always
        # lands after Eastern midnight and only the margin drifts. It rings the
        # same doorbell as a stream change, so it coalesces with a running
        # build instead of colliding with it.
        events.Rule(
            self,
            "NextSiteTimeRollSchedule",
            schedule=events.Schedule.expression("cron(5 5 * * ? *)"),
            targets=[
                targets.LambdaFunction(
                    trigger_fn,
                    event=events.RuleTargetInput.from_object(
                        {"reason": "time_roll"}
                    ),
                )
            ],
            description="Rebuild just after Eastern midnight so past events drop off",
        )

        # Daily safety-net rebuild at 4 AM EST (09:00 UTC; 5 AM during EDT).
        # Real content changes rebuild within ~90s via the streams trigger,
        # so this only catches anything the trigger missed.
        events.Rule(
            self,
            "NextSiteBuildSchedule",
            schedule=events.Schedule.expression("cron(0 9 * * ? *)"),
            targets=[targets.CodeBuildProject(self.project)],
            description="Daily safety-net rebuild of next.dctech.events (4 AM EST)",
        )

        cdk.CfnOutput(self, "NextSiteGeneratorProject", value=self.project.project_name)
