"""NextQaAgentStack — the weekly calendar quality-control agent.

A Strands agent on Bedrock AgentCore Runtime that reviews newly-imported iCal
events for duplicates and out-of-area listings. It reaches the data only
through the site's own MCP server, so its fixes are byte-for-byte the fixes the
/edit UI applies.

AgentCore rather than a Lambda for two reasons: a full pass runs well past
Lambda's 15-minute ceiling, and the managed Browser tool can read the Meetup
and Eventbrite pages it checks locations against, which block a plain HTTP
fetch.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")

# AgentRuntimeName is validated against ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no
# hyphens, so config.PREFIX ("dctech-events-next") cannot be used here.
RUNTIME_NAME = "dctechEventsCalendarQc"

# Cross-region inference profile. Everything this agent does removes an event
# from the site, so it all runs on the stronger model. A cheaper second pass
# for venues/titles/categories was tried and dropped — see the agent's module
# docstring.
TRIAGE_MODEL = "us.anthropic.claude-sonnet-5"


class NextQaAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        mcp_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Agent code ───────────────────────────────────────────────
        # Built for aarch64 by build_lambdas.sh: AgentCore Runtime is
        # Graviton-only, unlike every Lambda in this app.
        asset = s3_assets.Asset(
            self, "CalendarQcAsset",
            path=os.path.join(BUILD_DIR, "calendar_qc"),
        )

        role = iam.Role(
            self,
            "CalendarQcRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role for the calendar QC agent runtime",
        )
        asset.grant_read(role)

        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],  # inference profiles fan out across regions
            )
        )
        # The agent reads and writes events only through the MCP API, which is
        # behind an AWS_IAM authorizer — this is what its SigV4 signing buys.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["execute-api:Invoke"],
                resources=[self.format_arn(
                    service="execute-api", resource="*", resource_name="*/*/mcp*",
                )],
            )
        )
        # Managed Browser sessions for reading event pages that block plain
        # HTTP fetches.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:GetBrowserSession",
                    "bedrock-agentcore:ListBrowserSessions",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream",
                         "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=["arn:aws:logs:*:*:*"],
            )
        )

        self.runtime = agentcore.CfnRuntime(
            self,
            "CalendarQcRuntime",
            agent_runtime_name=RUNTIME_NAME,
            description="Weekly calendar quality-control agent for dctech.events",
            role_arn=role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            protocol_configuration="HTTP",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(
                            bucket=asset.s3_bucket_name,
                            prefix=asset.s3_object_key,
                        ),
                    ),
                    entry_point=["main.py"],
                    runtime="PYTHON_3_12",
                ),
            ),
            environment_variables={
                "DCTECH_MCP_URL": mcp_url,
                "QC_TRIAGE_MODEL": TRIAGE_MODEL,
                "ADMIN_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
                "FROM_EMAIL": config.NEWSLETTER_FROM_EMAIL,
            },
        )

        # ── Weekly trigger ───────────────────────────────────────────
        # EventBridge can't call InvokeAgentRuntime (data-plane), so a thin
        # Lambda bridges the schedule and mints the revertible run id.
        self.trigger = lambda_.Function(
            self,
            "CalendarQcTrigger",
            function_name=f"{config.PREFIX}-qa-trigger",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "qa_trigger")),
            timeout=cdk.Duration.minutes(1),
            memory_size=256,
            environment={"AGENT_RUNTIME_ARN": self.runtime.attr_agent_runtime_arn},
            log_group=logs.LogGroup(
                self,
                "CalendarQcTriggerLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        self.trigger.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[self.runtime.attr_agent_runtime_arn,
                           f"{self.runtime.attr_agent_runtime_arn}/*"],
            )
        )

        events.Rule(
            self,
            "CalendarQcSchedule",
            # Monday early morning: after the weekend's feeds have imported,
            # before anyone is looking at the site.
            schedule=events.Schedule.cron(minute="0", hour="9", week_day="MON"),
            targets=[targets.LambdaFunction(self.trigger)],
            description="Weekly calendar quality-control pass",
        )

        cdk.CfnOutput(self, "CalendarQcRuntimeArn",
                      value=self.runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "CalendarQcTriggerFunction",
                      value=self.trigger.function_name)
