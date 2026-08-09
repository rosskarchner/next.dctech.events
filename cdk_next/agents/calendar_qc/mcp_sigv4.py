"""SigV4 authentication for the dctech-events MCP endpoint.

The MCP server sits behind API Gateway with an AWS_IAM authorizer, so every
request has to be SigV4-signed. `scripts/mcp_sigv4_bridge.py` does this for
desktop MCP clients by bridging stdio to signed HTTP; here we're already an
HTTP client, so it's just an httpx auth hook.

Credentials come from the ordinary boto3 chain — the AgentCore Runtime
execution role in production, whatever your shell uses when running locally.
"""
import os

import boto3
import httpx
from botocore.auth import SigV4Auth as _BotoSigV4Auth
from botocore.awsrequest import AWSRequest

SERVICE = "execute-api"

# Signing covers a fixed header set; anything httpx adds afterwards (host is
# derived from the URL by botocore, content-length, user-agent) is outside the
# signature and can vary freely.
_SIGNED_HEADERS = ("content-type", "accept")


class SigV4Auth(httpx.Auth):
    """Signs each request with SigV4 immediately before it goes out."""

    # botocore hashes the payload, so httpx must hand us the body up front
    # rather than streaming it.
    requires_request_body = True

    def __init__(self, region: str, session: boto3.Session | None = None):
        self.region = region
        session = session or boto3.Session()
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise RuntimeError(
                "No AWS credentials found. Locally, run `aws sso login` "
                "(or set AWS_PROFILE); in AgentCore, check the runtime's "
                "execution role."
            )

    def auth_flow(self, request: httpx.Request):
        # Re-freeze per request: STS/SSO credentials expire mid-run and boto3
        # refreshes them behind this call. A long QC pass outlives them.
        frozen = self._credentials.get_frozen_credentials()

        signable = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={k: v for k, v in request.headers.items()
                     if k.lower() in _SIGNED_HEADERS},
        )
        _BotoSigV4Auth(frozen, SERVICE, self.region).add_auth(signable)

        for header, value in signable.headers.items():
            request.headers[header] = value
        yield request


def mcp_transport(url: str | None = None, region: str | None = None):
    """A streamable-HTTP MCP transport factory for strands' MCPClient.

    The server runs stateless with json_response=True (see
    lambda_src/mcp/handler.py), so each POST returns one complete JSON reply
    and there is no SSE stream to hold open.
    """
    from mcp.client.streamable_http import streamablehttp_client

    url = url or os.environ["DCTECH_MCP_URL"]
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    auth = SigV4Auth(region)
    return lambda: streamablehttp_client(url, auth=auth)
