"""Route-level tests for highlights: dispatch, the public quote and checkout,
rate limits, IPN routing on the shared endpoint, and admin-only access.

Run: python -m pytest test_highlight_routes.py
"""
import base64
import os
from decimal import Decimal
from urllib.parse import urlencode

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402
import handler  # noqa: E402
import highlights as hl  # noqa: E402
import sponsorships as sp  # noqa: E402
from test_highlights import EVENTS  # noqa: E402
from test_sponsorship_routes import ADMIN, USER, _call  # noqa: E402
from test_sponsorships import FakeTable  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(hl, "PRICE_USD", Decimal("10"))
    monkeypatch.setattr(hl, "_send", lambda *a, **k: None)
    monkeypatch.setattr(sp, "_send", lambda *a, **k: None)
    monkeypatch.setattr(hl, "_events_loader", lambda: [dict(e) for e in EVENTS])
    return fake


CHECKOUT = {"guid": "abc123", "contact_email": "pat@example.com"}


def test_quote_is_public():
    status, body = _call("GET", "/api/highlights/quote", query={"guid": "abc123"})
    assert status == 200
    assert body["eligible"] and body["target_key"] == "g-abc123"


def test_quote_unknown_event_is_404():
    status, _ = _call("GET", "/api/highlights/quote", query={"guid": "zzz"})
    assert status == 404


def test_checkout_returns_paypal_form_with_shared_notify_url():
    status, body = _call("POST", "/api/highlights", CHECKOUT)
    assert status == 201
    assert body["paypal_url"] == "https://www.paypal.com/cgi-bin/webscr"
    assert body["paypal_fields"]["notify_url"] == \
        "https://api.example/prod/api/paypal/ipn"
    assert body["paypal_fields"]["custom"].startswith("highlight|g-abc123|")


def test_double_checkout_is_409():
    _call("POST", "/api/highlights", CHECKOUT)
    status, _ = _call("POST", "/api/highlights",
                      {**CHECKOUT, "contact_email": "b@example.com"})
    assert status == 409


def test_checkout_is_rate_limited(monkeypatch):
    monkeypatch.setattr(handler.highlights, "check_and_record_write",
                        lambda who, action, **kw: (False, 60))
    status, _ = _call("POST", "/api/highlights", CHECKOUT)
    assert status == 429


def test_holds_have_their_own_smaller_budget():
    guids = ["abc123", "far999", "rec777"]
    for i in range(hl.HOLDS_PER_IP_PER_HOUR):
        guid = guids[i % len(guids)]
        status, body = _call("POST", "/api/highlights", {**CHECKOUT, "guid": guid,
                             "replace_booking_id": ""})
        if status == 409:  # same event twice: fine, it still spent a hold
            continue
        assert status == 201
    status, _ = _call("POST", "/api/highlights", {**CHECKOUT, "guid": "far999"})
    assert status == 429


def test_shared_ipn_routes_highlights_to_highlights(monkeypatch):
    _, body = _call("POST", "/api/highlights", CHECKOUT)
    monkeypatch.setattr(hl, "verify_with_paypal", lambda raw: True)
    monkeypatch.setattr(sp, "verify_with_paypal",
                        lambda raw: pytest.fail("sponsorships saw a highlight IPN"))
    raw = urlencode({**{k: body["paypal_fields"][k] for k in ("custom",)},
                     "txn_id": "T1", "payment_status": "Completed",
                     "mc_gross": "10.00", "mc_currency": "USD",
                     "receiver_email": "ross@karchner.com"})
    status, _ = _call("POST", "/api/paypal/ipn", raw)
    assert status == 200
    assert hl.get_highlight("g-abc123")["status"] == "active"


def test_shared_ipn_still_routes_sponsorships(monkeypatch):
    seen = []
    monkeypatch.setattr(sp, "handle_ipn", lambda raw: seen.append(raw) or "x")
    monkeypatch.setattr(hl, "handle_ipn",
                        lambda raw: pytest.fail("highlights saw a sponsorship IPN"))
    raw = urlencode({"custom": "2026-W44|abc", "txn_id": "T"})
    event_body = base64.b64encode(raw.encode()).decode()
    response = handler.lambda_handler({
        "httpMethod": "POST", "path": "/api/paypal/ipn", "headers": {},
        "requestContext": {}, "body": event_body, "isBase64Encoded": True}, None)
    assert response["statusCode"] == 200
    assert seen == [raw]


def test_admin_list_create_revoke():
    status, body = _call("POST", "/api/admin/highlights",
                         {"guid": "abc123", "note": "good one"}, claims=ADMIN)
    assert status == 201 and body["highlight"]["kind"] == "editor"
    assert body["highlight"]["created_by"] == "admin@example.com"
    status, body = _call("GET", "/api/admin/highlights", claims=ADMIN)
    assert status == 200 and len(body["highlights"]) == 1
    status, body = _call("POST", "/api/admin/highlights/g-abc123/revoke",
                         {"reason": "nope"}, claims=ADMIN)
    assert status == 200 and body["highlight"]["status"] == "revoked"


def test_admin_errors_are_json():
    status, body = _call("POST", "/api/admin/highlights", {"guid": "virt01"},
                         claims=ADMIN)
    assert status == 400 and "in-person" in body["error"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/admin/highlights"),
    ("POST", "/api/admin/highlights"),
    ("POST", "/api/admin/highlights/g-abc123/revoke"),
])
def test_admin_routes_need_admin(method, path):
    status, _ = _call(method, path, {"guid": "abc123"}, claims=USER)
    assert status == 403
    status, _ = _call(method, path, {"guid": "abc123"})
    assert status in (401, 403)
