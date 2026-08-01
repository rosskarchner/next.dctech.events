"""NextDiscoveryAgentStack — scaffold only (stub handler, weekly schedule).

Provisioned-but-unused Bedrock permissions (a web-search integration comes
with the follow-up real implementation, likely built on the Strands-based
agent in ~/projects/calendar-agents). Mutates nothing.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")


class NextDiscoveryAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.function = lambda_.Function(
            self,
            "NextDiscoveryAgent",
            function_name=f"{config.PREFIX}-discovery-agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "discovery_agent")),
            timeout=cdk.Duration.minutes(15),
            memory_size=512,
            environment={"DYNAMODB_TABLE_NAME": table.table_name},
            log_group=logs.LogGroup(
                self,
                "NextDiscoveryAgentLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(self.function)
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            )
        )

        events.Rule(
            self,
            "NextDiscoveryAgentSchedule",
            schedule=events.Schedule.rate(cdk.Duration.days(7)),
            targets=[targets.LambdaFunction(self.function)],
            description="Weekly discovery agent pass for new-event candidates (stub)",
        )

        cdk.CfnOutput(self, "NextDiscoveryAgentFunction", value=self.function.function_name)
