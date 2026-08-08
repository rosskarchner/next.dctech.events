"""NextApiStack — API Lambda (backend fork) + MCP Lambda behind one REST API.

Mirrors infrastructure/lib/lambda-api-stack.ts route/authorizer layout, minus
the queue-notification/cleanup Lambdas (not part of this scope). The /mcp
resource uses an AWS_IAM authorizer (trusted agents/Lambdas only).
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as apigateway,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

import config

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")


class NextApiStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        stage_name = "prod"

        # HMAC key backing magic-link submission tokens. Its own key rather
        # than the newsletter's: sharing one would couple this stack to
        # NextNewsletterStack, and rotating either purpose's key
        # independently is worth more than one fewer resource.
        self.submit_key = kms.Key(
            self,
            "NextSubmitLinkKey",
            description="HMAC key for dctech.events magic-link event submission",
            key_spec=kms.KeySpec.HMAC_512,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            # Destroying it invalidates every outstanding submission link,
            # which is recoverable (users request a new one) but rude.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        common_env = {
            "DYNAMODB_TABLE_NAME": table.table_name,
            "COGNITO_USER_POOL_ID": config.USER_POOL_ID,
            "COGNITO_USER_POOL_CLIENT_ID": user_pool_client.user_pool_client_id,
            "COGNITO_DOMAIN": config.COGNITO_HOSTED_UI_DOMAIN,
            "CONTACT_LIST_NAME": config.NEWSLETTER_CONTACT_LIST,
            "NEWSLETTER_TOPIC": config.NEWSLETTER_TOPIC,
            "STAGE": stage_name,
            "SUBMIT_KEY_ID": self.submit_key.key_id,
            "BASE_URL": config.BASE_URL,
            "FROM_EMAIL": "outbound@dctech.events",
            "REPLY_TO_EMAIL": config.NEWSLETTER_ADMIN_EMAIL,
        }

        self.api_function = lambda_.Function(
            self,
            "NextApiFunction",
            function_name=f"{config.PREFIX}-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "api")),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment=common_env,
            log_group=logs.LogGroup(
                self,
                "NextApiFunctionLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        self.mcp_function = lambda_.Function(
            self,
            "NextMcpFunction",
            function_name=f"{config.PREFIX}-mcp",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(BUILD_DIR, "mcp")),
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
            environment=common_env,
            log_group=logs.LogGroup(
                self,
                "NextMcpFunctionLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        for fn in (self.api_function, self.mcp_function):
            table.grant_read_write_data(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "ses:ListContacts",
                        # Magic-link emails, plus the newsletter opt-in on the
                        # submission form (create/get/update contact).
                        "ses:SendEmail",
                        "ses:CreateContact",
                        "ses:GetContact",
                        "ses:UpdateContact",
                    ],
                    resources=["*"],
                )
            )
            self.submit_key.grant(fn, "kms:GenerateMac", "kms:VerifyMac")
            # trigger_rebuild / POST /api/admin/rebuild (project name wired in
            # by the site-generator stack once it exists)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["codebuild:StartBuild"],
                    resources=[
                        f"arn:aws:codebuild:{self.region}:{self.account}:project/{config.PREFIX}-site-generator"
                    ],
                )
            )

        api = apigateway.RestApi(
            self,
            "NextApi",
            rest_api_name=f"{config.PREFIX}-api",
            description="next.dctech.events API",
            deploy_options=apigateway.StageOptions(
                stage_name=stage_name,
                metrics_enabled=True,
            ),
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=[
                    "Content-Type", "Authorization", "HX-Request", "HX-Trigger",
                    "HX-Trigger-Name", "HX-Target", "HX-Current-URL",
                ],
            ),
        )

        authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "NextCognitoAuthorizer",
            cognito_user_pools=[user_pool],
            identity_source="method.request.header.Authorization",
        )
        authed = {
            "authorizer": authorizer,
            "authorization_type": apigateway.AuthorizationType.COGNITO,
        }

        integration = apigateway.LambdaIntegration(
            self.api_function, proxy=True, allow_test_invoke=False
        )

        # Public routes
        api.root.add_resource("health").add_method("GET", integration)
        api_res = api.root.add_resource("api")
        api_res.add_resource("events").add_method("GET", integration)
        api_res.add_resource("categories").add_method("GET", integration)

        # Event submission is deliberately unauthenticated at the gateway:
        # submitters authenticate with an emailed magic link that the Lambda
        # verifies itself (routes/submit.py). Attaching the Cognito authorizer
        # here would reject link-holders before Lambda ever sees the token.
        api_res.add_resource("submit-link").add_method("POST", integration)
        api_res.add_resource("submissions").add_method("POST", integration)

        # Authenticated JSON API
        api_res.add_resource("my-submissions").add_method("GET", integration, **authed)
        api_res.add_resource("admin").add_proxy(
            default_integration=integration,
            default_method_options=apigateway.MethodOptions(**authed),
            any_method=True,
        )

        # Authenticated HTMX routes
        submit = api.root.add_resource("submit")
        submit.add_method("GET", integration, **authed)
        submit.add_method("POST", integration, **authed)
        api.root.add_resource("my-submissions").add_method("GET", integration, **authed)
        admin = api.root.add_resource("admin")
        admin.add_method("ANY", integration, **authed)
        admin.add_proxy(
            default_integration=integration,
            default_method_options=apigateway.MethodOptions(**authed),
            any_method=True,
        )

        # MCP endpoint — IAM auth (trusted agents/Lambdas only, not end users)
        mcp_integration = apigateway.LambdaIntegration(
            self.mcp_function, proxy=True, allow_test_invoke=False
        )
        mcp_res = api.root.add_resource("mcp")
        mcp_res.add_method(
            "ANY", mcp_integration,
            authorization_type=apigateway.AuthorizationType.IAM,
        )
        mcp_res.add_proxy(
            default_integration=mcp_integration,
            default_method_options=apigateway.MethodOptions(
                authorization_type=apigateway.AuthorizationType.IAM
            ),
            any_method=True,
        )

        # Cognito authorizer rejections bypass Lambda entirely, so they have no
        # CORS headers; make 401/403 readable by the browser.
        api.add_gateway_response(
            "UnauthorizedGatewayResponse",
            type=apigateway.ResponseType.UNAUTHORIZED,
            response_headers={
                "Access-Control-Allow-Origin": "'*'",
                "Access-Control-Allow-Headers": "'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL'",
            },
        )
        api.add_gateway_response(
            "AccessDeniedGatewayResponse",
            type=apigateway.ResponseType.ACCESS_DENIED,
            response_headers={
                "Access-Control-Allow-Origin": "'*'",
                "Access-Control-Allow-Headers": "'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL'",
            },
        )

        self.api = api
        self.api_endpoint = api.url

        cdk.CfnOutput(self, "NextApiEndpoint", value=api.url)
        cdk.CfnOutput(self, "NextMcpUrl", value=f"{api.url}mcp")
