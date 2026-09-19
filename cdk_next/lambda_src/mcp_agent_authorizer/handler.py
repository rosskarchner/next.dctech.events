"""Lambda authorizer for /mcp-agent: a static bearer token check.

Claude Scheduled Tasks call this route with a long-lived shared secret
(TOKEN_SECRET_ARN) instead of a SigV4-signed AWS identity, since a
cloud-hosted scheduled agent has no AWS credentials to sign with — see
api_stack.py's McpAgentToken / NextMcpAgentTokenAuthorizer. Tool-level
scoping happens in mcp/handler.py, keyed off the 'authType' context value
returned here.
"""
import hmac
import os

import boto3

_secrets = boto3.client("secretsmanager")
_TOKEN_SECRET_ARN = os.environ["TOKEN_SECRET_ARN"]
_cached_token = None


def _expected_token() -> str:
    global _cached_token
    if _cached_token is None:
        _cached_token = _secrets.get_secret_value(
            SecretId=_TOKEN_SECRET_ARN)["SecretString"]
    return _cached_token


def _extract_token(raw: str) -> str:
    prefix = "bearer "
    return raw[len(prefix):] if raw.lower().startswith(prefix) else raw


def lambda_handler(event, context):
    token = _extract_token(event.get("authorizationToken", ""))
    allowed = bool(token) and hmac.compare_digest(token, _expected_token())
    return {
        "principalId": "scheduled-agent",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Action": "execute-api:Invoke",
                "Effect": "Allow" if allowed else "Deny",
                "Resource": event["methodArn"],
            }],
        },
        "context": {"authType": "scheduled-agent"},
    }
