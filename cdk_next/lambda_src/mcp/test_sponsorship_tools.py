"""Tests for the MCP sponsorship tools: thin wrappers, so these check the
wiring — errors surface as ValueError, the reviewer is identifiable, and
none of them are reachable from the bearer-token /mcp-agent route.

Run: python -m pytest test_sponsorship_tools.py
"""
import os
import sys
from decimal import Decimal

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import db  # noqa: E402
import server  # noqa: E402
import sponsorships as sp  # noqa: E402
from test_sponsorships import FakeTable  # noqa: E402

TOOLS = ("list_sponsorships", "get_sponsorship", "approve_sponsorship",
         "reject_sponsorship", "update_sponsorship_copy", "book_sponsorship",
         "mint_sponsor_coupons", "list_sponsor_coupons", "revoke_sponsor_coupon")


@pytest.fixture(autouse=True)
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(sp, "PRICE_USD", Decimal("150"))
    monkeypatch.setattr(sp, "_send", lambda *a, **k: None)
    return fake


def test_sponsorship_tools_are_not_on_the_scheduled_agent_route():
    # Loaded by path: on this test run's sys.path, `handler` is the api's.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_handler", os.path.join(os.path.dirname(__file__), "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    assert not set(TOOLS) & handler.SCHEDULED_AGENT_TOOLS


def test_book_then_approve_edit():
    server.book_sponsorship("2026-W50", "Acme", "Hiring.", "https://acme.example",
                            approve=False)
    booking = server.approve_sponsorship("2026-W50", message="Hiring now.")
    assert booking["status"] == "approved"
    assert booking["message"] == "Hiring now."
    assert booking["reviewed_by"] == server.MCP_REVIEWER


def test_errors_surface_as_value_error():
    with pytest.raises(ValueError, match="No booking"):
        server.get_sponsorship("2026-W50")
    with pytest.raises(ValueError, match="No booking"):
        server.reject_sponsorship("2026-W50")


def test_mint_list_revoke():
    codes = server.mint_sponsor_coupons(2, note="comp")
    assert len(server.list_sponsor_coupons()) == 2
    assert server.revoke_sponsor_coupon(codes[0]["code"])["revoked"]
