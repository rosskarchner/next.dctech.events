"""NextDynamoDBStack — new isolated single-table store for the parallel stack.

Mirrors infrastructure/lib/dynamodb-stack.ts (GSI1–GSI4) with additions:
GSI5 for QA/Discovery review queuing, a `ttl` attribute for self-expiring
ICAL# cache items, and RemovalPolicy.DESTROY (parallel/dev environment —
flip to RETAIN once real subscribers/users depend on it).
"""
import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

import config


class NextDynamoDBStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = dynamodb.Table(
            self,
            "NextEventsTable",
            table_name=config.TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        for i in range(1, 6):
            self.table.add_global_secondary_index(
                index_name=f"GSI{i}",
                partition_key=dynamodb.Attribute(
                    name=f"GSI{i}PK", type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(
                    name=f"GSI{i}SK", type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.ALL,
            )

        cdk.CfnOutput(self, "NextTableName", value=self.table.table_name)
        cdk.CfnOutput(self, "NextTableArn", value=self.table.table_arn)
        cdk.CfnOutput(self, "NextTableStreamArn", value=self.table.table_stream_arn)
