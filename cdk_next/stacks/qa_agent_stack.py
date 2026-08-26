"""NextQaAgentStack — the weekly calendar quality-control agent.

A Strands agent on Bedrock AgentCore Runtime that makes two passes over each
week's newly-imported iCal events: one for duplicates and out-of-area listings
(which remove an event), then one for titles, locations and categories that
disagree with the event's own page (which correct it). It reaches the data only
through the site's own MCP server, so its fixes are byte-for-byte the fixes the
/edit UI applies.

AgentCore rather than a Lambda for two reasons: a full pass runs well past
Lambda's 15-minute ceiling, and the managed Browser tool can read the Meetup
and Eventbrite pages it checks against, which block a plain HTTP fetch. Tavily
sits in front of the browser as the cheaper first try — see the agent's
`_load_search_key`.
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

        # Tavily, for reading the event pages the polish pass compares entries
        # against and for resolving venues those pages only name. The same
        # secret the discovery agent created, referenced by name rather than
        # duplicated: it is one Tavily account, and a second key would be a
        # second thing to rotate. The `discovery/` in the path is historical.
        search_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "SearchApiKeyRef", f"{config.PREFIX}/discovery/tavily"
        )
        search_secret.grant_read(role)
        # The agent releases the Step Functions task token the Monday state
        # machine is waiting on. It has to be the agent and not the trigger
        # Lambda: the trigger returns as soon as the runtime accepts the
        # invocation, minutes before the pass is done. Not scoped to one state
        # machine ARN — a token is already an unguessable capability, and
        # scoping it would make NextQaAgentStack depend on the orchestration
        # stack that invokes it.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure",
                         "states:SendTaskHeartbeat"],
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
                "SEARCH_SECRET_ARN": search_secret.secret_arn,
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
            # No async retries. This function starts a QC pass; retrying it
            # starts a *second* pass rather than recovering the first. On
            # 2026-08-26 the default of 2 turned one dry run into three
            # concurrent passes that then starved each other of Bedrock
            # throughput until all three died on read timeouts.
            retry_attempts=0,
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

        # No schedule here. NextOrchestrationStack's Monday state machine
        # invokes this trigger, because the pass has to finish before the site
        # rebuild and the week-ahead post that depend on it — an ordering a
        # second cron expression cannot express.

        cdk.CfnOutput(self, "CalendarQcRuntimeArn",
                      value=self.runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "CalendarQcTriggerFunction",
                      value=self.trigger.function_name)
