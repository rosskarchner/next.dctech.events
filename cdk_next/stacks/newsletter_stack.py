"""NextNewsletterStack — isolated newsletter infrastructure.

Fresh KMS HMAC key, Secrets Manager CSRF secret, SNS feedback topic, and
three Lambdas (signup/confirm router behind its own API, weekly sender on
production's cron(0 11 ? * MON *), SNS bounce handler). SES contact list /
template / configuration set are provisioned by the idempotent
scripts/setup_ses_next.py (SESv2 contact lists lack solid L2 constructs),
run after deploy with this stack's feedback topic ARN.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as apigateway,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")


class NextNewsletterStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # KMS HMAC key for signed confirmation links (HMAC_SHA_512, like prod)
        confirmation_key = kms.Key(
            self,
            "NextNewsletterConfirmationKey",
            description="HMAC key for next.dctech.events newsletter confirmation links",
            key_spec=kms.KeySpec.HMAC_512,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        csrf_secret = secretsmanager.Secret(
            self,
            "NextNewsletterCsrfSecret",
            secret_name=f"{config.PREFIX}/newsletter-csrf",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="csrf_secret",
                exclude_punctuation=True,
                password_length=48,
            ),
        )

        feedback_topic = sns.Topic(
            self,
            "NextNewsletterFeedbackTopic",
            topic_name=f"{config.PREFIX}-newsletter-feedback",
        )

        newsletter_env = {
            "FROM_EMAIL": "outbound@dctech.events",
            "REPLY_TO_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
            "CONTACT_LIST_NAME": config.NEWSLETTER_CONTACT_LIST,
            "TOPIC_NAME": config.NEWSLETTER_TOPIC,
            "TEMPLATE_NAME": f"{config.PREFIX}-newsletter",
            "CONFIGURATION_SET": config.PREFIX,
            "CONFIRMATION_KEY_ID": confirmation_key.key_id,
            "CSRF_SECRET_NAME": csrf_secret.secret_name,
            "DYNAMODB_TABLE_NAME": table.table_name,
        }

        # 1. Signup/confirm web app
        self.signup_function = lambda_.Function(
            self,
            "NextNewsletterSignup",
            function_name=f"{config.PREFIX}-newsletter-signup",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="app.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "newsletter")),
            timeout=cdk.Duration.seconds(30),
            memory_size=512,
            environment=newsletter_env,
            log_group=logs.LogGroup(
                self,
                "NextNewsletterSignupLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        confirmation_key.grant(self.signup_function, "kms:GenerateMac", "kms:VerifyMac")
        csrf_secret.grant_read(self.signup_function)

        # 2. Weekly sender — same schedule as production
        self.sender_function = lambda_.Function(
            self,
            "NextNewsletterSender",
            function_name=f"{config.PREFIX}-newsletter-sender",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="sender.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "newsletter")),
            timeout=cdk.Duration.minutes(15),
            memory_size=2048,
            environment=newsletter_env,
            log_group=logs.LogGroup(
                self,
                "NextNewsletterSenderLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_data(self.sender_function)

        # 3. Bounce/complaint handler
        bounce_function = lambda_.Function(
            self,
            "NextNewsletterBounceHandler",
            function_name=f"{config.PREFIX}-newsletter-bounce",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="bounce_handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "newsletter")),
            timeout=cdk.Duration.seconds(30),
            environment=newsletter_env,
            log_group=logs.LogGroup(
                self,
                "NextNewsletterBounceLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        bounce_function.add_event_source(event_sources.SnsEventSource(feedback_topic))

        for fn in (self.signup_function, self.sender_function, bounce_function):
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "ses:SendEmail",
                        "ses:SendTemplatedEmail",
                        "ses:CreateContact",
                        "ses:GetContact",
                        "ses:UpdateContact",
                        "ses:DeleteContact",
                        "ses:ListContacts",
                    ],
                    resources=["*"],
                )
            )

        api = apigateway.LambdaRestApi(
            self,
            "NextNewsletterApi",
            rest_api_name=f"{config.PREFIX}-newsletter",
            handler=self.signup_function,
            proxy=True,
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )

        events.Rule(
            self,
            "NextNewsletterSchedule",
            schedule=events.Schedule.expression("cron(0 11 ? * MON *)"),
            targets=[targets.LambdaFunction(self.sender_function)],
            description="Weekly next.dctech.events newsletter send (Mon 11:00 UTC)",
        )

        cdk.CfnOutput(self, "NextNewsletterApiUrl", value=api.url)
        cdk.CfnOutput(self, "NextNewsletterFeedbackTopicArn", value=feedback_topic.topic_arn)
        cdk.CfnOutput(self, "NextNewsletterKmsKeyId", value=confirmation_key.key_id)
