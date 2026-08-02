"""NextOpsStack — scheduled maintenance jobs.

Both were lost when the old TypeScript app's dctech-events-api stack was
deleted on 2026-08-02 and are re-created here. Kept out of NextCognitoStack
so that carefully-imported stack stays untouched.
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


class NextOpsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_defaults = dict(
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── Weekly: prune unconfirmed Cognito signups ────────────────
        self.cleanup_function = lambda_.Function(
            self,
            "NextCleanupUnconfirmedUsers",
            function_name=f"{config.PREFIX}-cleanup-unconfirmed-users",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="cleanup_unconfirmed_users.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "ops")),
            timeout=cdk.Duration.minutes(5),
            environment={
                "COGNITO_USER_POOL_ID": config.USER_POOL_ID,
                # The pool is taking sustained signup spam (~50 unconfirmed
                # bot registrations a week as of 2026-08-02), so the runaway
                # guard has to sit well above that or it blocks the cleanup
                # it exists to protect. The UNCONFIRMED status filter is the
                # real safeguard for confirmed accounts.
                "MAX_DELETIONS": "1000",
            },
            log_group=logs.LogGroup(self, "NextCleanupLogGroup", **log_defaults),
        )
        self.cleanup_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:ListUsers", "cognito-idp:AdminDeleteUser"],
                resources=[
                    f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/{config.USER_POOL_ID}"
                ],
            )
        )
        events.Rule(
            self,
            "NextCleanupSchedule",
            schedule=events.Schedule.expression("cron(0 2 ? * SUN *)"),
            targets=[targets.LambdaFunction(self.cleanup_function)],
            description="Weekly prune of unconfirmed Cognito signups (Sun 02:00 UTC)",
        )

        # ── Daily: moderation queue summary ─────────────────────────
        self.queue_notification_function = lambda_.Function(
            self,
            "NextQueueNotification",
            function_name=f"{config.PREFIX}-queue-notification",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="queue_notification.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "ops")),
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
                "ADMIN_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
                "SENDER_EMAIL": f"noreply@{config.ZONE_NAME}",
                "QUEUE_URL": f"{config.BASE_URL}/edit/queue.html",
            },
            log_group=logs.LogGroup(self, "NextQueueNotificationLogGroup", **log_defaults),
        )
        table.grant_read_data(self.queue_notification_function)
        self.queue_notification_function.add_to_role_policy(
            iam.PolicyStatement(actions=["ses:SendEmail"], resources=["*"])
        )
        events.Rule(
            self,
            "NextQueueNotificationSchedule",
            # 8:30 AM EST, matching the old app's cadence.
            schedule=events.Schedule.expression("cron(30 13 * * ? *)"),
            targets=[targets.LambdaFunction(self.queue_notification_function)],
            description="Daily moderation queue summary email (13:30 UTC)",
        )

        cdk.CfnOutput(self, "NextCleanupFunction",
                      value=self.cleanup_function.function_name)
        cdk.CfnOutput(self, "NextQueueNotificationFunction",
                      value=self.queue_notification_function.function_name)
