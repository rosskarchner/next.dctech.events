"""Tests for highlighted events: quotes, holds and takeovers, editor picks,
revocation, every PayPal IPN outcome, and price parity with the site.

Reuses the expression-aware FakeTable from test_sponsorships.py: as with
sponsorships, "one live highlight per occurrence" lives in the condition
expressions, so a fake that ignored them would test nothing.

Run: python -m pytest test_highlights.py
"""
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ["PAYPAL_RECEIVER_EMAIL"] = "ross@karchner.com"

import db  # noqa: E402
import highlights as hl  # noqa: E402
from test_sponsorships import FakeTable  # noqa: E402

ET = ZoneInfo("America/New_York")
TODAY = datetime.now(ET).date()


def _day(n):
    return (TODAY + timedelta(days=n)).isoformat()


EVENTS = [
    {"guid": "abc123", "title": "BSides NOVA", "date": _day(10), "time": "08:00",
     "url": "https://bsidesnova.example", "group": "BSides", "source": "ical",
     "location": "Fairfax, VA", "location_type": "physical",
     "categories": ["cybersecurity"]},
    {"guid": "far999", "title": "Far Off Conf", "date": _day(40), "time": "09:00",
     "url": "https://far.example", "group": "Far", "source": "manual",
     "location": "DC", "categories": []},
    {"guid": "virt01", "title": "Webinar", "date": _day(5), "time": "12:00",
     "url": "https://webinar.example", "location": "Online",
     "location_type": "virtual", "source": "ical", "categories": []},
    {"guid": "past01", "title": "Yesterday", "date": _day(-1), "time": "12:00",
     "url": "https://past.example", "source": "ical", "categories": []},
    {"guid": "rec777", "title": "Community Tuesdays", "date": _day(3),
     "time": "09:00", "url": "https://refraction.example",
     "source": "recurring", "recurring_slug": "community-tuesdays",
     "is_recurring": True, "categories": []},
]


@pytest.fixture(autouse=True)
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(hl, "PRICE_USD", Decimal("10"))
    monkeypatch.setattr(hl, "_events_loader", lambda: [dict(e) for e in EVENTS])
    return fake


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(hl, "_send", lambda to, subject, text, reply_to=None:
                        sent.append({"to": to, "subject": subject, "text": text}))
    return sent


def _hold(guid="abc123", email="pat@example.com", **extra):
    return hl.create_hold({"guid": guid, "contact_email": email, **extra})


def _ipn(item, status="Completed", txn="T1", amount="10.00", **extra):
    return urlencode({
        "txn_id": txn, "payment_status": status, "mc_gross": amount,
        "mc_currency": "USD", "receiver_email": "ross@karchner.com",
        "payer_email": "payer@example.com",
        "custom": f"highlight|{item['target_key']}|{item['booking_id']}",
        **extra})


def _ok(raw):
    return True


# ─── Keys and quotes ────────────────────────────────────────────────

def test_target_key_for_stored_and_recurring_events():
    assert hl.target_key_for(EVENTS[0]) == "g-abc123"
    assert hl.target_key_for(EVENTS[4]) == f"r-community-tuesdays-{_day(3)}"


def test_quote_counts_days_and_mondays():
    q = hl.quote("abc123")
    assert q["eligible"] and q["target_key"] == "g-abc123"
    assert q["start_date"] == TODAY.isoformat()  # inside 21 days: starts now
    assert q["end_date"] == _day(10)
    assert q["days"] == 11
    expected_mondays = sum(1 for n in range(1, 11)
                           if (TODAY + timedelta(days=n)).weekday() == 0)
    assert q["newsletters"] == expected_mondays
    assert q["price_usd"] == "10"
    assert q["event"]["title"] == "BSides NOVA"


def test_quote_for_a_far_event_starts_21_days_out():
    q = hl.quote("far999")
    assert q["start_date"] == _day(19)
    assert q["days"] == 22


@pytest.mark.parametrize("guid,reason", [
    ("virt01", "in-person"),
    ("past01", "already happened"),
])
def test_quote_ineligible(guid, reason):
    q = hl.quote(guid)
    assert not q["eligible"]
    assert reason in q["reason"]


def test_unknown_event_is_404():
    with pytest.raises(hl.HighlightError) as exc:
        hl.quote("nope")
    assert exc.value.status_code == 404


def test_malformed_guid_is_404():
    with pytest.raises(hl.HighlightError):
        hl.quote("../../etc")


# ─── Holds ──────────────────────────────────────────────────────────

def test_hold_snapshots_the_event(table):
    item = _hold()
    assert item["status"] == "held" and item["kind"] == "paid"
    assert item["price_usd"] == "10"
    assert item["title"] == "BSides NOVA" and item["url"] == "https://bsidesnova.example"
    assert ("HIGHLIGHT#g-abc123", "META") in table.items
    assert hl.quote("abc123")["eligible"] is False


def test_second_buyer_is_409_while_hold_is_live():
    _hold()
    with pytest.raises(hl.HighlightError) as exc:
        _hold(email="other@example.com")
    assert exc.value.status_code == 409


def test_lapsed_hold_can_be_taken_over(table):
    first = _hold()
    table.items[("HIGHLIGHT#g-abc123", "META")]["hold_expires_at"] = 0
    second = _hold(email="other@example.com")
    assert second["booking_id"] != first["booking_id"]


def test_replace_booking_id_retakes_own_hold():
    first = _hold()
    second = _hold(replace_booking_id=first["booking_id"])
    assert second["booking_id"] != first["booking_id"]


def test_wrong_replace_id_does_not_take_over():
    _hold()
    with pytest.raises(hl.HighlightError):
        _hold(email="x@example.com", replace_booking_id="guess")


def test_recurring_occurrence_hold_uses_slug_and_date(table):
    item = _hold("rec777")
    assert item["target_key"] == f"r-community-tuesdays-{_day(3)}"
    assert item["recurring_slug"] == "community-tuesdays"


@pytest.mark.parametrize("guid", ["virt01", "past01"])
def test_ineligible_events_cannot_be_held(guid):
    with pytest.raises(hl.HighlightError):
        _hold(guid)


def test_hold_requires_email():
    with pytest.raises(hl.HighlightError, match="email"):
        _hold(email="nope")


def test_not_on_sale_at_zero_price(monkeypatch):
    monkeypatch.setattr(hl, "PRICE_USD", Decimal("0"))
    with pytest.raises(hl.HighlightError) as exc:
        _hold()
    assert exc.value.status_code == 503


def test_paypal_fields_tag_custom_and_return_to_checkout():
    item = _hold()
    fields = hl.paypal_fields(item, "https://api.example/prod/api/paypal/ipn")
    assert fields["custom"] == f"highlight|g-abc123|{item['booking_id']}"
    assert fields["amount"] == "10"
    assert fields["return"].endswith("/edit/highlight.html?status=returned&guid=abc123")
    assert fields["notify_url"] == "https://api.example/prod/api/paypal/ipn"
    assert hl.is_highlight_custom(fields["custom"])


# ─── Editor picks and revoke ────────────────────────────────────────

def test_editor_pick_is_active_and_free():
    item = hl.add_editor_pick("abc123", "ross@example.com", note="  great\nevent ")
    assert item["status"] == "active" and item["kind"] == "editor"
    assert item["price_usd"] == "0"
    assert item["note"] == "great event"
    assert [h["target_key"] for h in hl.list_highlights("active")] == ["g-abc123"]


def test_editor_pick_never_displaces_a_paid_hold():
    _hold()
    with pytest.raises(hl.HighlightError) as exc:
        hl.add_editor_pick("abc123", "ross")
    assert exc.value.status_code == 409


def test_revoke_frees_the_event():
    hl.add_editor_pick("abc123", "ross")
    item = hl.revoke("g-abc123", "ross", "changed my mind")
    assert item["status"] == "revoked" and item["revoked_by"] == "ross"
    assert hl.quote("abc123")["eligible"]
    hl.add_editor_pick("abc123", "ross")  # takes it again


def test_revoke_unknown_is_404():
    with pytest.raises(hl.HighlightError) as exc:
        hl.revoke("g-nope", "ross")
    assert exc.value.status_code == 404


def test_revoke_rejects_garbage_keys():
    with pytest.raises(hl.HighlightError):
        hl.revoke("SPONSOR#2026-W44", "ross")


# ─── IPN ────────────────────────────────────────────────────────────

def test_completed_payment_goes_live_and_emails_buyer_only(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item), verify=_ok) == "active"
    live = hl.get_highlight("g-abc123")
    assert live["status"] == "active" and live["txn_id"] == "T1"
    assert live["payer_email"] == "payer@example.com"
    assert [m["to"] for m in mail] == ["pat@example.com"]
    assert "is live" in mail[0]["subject"]


def test_unverified_ipn_is_ignored(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item), verify=lambda raw: False) == "invalid"
    assert hl.get_highlight("g-abc123")["status"] == "held"


def test_duplicate_ipn(mail):
    item = _hold()
    hl.handle_ipn(_ipn(item), verify=_ok)
    assert hl.handle_ipn(_ipn(item), verify=_ok) == "duplicate"


def test_wrong_amount_does_not_activate(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item, amount="0.01"), verify=_ok) == "wrong_amount"
    assert hl.get_highlight("g-abc123")["status"] == "held"
    assert mail and "Wrong amount" in mail[0]["subject"]


def test_wrong_currency_does_not_activate(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item, mc_currency="EUR"), verify=_ok) == "wrong_amount"


def test_wrong_receiver(mail):
    item = _hold()
    raw = _ipn(item, receiver_email="someone@else.example")
    assert hl.handle_ipn(raw, verify=_ok) == "wrong_receiver"


def test_pending_then_completed(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item, status="Pending"), verify=_ok) == "pending"
    assert hl.get_highlight("g-abc123")["status"] == "payment_pending"
    assert hl.handle_ipn(_ipn(item, status="Completed"), verify=_ok) == "active"


def test_pending_then_failed_frees_the_event(mail):
    item = _hold()
    hl.handle_ipn(_ipn(item, status="Pending"), verify=_ok)
    assert hl.handle_ipn(_ipn(item, status="Failed"), verify=_ok) == "payment_failed"
    assert hl.get_highlight("g-abc123")["status"] == "cancelled"
    assert hl.quote("abc123")["eligible"]


def test_denied_leaves_a_plain_hold_alone(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item, status="Denied"), verify=_ok) == "payment_failed"
    assert hl.get_highlight("g-abc123")["status"] == "held"


@pytest.mark.parametrize("status", ["Refunded", "Reversed"])
def test_refund_takes_it_down(mail, status):
    item = _hold()
    hl.handle_ipn(_ipn(item), verify=_ok)
    assert hl.handle_ipn(_ipn(item, status=status, txn="T2"),
                         verify=_ok) == status.lower()
    assert hl.get_highlight("g-abc123")["status"] == "refunded"


def test_canceled_reversal_changes_nothing(mail):
    item = _hold()
    hl.handle_ipn(_ipn(item), verify=_ok)
    hl.handle_ipn(_ipn(item, status="Canceled_Reversal", txn="T3"), verify=_ok)
    assert hl.get_highlight("g-abc123")["status"] == "active"


def test_other_statuses_are_ignored(mail):
    item = _hold()
    assert hl.handle_ipn(_ipn(item, status="Processed"), verify=_ok) == "ignored"


def test_payment_after_lost_hold_is_a_conflict(table, mail):
    first = _hold()
    table.items[("HIGHLIGHT#g-abc123", "META")]["hold_expires_at"] = 0
    _hold(email="other@example.com")
    assert hl.handle_ipn(_ipn(first), verify=_ok) == "conflict"
    assert any("CONFLICT" in m["subject"] for m in mail)


def test_payment_after_expiry_with_no_takeover_still_activates(table, mail):
    item = _hold()
    table.items[("HIGHLIGHT#g-abc123", "META")]["hold_expires_at"] = 0
    assert hl.handle_ipn(_ipn(item), verify=_ok) == "active"


def test_already_active(mail):
    item = _hold()
    hl.handle_ipn(_ipn(item), verify=_ok)
    assert hl.handle_ipn(_ipn(item, txn="T9"), verify=_ok) == "already_active"


def test_ipn_without_custom_is_unmatched(mail):
    raw = urlencode({"txn_id": "T1", "payment_status": "Completed",
                     "custom": "highlight|bogus"})
    assert hl.handle_ipn(raw, verify=_ok) == "unmatched"


def test_ipn_for_recurring_occurrence(mail):
    item = _hold("rec777")
    assert hl.handle_ipn(_ipn(item), verify=_ok) == "active"
    assert hl.get_highlight(item["target_key"])["status"] == "active"


def test_payment_record_is_stored_under_the_highlight(table, mail):
    item = _hold()
    hl.handle_ipn(_ipn(item), verify=_ok)
    assert ("HIGHLIGHT#g-abc123", "PAYMENT#T1#Completed") in table.items


# ─── Config parity ──────────────────────────────────────────────────

def test_price_matches_site_config():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    with open(os.path.join(root, "cdk_next", "config.py")) as f:
        cdk_price = re.search(r'^HIGHLIGHT_PRICE_USD = "([^"]*)"', f.read(), re.M)
    with open(os.path.join(root, "site", "config.yaml")) as f:
        site_price = re.search(r'^highlight_price_usd: "([^"]*)"', f.read(), re.M)
    assert cdk_price and site_price
    assert Decimal(cdk_price.group(1)) == Decimal(site_price.group(1))


def test_window_matches_calgen_upcoming_window():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    with open(os.path.join(root, "packages", "calgen", "src", "calgen",
                           "routes", "common.py")) as f:
        m = re.search(r"^UPCOMING_WINDOW_DAYS = (\d+)", f.read(), re.M)
    assert m and int(m.group(1)) == hl.WINDOW_DAYS


def test_mondays_between():
    mon = date(2026, 11, 2)
    assert hl.mondays_between(mon, mon) == [mon]
    assert hl.mondays_between(date(2026, 11, 3), date(2026, 11, 16)) == [
        date(2026, 11, 9), date(2026, 11, 16)]
