"""NextDiscoveryAgentStack — the weekly event discovery agent.

A Strands agent on Bedrock AgentCore Runtime that searches the web and scans a
curated source list for DC-area tech events and groups not yet on the
calendar, and proposes them into the ordinary moderation queue.

AgentCore rather than a Lambda for the same two reasons as the QC agent: a
full pass runs well past Lambda's 15-minute ceiling, and the managed Browser
tool can read the Meetup and Eventbrite pages that block a plain HTTP fetch.
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
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")

# AgentRuntimeName is validated against ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no
# hyphens, so config.PREFIX ("dctech-events-next") cannot be used here.
RUNTIME_NAME = "dctechEventsDiscovery"
MODEL = "us.anthropic.claude-sonnet-5"


class NextDiscoveryAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        mcp_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        asset = s3_assets.Asset(
            self, "DiscoveryAsset",
            path=os.path.join(BUILD_DIR, "discovery"),
        )

        search_secret = secretsmanager.Secret(
            self,
            "DiscoverySearchApiKey",
            secret_name=f"{config.PREFIX}/discovery/tavily",
            description="Tavily API key for the discovery agent's web search",
        )

        role = iam.Role(
            self,
            "DiscoveryRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role for the discovery agent runtime",
        )
        asset.grant_read(role)
        search_secret.grant_read(role)

        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["execute-api:Invoke"],
                resources=[self.format_arn(
                    service="execute-api", resource="*", resource_name="*/*/mcp*",
                )],
            )
        )
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
        role.add_to_policy(iam.PolicyStatement(actions=["ses:SendEmail"], resources=["*"]))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream",
                         "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=["arn:aws:logs:*:*:*"],
            )
        )

        self.runtime = agentcore.CfnRuntime(
            self,
            "DiscoveryRuntime",
            agent_runtime_name=RUNTIME_NAME,
            description="Weekly event discovery agent for dctech.events",
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
                "DISCOVERY_MODEL": MODEL,
                "SEARCH_SECRET_ARN": search_secret.secret_arn,
                "ADMIN_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
                "FROM_EMAIL": config.NEWSLETTER_FROM_EMAIL,
            },
        )

        self.trigger = lambda_.Function(
            self,
            "DiscoveryTrigger",
            function_name=f"{config.PREFIX}-discovery-trigger",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "discovery_trigger")),
            timeout=cdk.Duration.minutes(3),
            memory_size=256,
            environment={"AGENT_RUNTIME_ARN": self.runtime.attr_agent_runtime_arn},
            log_group=logs.LogGroup(
                self,
                "DiscoveryTriggerLogGroup",
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
            "DiscoverySchedule",
            schedule=events.Schedule.cron(minute="0", hour="9", week_day="THU"),
            targets=[targets.LambdaFunction(self.trigger)],
            description="Weekly event discovery pass",
            # Disabled 2026-08-28 pending review of the runaway-cost incident
            # in next-stack-bedrock-cost-incident memory. Flip back to True
            # (the implicit default) to re-enable.
            enabled=False,
        )

        cdk.CfnOutput(self, "DiscoveryRuntimeArn",
                      value=self.runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "DiscoveryTriggerFunction",
                      value=self.trigger.function_name)
