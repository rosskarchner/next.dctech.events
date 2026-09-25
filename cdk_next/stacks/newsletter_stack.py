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

        # Subscriber category/region preferences links (magic_link.py's
        # purpose='prefs'). Its own key, not confirmation_key or
        # NextApiStack's submit_key — matching the standing rule that each
        # purpose's key is independent and separately rotatable. Owned here
        # (not NextApiStack) because this stack's sender_function is what
        # actually generates one per subscriber on every send; NextApiStack
        # gets a reference to this same construct (self.prefs_key below,
        # passed into NextApiStack's constructor in app.py) since its
        # /api/preferences route is what verifies them — the one purpose
        # that legitimately spans both stacks, wired the same way both
        # already share the DynamoDB table.
        self.prefs_key = kms.Key(
            self,
            "NextPreferencesLinkKey",
            description="HMAC key for next.dctech.events subscriber preferences links",
            key_spec=kms.KeySpec.HMAC_512,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            # Destroying it invalidates every outstanding preferences link,
            # which is recoverable (a subscriber requests a new one) but
            # rude — matching submit_key's own reasoning in NextApiStack.
            removal_policy=cdk.RemovalPolicy.RETAIN,
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
            "PREFS_KEY_ID": self.prefs_key.key_id,
            "CSRF_SECRET_NAME": csrf_secret.secret_name,
            "DYNAMODB_TABLE_NAME": table.table_name,
            # Served same-origin through the CloudFront /newsletter* behavior,
            # so forms, redirects, and emailed confirmation links all use the
            # public site URL rather than the raw execute-api hostname.
            "BASE_URL": f"{config.BASE_URL}/newsletter",
            "PATH_PREFIX": "/newsletter",
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
        # Writes subscriber category/region preferences (db.put_subscriber_preferences)
        # on confirm — this Lambda touched no DynamoDB at all before that.
        table.grant_read_write_data(self.signup_function)

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
            # Last measured run: 153 MB max used at 60-ish subscribers
            # (CloudWatch REPORT line) — 512 MB keeps ~3x headroom without
            # paying for the 2048 MB this was originally set to.
            memory_size=512,
            environment={
                **newsletter_env,
                # Unlike the signup app's own confirm links, the prefs
                # link this function's magic_link.build_link() embeds
                # points at /edit/preferences.html on the public site
                # root, not anything under /newsletter -- reusing
                # newsletter_env's BASE_URL here 404s the emailed link.
                "BASE_URL": config.BASE_URL,
            },
            log_group=logs.LogGroup(
                self,
                "NextNewsletterSenderLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_data(self.sender_function)
        # Generates (never verifies) a purpose='prefs' magic_link token per
        # subscriber, embedded in every send — see routes/preferences.py in
        # NextApiStack for the other half (verification).
        self.prefs_key.grant(self.sender_function, "kms:GenerateMac")
        # Defense in depth beyond the IAM grant above, matching submit_key's
        # own pattern in NextApiStack: an explicit statement on the key's
        # own resource policy naming this role, so a wildcard IAM policy
        # elsewhere still isn't enough on its own to use this key. Only for
        # sender_function, not api_function too: this key is a
        # NextNewsletterStack resource, so naming NextApiStack's role here
        # would embed NextApiStack's role ARN into a NextNewsletterStack
        # resource, creating a cyclic stack dependency the other direction
        # (`cdk synth` refuses it) — api_function relies on the plain
        # grant() in api_stack.py alone, which is sufficient on its own
        # since a CDK-created key's default policy already trusts account
        # identities.
        self.prefs_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="RestrictMacGenerationToNewsletterSenderRole",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(self.sender_function.role.role_arn)],
                actions=["kms:GenerateMac"],
                resources=["*"],
            )
        )

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
                    # dctech.events is verified at the domain level (SES
                    # DKIM), not per-address, so that's the identity ARN that
                    # actually gates sending regardless of which address
                    # under it FROM_EMAIL uses.
                    actions=["ses:SendEmail", "ses:SendTemplatedEmail"],
                    resources=[
                        f"arn:aws:ses:{config.REGION}:{config.ACCOUNT}:identity/dctech.events"
                    ],
                )
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    # SendTemplatedEmail with ListManagementOptions (used to
                    # attach a contact list for unsubscribe tracking) checks
                    # IAM on the contact-list ARN in addition to the identity
                    # ARN above -- Send* has to be granted here too, or SES
                    # AccessDenies every send that carries ListManagementOptions
                    # (next-dctech-events: broke the 2026-09-07 newsletter run,
                    # 0/67 sent, after the grants here were scoped down from "*").
                    actions=[
                        "ses:SendEmail",
                        "ses:SendTemplatedEmail",
                        "ses:CreateContact",
                        "ses:GetContact",
                        "ses:UpdateContact",
                        "ses:DeleteContact",
                        "ses:ListContacts",
                    ],
                    resources=[
                        f"arn:aws:ses:{config.REGION}:{config.ACCOUNT}:"
                        f"contact-list/{config.NEWSLETTER_CONTACT_LIST}"
                    ],
                )
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    # SendTemplatedEmail also checks IAM on the template
                    # ARN itself, separately from the identity and
                    # contact-list ARNs above -- SES authorizes every
                    # resource named in the call, not just one of them.
                    actions=["ses:SendTemplatedEmail"],
                    resources=[
                        f"arn:aws:ses:{config.REGION}:{config.ACCOUNT}:"
                        f"template/{config.NEWSLETTER_TEMPLATE}"
                    ],
                )
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    # ...and again on the configuration-set ARN (sender.py
                    # passes ConfigurationSetName=config.PREFIX) -- same
                    # per-resource IAM check, fourth resource in the call.
                    actions=["ses:SendTemplatedEmail"],
                    resources=[
                        f"arn:aws:ses:{config.REGION}:{config.ACCOUNT}:"
                        f"configuration-set/{config.PREFIX}"
                    ],
                )
            )

        self.api = apigateway.LambdaRestApi(
            self,
            "NextNewsletterApi",
            rest_api_name=f"{config.PREFIX}-newsletter",
            handler=self.signup_function,
            proxy=True,
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )
        api = self.api

        events.Rule(
            self,
            "NextNewsletterSchedule",
            # Disabled: the send is the last step of NextOrchestrationStack's
            # Monday state machine, so it goes out only once the QC pass has
            # finished and the week-ahead post it links is live. Kept as a
            # disabled rule rather than deleted so re-enabling it is a
            # one-line fallback if the state machine is ever taken out.
            enabled=False,
            schedule=events.Schedule.expression("cron(0 11 ? * MON *)"),
            targets=[targets.LambdaFunction(self.sender_function)],
            description="Weekly next.dctech.events newsletter send (Mon 11:00 UTC)",
        )

        cdk.CfnOutput(self, "NextNewsletterApiUrl", value=api.url)
        cdk.CfnOutput(self, "NextNewsletterFeedbackTopicArn", value=feedback_topic.topic_arn)
        cdk.CfnOutput(self, "NextNewsletterKmsKeyId", value=confirmation_key.key_id)
        cdk.CfnOutput(self, "NextPreferencesLinkKeyId", value=self.prefs_key.key_id)
