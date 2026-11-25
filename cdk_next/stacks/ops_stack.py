"""NextOpsStack — scheduled maintenance jobs.

Lost when the old TypeScript app's dctech-events-api stack was deleted on
2026-08-02 and re-created here. Kept out of NextCognitoStack so that
carefully-imported stack stays untouched. The weekly prune of unconfirmed
Cognito signups was removed on 2026-11-25 as no longer needed.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
)
from constructs import Construct

import config
from constructs_lib.python_lambda import python_lambda

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

        # ── Daily: moderation queue summary ─────────────────────────
        self.queue_notification_function = python_lambda(
            self,
            "NextQueueNotification",
            function_name=f"{config.PREFIX}-queue-notification",
            handler="queue_notification.lambda_handler",
            code_dir="ops",
            build_dir=BUILD_DIR,
            log_group_id="NextQueueNotificationLogGroup",
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE_NAME": table.table_name,
                "ADMIN_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
                "SENDER_EMAIL": f"noreply@{config.ZONE_NAME}",
                "QUEUE_URL": f"{config.BASE_URL}/edit/queue.html",
            },
        )
        table.grant_read_data(self.queue_notification_function)
        self.queue_notification_function.add_to_role_policy(
            iam.PolicyStatement(
                # dctech.events is verified at the domain level (SES DKIM),
                # not per-address — matches the scoping already applied to
                # every other ses:SendEmail grant in this app this session.
                actions=["ses:SendEmail"],
                resources=[
                    f"arn:aws:ses:{self.region}:{self.account}:identity/dctech.events"
                ],
            )
        )
        events.Rule(
            self,
            "NextQueueNotificationSchedule",
            # 8:30 AM EST, matching the old app's cadence.
            schedule=events.Schedule.expression("cron(30 13 * * ? *)"),
            targets=[targets.LambdaFunction(self.queue_notification_function)],
            description="Daily moderation queue summary email (13:30 UTC)",
        )

        cdk.CfnOutput(self, "NextQueueNotificationFunction",
                      value=self.queue_notification_function.function_name)
