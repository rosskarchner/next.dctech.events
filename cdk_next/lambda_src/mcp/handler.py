"""Lambda entry point for the MCP server.

Mangum runs the ASGI lifespan cycle per event, but StreamableHTTPSessionManager
only allows .run() once per instance — so instead of mounting FastMCP's
streamable_http_app (whose lifespan starts the manager), we create a fresh
session manager per request. Stateless mode makes this cheap and correct;
Lambda invocations in one container are serialized anyway.

Two routes share this one Lambda: /mcp (AWS_IAM, trusted agents/Lambdas —
the full tool surface) and /mcp-agent (bearer token via
NextMcpAgentTokenAuthorizer, for Claude Scheduled Tasks — restricted to
SCHEDULED_AGENT_TOOLS below). The authorizer stamps
requestContext.authorizer.authType='scheduled-agent' on the second route;
IAM-authorized requests carry no such context and get the full surface.
"""
import json
import os

from mangum import Mangum
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from server import mcp

_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Deliberately excludes anything destructive or trust-changing (delete_*,
# add_group, set_group_active, trigger_rebuild, submission/correction
# moderation, trust/untrust_submitter, revert_qa_run) — a leaked long-lived
# token should still not be able to publish, delete, or grant trust on its
# own. Rebuilds already happen on their own via the DynamoDB stream trigger
# once set_overlay writes land, so this route has no need of trigger_rebuild.
SCHEDULED_AGENT_TOOLS = frozenset({
    "list_groups", "list_categories",
    "get_events", "get_event", "get_overlay",
    "list_pending_qa", "resolve_qa_review", "set_overlay",
    "propose_event", "propose_group", "list_discovery_proposals",
    "verify_ical_feed",
})


def _is_scheduled_agent(scope) -> bool:
    event = scope.get("aws.event") or {}
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    return authorizer.get("authType") == "scheduled-agent"


async def _buffer_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return body


def _replay(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _disallowed_tool(body: bytes) -> str | None:
    """None if every tools/call in the request is allowed; otherwise the
    first disallowed tool name. Malformed JSON is let through — the MCP
    server itself is the right place to reject that with a proper error."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "tools/call":
            name = (message.get("params") or {}).get("name")
            if name not in SCHEDULED_AGENT_TOOLS:
                return name or "?"
    return None


async def _send_json_rpc_error(send, tool_name: str):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -32001,
            "message": f"Tool {tool_name!r} is not available on this route",
        },
    }).encode()
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})


async def asgi(scope, receive, send):
    if scope['type'] == 'lifespan':
        return

    if _is_scheduled_agent(scope):
        body = await _buffer_body(receive)
        denied = _disallowed_tool(body)
        if denied is not None:
            await _send_json_rpc_error(send, denied)
            return
        receive = _replay(body)

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
