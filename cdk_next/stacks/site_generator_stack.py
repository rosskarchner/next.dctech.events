"""NextSiteGeneratorStack — CodeBuild project + triggers for the static site.

CodeBuild (not Lambda) because calgen/Frozen-Flask needs a real writable
directory tree and produces thousands of files. Source is a CDK S3 asset
containing the site source (templates/static/config), the DynamoDB export
script, and a locally-built calgen wheel — calgen's rendering logic is never
forked, only its data source changes.

Triggers: DynamoDB-Streams-fed debounced Lambda (near-real-time rebuilds
after admin approvals) + a fixed 4-hour schedule aligned with the iCal
aggregator cadence + on-demand via POST /admin/rebuild / MCP trigger_rebuild.
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

BUILDSPEC = {
    "version": "0.2",
    "phases": {
        "install": {
            "runtime-versions": {"python": "3.12"},
            "commands": [
                "pip install --quiet wheels/*.whl boto3",
            ],
        },
        "build": {
            "commands": [
                "cd site",
                "python ../export_dynamo_to_calgen.py --table $TABLE_NAME",
                # Deliberately no `calgen refresh` — the iCal Aggregator owns
                # fetching; the export already materialized the cache files.
                "calgen pipeline --site-dir .",
                "calgen build --site-dir .",
                'aws s3 sync build/ "s3://$SITE_BUCKET/" --delete --exclude "edit/*"',
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

        # Streams-fed debounced trigger for near-real-time rebuilds
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
            environment={"CODEBUILD_PROJECT_NAME": self.project.project_name},
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
            )
        )
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "codebuild:StartBuild",
                    "codebuild:ListBuildsForProject",
                    "codebuild:BatchGetBuilds",
                ],
                resources=[self.project.project_arn],
            )
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
