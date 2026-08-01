"""NextCicdStack — narrowly-scoped GitHub Actions deploy role.

Reuses the existing account-level GitHub OIDC provider (imported read-only by
ARN — OIDC providers are account singletons) but provisions a NEW role rather
than broadening production's GithubActionsDeployRole. Trusts only the
next.dctech.events repo's main branch.
"""
import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

import config

GITHUB_REPO = "rosskarchner/next.dctech.events"  # green-field repo for this codebase
OIDC_PROVIDER_ARN = (
    f"arn:aws:iam::{config.ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
)


class NextCicdStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self, "GithubOidcProvider", OIDC_PROVIDER_ARN
        )

        role = iam.Role(
            self,
            "NextGithubActionsDeployRole",
            role_name="NextGithubActionsDeployRole",
            assumed_by=iam.WebIdentityPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub": f"repo:{GITHUB_REPO}:ref:refs/heads/main"
                    },
                },
            ),
            max_session_duration=cdk.Duration.hours(1),
            description="GitHub Actions deploy role for the next.dctech.events parallel stack",
        )

        # CDK deploys happen through the CDK bootstrap roles
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{config.ACCOUNT}:role/cdk-*"],
            )
        )
        # Read stack outputs for the edit-UI templating step
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    f"arn:aws:cloudformation:{config.REGION}:{config.ACCOUNT}:stack/Next*"
                ],
            )
        )
        # Trigger-and-wait on the site generator CodeBuild project
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[
                    f"arn:aws:codebuild:{config.REGION}:{config.ACCOUNT}:project/{config.PREFIX}-site-generator"
                ],
            )
        )
        # Sync the templated edit UI + invalidate CloudFront
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[
                    "arn:aws:s3:::nexthostingstack-nextsitebucket*",
                    "arn:aws:s3:::nexthostingstack-nextsitebucket*/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[f"arn:aws:cloudfront::{config.ACCOUNT}:distribution/*"],
            )
        )
        # Idempotent SES setup script
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ses:GetContactList",
                    "ses:UpdateContactList",
                    "ses:GetEmailTemplate",
                    "ses:CreateEmailTemplate",
                    "ses:UpdateEmailTemplate",
                    "ses:GetConfigurationSet",
                    "ses:CreateConfigurationSet",
                    "ses:GetConfigurationSetEventDestinations",
                    "ses:CreateConfigurationSetEventDestination",
                    "ses:UpdateConfigurationSetEventDestination",
                ],
                resources=["*"],
            )
        )

        cdk.CfnOutput(self, "NextGithubActionsDeployRoleArn", value=role.role_arn)
