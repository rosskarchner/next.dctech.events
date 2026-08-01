"""NextCognitoClientStack — new app client on the existing shared user pool.

Imports the production pool (us-east-1_8Ay4dTt8j) read-only by literal ID and
adds only a new app client with next.dctech.events callback URLs, so logins
carry over without touching production's pool, domain, or existing client.
"""
import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

import config


class NextCognitoClientStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        user_pool = cognito.UserPool.from_user_pool_id(
            self, "SharedUserPool", config.USER_POOL_ID
        )

        self.user_pool = user_pool
        self.user_pool_client = cognito.UserPoolClient(
            self,
            "NextAppClient",
            user_pool=user_pool,
            user_pool_client_name="dctech-events-next-web",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                ),
                scopes=[
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[
                    f"{config.BASE_URL}/edit/auth/callback.html",
                    "http://localhost:5000/auth/callback",
                ],
                logout_urls=[
                    f"{config.BASE_URL}/edit/",
                    "http://localhost:5000/",
                ],
            ),
            id_token_validity=cdk.Duration.hours(1),
            access_token_validity=cdk.Duration.hours(1),
            refresh_token_validity=cdk.Duration.days(30),
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
                custom=False,
                admin_user_password=False,
            ),
            prevent_user_existence_errors=True,
        )

        cdk.CfnOutput(
            self, "NextUserPoolClientId", value=self.user_pool_client.user_pool_client_id
        )
