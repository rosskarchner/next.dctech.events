"""Lambda entry point for the MCP server.

Mangum runs the ASGI lifespan cycle per event, but StreamableHTTPSessionManager
only allows .run() once per instance — so instead of mounting FastMCP's
streamable_http_app (whose lifespan starts the manager), we create a fresh
session manager per request. Stateless mode makes this cheap and correct;
Lambda invocations in one container are serialized anyway.
"""
import os

from mangum import Mangum
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from server import mcp

_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


async def asgi(scope, receive, send):
    if scope['type'] == 'lifespan':
        return
    manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=None,
        json_response=True,
        stateless=True,
        security_settings=_SECURITY,
    )
    async with manager.run():
        await manager.handle_request(scope, receive, send)


lambda_handler = Mangum(asgi, lifespan='off',
                        api_gateway_base_path=f"/{os.environ.get('STAGE', 'prod')}")
