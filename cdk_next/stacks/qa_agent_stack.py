"""NextQaAgentStack — scaffold only (stub handler, daily schedule).

Provisioned-but-unused Bedrock permissions for the follow-up LLM work.
The handler logs what it would do and mutates nothing.
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


class NextQaAgentStack(cdk.Stack):
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
            "NextQaAgent",
            function_name=f"{config.PREFIX}-qa-agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "qa_agent")),
            timeout=cdk.Duration.minutes(15),
            memory_size=512,
            environment={"DYNAMODB_TABLE_NAME": table.table_name},
            log_group=logs.LogGroup(
                self,
                "NextQaAgentLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(self.function)
        # Provisioned but unused until the real LLM orchestration lands
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            )
        )

        events.Rule(
            self,
            "NextQaAgentSchedule",
            schedule=events.Schedule.rate(cdk.Duration.days(1)),
            targets=[targets.LambdaFunction(self.function)],
            description="Daily QA agent pass over pending_qa events (stub)",
        )

        cdk.CfnOutput(self, "NextQaAgentFunction", value=self.function.function_name)
