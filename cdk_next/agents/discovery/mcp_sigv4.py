"""SigV4 authentication for the dctech-events MCP endpoint."""
import os

import boto3
import httpx
from botocore.auth import SigV4Auth as _BotoSigV4Auth
from botocore.awsrequest import AWSRequest

SERVICE = "execute-api"
_SIGNED_HEADERS = ("content-type", "accept")


class SigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str, session: boto3.Session | None = None):
        self.region = region
        session = session or boto3.Session()
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise RuntimeError(
                "No AWS credentials found. Locally, run `aws sso login` "
                "(or set AWS_PROFILE); in AgentCore, check the runtime's execution role."
            )

    def auth_flow(self, request: httpx.Request):
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
    from mcp.client.streamable_http import streamablehttp_client

    url = url or os.environ["DCTECH_MCP_URL"]
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    auth = SigV4Auth(region)
    return lambda: streamablehttp_client(url, auth=auth)
