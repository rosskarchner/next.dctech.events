"""NextCognitoStack — owns the shared Cognito user pool.

The pool, its admins group, and the login.dctech.events hosted-UI domain
were created by the older TypeScript CDK app's `dctech-events-cognito`
stack. On 2026-08-02 that stack was deleted with every resource marked
RETAIN, orphaning them, and they were adopted here via `cdk import` so this
app is self-contained.

Deliberately built from L1 (Cfn*) constructs mirroring the old template
property-for-property. L2 constructs apply their own defaults, and a
mismatch on an immutable UserPool property (UsernameAttributes, Schema)
forces replacement — which would strand every existing user. Any change
here must be checked with `cdk diff` reporting no differences first.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
)
from constructs import Construct

import config

# Wildcard cert (dctech.events + *.dctech.events) owned by the old app's
# `dctech-events` stack; referenced by literal ARN, never imported.
# Set CDK_IMPORT_MODE=1 only while running `cdk import` (see module docstring).
IMPORT_MODE = os.environ.get("CDK_IMPORT_MODE") == "1"

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")

HOSTED_UI_CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:797438674243:certificate/013bf7ee-a628-4806-9ddd-ac51ef7b5391"
)


class NextCognitoStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Rejects automated sign-ups before Cognito sends a verification
        # email. Lives here rather than in NextOpsStack so the pool and the
        # trigger guarding it deploy as one unit.
        self.pre_signup_function = lambda_.Function(
            self,
            "NextPreSignUpCheck",
            function_name=f"{config.PREFIX}-pre-signup-check",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="pre_signup.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "ops")),
            timeout=cdk.Duration.seconds(5),
            environment={
                # Audit first: log verdicts without blocking anyone. Flip to
                # "true" once the logs show no legitimate sign-ups caught.
                "ENFORCE": "false",
            },
            log_group=logs.LogGroup(
                self,
                "NextPreSignUpLogGroup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        self.pre_signup_function.add_permission(
            "AllowCognitoInvoke",
            principal=iam.ServicePrincipal("cognito-idp.amazonaws.com"),
            source_arn=(f"arn:aws:cognito-idp:{self.region}:{self.account}"
                        f":userpool/{config.USER_POOL_ID}"),
        )

        self.user_pool = cognito.CfnUserPool(
            self,
            "DctechEventsUserPool",
            user_pool_name="dctech-events-users",
            username_attributes=["email"],
            auto_verified_attributes=["email"],
            # Public sign-up is closed: event submission now runs on emailed
            # magic links (see routes/submit.py), so a self-service account
            # buys a submitter nothing, while the open SignUp endpoint was
            # taking sustained bot registrations. Admins are created with
            # AdminCreateUser. Mutable property — existing accounts are
            # untouched and keep signing in.
            admin_create_user_config=cognito.CfnUserPool.AdminCreateUserConfigProperty(
                allow_admin_create_user_only=True,
            ),
            account_recovery_setting=cognito.CfnUserPool.AccountRecoverySettingProperty(
                recovery_mechanisms=[
                    cognito.CfnUserPool.RecoveryOptionProperty(
                        name="verified_email", priority=1
                    )
                ],
            ),
            email_configuration=cognito.CfnUserPool.EmailConfigurationProperty(
                email_sending_account="DEVELOPER",
                from_="DC Tech Events <noreply@dctech.events>",
                reply_to_email_address="support@dctech.events",
                source_arn=f"arn:aws:ses:{self.region}:{self.account}:identity/{config.ZONE_NAME}",
            ),
            email_verification_message="Welcome to DC Tech Events! Your verification code is {####}.",
            email_verification_subject="Verify your email for DC Tech Events",
            sms_verification_message="The verification code to your new account is {####}",
            verification_message_template=cognito.CfnUserPool.VerificationMessageTemplateProperty(
                default_email_option="CONFIRM_WITH_CODE",
                email_message="Welcome to DC Tech Events! Your verification code is {####}.",
                email_subject="Verify your email for DC Tech Events",
                sms_message="The verification code to your new account is {####}",
            ),
            enabled_mfas=["SOFTWARE_TOKEN_MFA"],
            mfa_configuration="OPTIONAL",
            policies=cognito.CfnUserPool.PoliciesProperty(
                password_policy=cognito.CfnUserPool.PasswordPolicyProperty(
                    minimum_length=8,
                    require_lowercase=True,
                    require_numbers=True,
                    require_symbols=False,
                    require_uppercase=True,
                    temporary_password_validity_days=7,
                ),
            ),
            schema=[
                cognito.CfnUserPool.SchemaAttributeProperty(
                    name="email", required=True, mutable=False),
                cognito.CfnUserPool.SchemaAttributeProperty(
                    name="name", required=False, mutable=True),
                cognito.CfnUserPool.SchemaAttributeProperty(
                    name="submissions_count", attribute_data_type="Number", mutable=True,
                    number_attribute_constraints=cognito.CfnUserPool.NumberAttributeConstraintsProperty(
                        max_value="10000", min_value="0"),
                ),
                cognito.CfnUserPool.SchemaAttributeProperty(
                    name="approved_count", attribute_data_type="Number", mutable=True,
                    number_attribute_constraints=cognito.CfnUserPool.NumberAttributeConstraintsProperty(
                        max_value="10000", min_value="0"),
                ),
            ],
            # CloudFormation forbids adding tags during an import; they are
            # applied by the ordinary deploy that follows it.
            lambda_config=cognito.CfnUserPool.LambdaConfigProperty(
                pre_sign_up=self.pre_signup_function.function_arn,
            ),
            user_pool_tags=None if IMPORT_MODE else {
                "component": "authentication",
                "managedBy": "CDK",
                "project": "dctech-events",
            },
        )
        # 66 real accounts live here.
        self.user_pool.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        self.admin_group = cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.ref,
            group_name="admins",
            description="Administrators who can approve/reject submissions and edit content",
            precedence=1,
        )
        self.admin_group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        self.hosted_ui_domain = cognito.CfnUserPoolDomain(
            self,
            "DctechEventsCustomDomain",
            user_pool_id=self.user_pool.ref,
            domain=config.COGNITO_HOSTED_UI_DOMAIN,
            custom_domain_config=cognito.CfnUserPoolDomain.CustomDomainConfigTypeProperty(
                certificate_arn=HOSTED_UI_CERTIFICATE_ARN,
            ),
        )
        self.hosted_ui_domain.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        # Route53 RecordSets cannot be imported by CloudFormation, so this
        # one is adopted by declaring it: CFN creates record sets with UPSERT,
        # and the alias target is unchanged, so the live record is rewritten
        # with identical content rather than disturbed.
        self.hosted_ui_record = route53.CfnRecordSet(
            self,
            "CognitoCustomDomainARecord",
            hosted_zone_id=config.HOSTED_ZONE_ID,
            name=f"{config.COGNITO_HOSTED_UI_DOMAIN}.",
            type="A",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=self.hosted_ui_domain.attr_cloud_front_distribution,
                # CloudFront's fixed global alias zone ID.
                hosted_zone_id="Z2FDTNDATAQYW2",
            ),
        )
        self.hosted_ui_record.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        cdk.CfnOutput(self, "NextUserPoolId", value=self.user_pool.ref)
        cdk.CfnOutput(self, "NextHostedUiDomain",
                      value=f"https://{config.COGNITO_HOSTED_UI_DOMAIN}")
