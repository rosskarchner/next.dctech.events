"""Tests for the MCP highlight tools: thin wrappers, so these check the
wiring — errors surface as ValueError, the reviewer is identifiable, and
none of them are reachable from the bearer-token /mcp-agent route.

Run: python -m pytest test_highlight_tools.py
"""
import os
import sys
from decimal import Decimal

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import db  # noqa: E402
import highlights as hl  # noqa: E402
import server  # noqa: E402
from test_highlights import EVENTS  # noqa: E402
from test_sponsorships import FakeTable  # noqa: E402

TOOLS = ("list_highlights", "highlight_event", "revoke_highlight")


@pytest.fixture(autouse=True)
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(hl, "PRICE_USD", Decimal("10"))
    monkeypatch.setattr(hl, "_send", lambda *a, **k: None)
    monkeypatch.setattr(hl, "_events_loader", lambda: [dict(e) for e in EVENTS])
    return fake


def test_highlight_tools_are_registered():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert set(TOOLS) <= names


def test_highlight_tools_are_not_on_the_scheduled_agent_route():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_handler", os.path.join(os.path.dirname(__file__), "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    assert not set(TOOLS) & handler.SCHEDULED_AGENT_TOOLS


def test_highlight_list_revoke():
    item = server.highlight_event("abc123", note="worth it")
    assert item["kind"] == "editor" and item["created_by"] == server.MCP_REVIEWER
    assert [h["target_key"] for h in server.list_highlights("active")] == ["g-abc123"]
    revoked = server.revoke_highlight("g-abc123", "test")
    assert revoked["status"] == "revoked"
    assert revoked["revoked_by"] == server.MCP_REVIEWER


def test_errors_surface_as_value_error():
    with pytest.raises(ValueError, match="in-person"):
        server.highlight_event("virt01")
    with pytest.raises(ValueError, match="No highlight"):
        server.revoke_highlight("g-nothing")
