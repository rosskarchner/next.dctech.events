"""Route-level tests for sponsorships: handler dispatch, the public booking
and IPN endpoints, and that the admin endpoints really are admin-only.

The rules themselves are covered in test_sponsorships.py; this reuses its
expression-aware fake table.

Run: python -m pytest test_sponsorship_routes.py
"""
import base64
import json
import os
from decimal import Decimal
from urllib.parse import urlencode

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402
import handler  # noqa: E402
import sponsorships as sp  # noqa: E402
from test_sponsorships import FakeTable, NOW  # noqa: E402

ADMIN = {"sub": "u1", "email": "admin@example.com", "cognito:groups": "admins"}
USER = {"sub": "u2", "email": "user@example.com"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(sp, "PRICE_USD", Decimal("150"))
    monkeypatch.setattr(sp, "_send", lambda *a, **k: None)
    # Pin "now" so the booking window is deterministic.
    real_bookable = sp.bookable_weeks
    monkeypatch.setattr(sp, "bookable_weeks", lambda now=None: real_bookable(NOW))
    return fake


def _event(method, path, body=None, claims=None, query=None):
    ctx = {"domainName": "api.example", "stage": "prod",
           "identity": {"sourceIp": "203.0.113.9"}}
    if claims:
        ctx["authorizer"] = {"claims": claims}
    return {"httpMethod": method, "path": path, "headers": {},
            "requestContext": ctx, "queryStringParameters": query,
            "body": body if isinstance(body, str) or body is None
            else json.dumps(body)}


def _call(*args, **kwargs):
    response = handler.lambda_handler(_event(*args, **kwargs), None)
    body = response.get("body") or ""
    try:
        body = json.loads(body)
    except ValueError:
        pass
    return response["statusCode"], body


BOOKING = {"week_id": "2026-W44", "name": "Acme", "message": "Hiring.",
           "url": "https://acme.example", "contact_email": "pat@acme.example"}


def test_availability_is_public():
    status, body = _call("GET", "/api/sponsorships/availability")
    assert status == 200
    assert body["weeks"][0] == {"week_id": "2026-W44", "week_start": "2026-10-26",
                                "available": True}


def test_booking_returns_paypal_form_with_notify_url():
    status, body = _call("POST", "/api/sponsorships", BOOKING)
    assert status == 201
    assert body["paypal_url"] == "https://www.paypal.com/cgi-bin/webscr"
    assert body["paypal_fields"]["notify_url"] == \
        "https://api.example/prod/api/paypal/ipn"


def test_double_booking_is_409():
    _call("POST", "/api/sponsorships", BOOKING)
    status, body = _call("POST", "/api/sponsorships",
                         {**BOOKING, "contact_email": "b@example.com"})
    assert status == 409


def test_coupon_booking_skips_paypal():
    code = sp.mint_coupons(1)[0]["code"]
    status, body = _call("POST", "/api/sponsorships",
                         {**BOOKING, "coupon_code": code})
    assert status == 201
    assert body == {"week_id": "2026-W44", "status": "paid"}


def test_booking_is_rate_limited(monkeypatch):
    monkeypatch.setattr(handler.sponsorships, "check_and_record_write",
                        lambda who, action, **kw: (False, 60))
    status, _ = _call("POST", "/api/sponsorships", BOOKING)
    assert status == 429


def test_holds_have_their_own_smaller_budget():
    weeks = sp.bookable_weeks()
    for week in weeks[:sp.HOLDS_PER_IP_PER_HOUR]:
        status, _ = _call("POST", "/api/sponsorships", {**BOOKING, "week_id": week})
        assert status == 201
    status, body = _call("POST", "/api/sponsorships",
                         {**BOOKING, "week_id": weeks[sp.HOLDS_PER_IP_PER_HOUR]})
    assert status == 429
    # Coupon bookings draw on the larger budget, not the hold one.
    code = sp.mint_coupons(1)[0]["code"]
    status, _ = _call("POST", "/api/sponsorships",
                      {**BOOKING, "week_id": weeks[-1], "coupon_code": code})
    assert status == 201


def test_rebooking_with_the_returned_booking_id_replaces_the_hold():
    _, first = _call("POST", "/api/sponsorships", BOOKING)
    status, second = _call("POST", "/api/sponsorships",
                           {**BOOKING, "replace_booking_id": first["booking_id"]})
    assert status == 201
    assert second["booking_id"] != first["booking_id"]


def test_ipn_answers_200_and_echoes_body_verbatim(monkeypatch):
    _, booking = _call("POST", "/api/sponsorships", BOOKING)
    seen = []
    monkeypatch.setattr(sp, "verify_with_paypal",
                        lambda raw: seen.append(raw) or True)
    raw = urlencode({"txn_id": "T1", "payment_status": "Completed",
                     "mc_gross": "150.00", "mc_currency": "USD",
                     "receiver_email": "ross@karchner.com",
                     "custom": booking["paypal_fields"]["custom"]})
    event = _event("POST", "/api/paypal/ipn",
                   base64.b64encode(raw.encode()).decode())
    event["isBase64Encoded"] = True
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 200
    assert seen == [raw]
    assert sp.get_sponsorship("2026-W44")["status"] == "paid"


def test_ipn_crash_returns_500_so_paypal_retries(monkeypatch):
    def boom(raw):
        raise RuntimeError("dynamo down")
    monkeypatch.setattr(sp, "handle_ipn", boom)
    response = handler.lambda_handler(_event("POST", "/api/paypal/ipn", "x=1"), None)
    assert response["statusCode"] == 500


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/admin/sponsorships"),
    ("POST", "/api/admin/sponsorships/2026-W44/approve"),
    ("GET", "/api/admin/coupons"),
    ("POST", "/api/admin/coupons"),
    ("DELETE", "/api/admin/coupons/ABCDEFGHJK"),
])
def test_admin_routes_refuse_non_admins(method, path):
    status, _ = _call(method, path, {}, claims=USER)
    assert status == 403


def test_admin_review_flow():
    code = sp.mint_coupons(1)[0]["code"]
    _call("POST", "/api/sponsorships", {**BOOKING, "coupon_code": code})

    status, body = _call("GET", "/api/admin/sponsorships", claims=ADMIN,
                         query={"status": "paid"})
    assert status == 200
    assert [s["week_id"] for s in body["sponsorships"]] == ["2026-W44"]

    status, body = _call("PUT", "/api/admin/sponsorships/2026-W44",
                         {"message": "Edited."}, claims=ADMIN)
    assert status == 200 and body["sponsorship"]["message"] == "Edited."

    status, body = _call("POST", "/api/admin/sponsorships/2026-W44/approve",
                         claims=ADMIN)
    assert status == 200 and body["sponsorship"]["status"] == "approved"
    assert body["sponsorship"]["reviewed_by"] == "admin@example.com"


def test_admin_mint_and_revoke():
    status, body = _call("POST", "/api/admin/coupons",
                         {"count": 2, "note": "comp"}, claims=ADMIN)
    assert status == 201 and len(body["coupons"]) == 2
    code = body["coupons"][0]["code"]
    status, body = _call("DELETE", f"/api/admin/coupons/{code}", claims=ADMIN)
    assert status == 200 and body["coupon"]["revoked"]


def test_admin_mint_rejects_garbage_count():
    status, body = _call("POST", "/api/admin/coupons", {"count": "lots"},
                         claims=ADMIN)
    assert status == 400
