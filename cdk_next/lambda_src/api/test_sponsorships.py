"""Tests for weekly sponsorships: the booking window, holds, PayPal IPN
handling, coupons, and the admin review path.

The fake table below evaluates the condition and update expressions
sponsorships.py actually sends, because the conditions *are* the feature —
"one sponsor per week" and "a coupon is used once" live in them, not in
Python, so a fake that ignored them would test nothing.

Run: python -m pytest test_sponsorships.py
"""
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ["SPONSOR_WEEK_PRICE_USD"] = "150"
os.environ["PAYPAL_RECEIVER_EMAIL"] = "ross@karchner.com"

import db  # noqa: E402
import sponsorships as sp  # noqa: E402

ET = ZoneInfo("America/New_York")
# A Tuesday. This week is 2026-W43 (Mon Oct 19); next week, W44, closes at
# the end of Thursday Oct 22.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=ET)
NOW_TS = int(NOW.timestamp())


# ─── A fake table that honours the expressions ─────────────────────

_TOKEN = re.compile(r"\s*(\(|\)|,|<=|>=|<>|=|<|>|[A-Za-z_#:][\w#:]*)")


def _tokens(expr):
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"cannot tokenize {expr[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Cond:
    """Recursive-descent evaluator for the condition subset we use."""

    def __init__(self, expr, item, names, values):
        self.t = _tokens(expr)
        self.i = 0
        self.item, self.names, self.values = item, names or {}, values or {}

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, want=None):
        tok = self.t[self.i]
        if want and tok.upper() != want:
            raise ValueError(f"expected {want}, got {tok}")
        self.i += 1
        return tok

    def operand(self):
        tok = self.take()
        if tok.startswith(":"):
            return self.values[tok]
        name = self.names.get(tok, tok)
        return (self.item or {}).get(name)

    def atom(self):
        tok = self.peek()
        if tok == "(":
            self.take()
            val = self.expr()
            self.take(")")
            return val
        if tok in ("attribute_exists", "attribute_not_exists"):
            self.take()
            self.take("(")
            path = self.take()
            self.take(")")
            present = self.item is not None and self.names.get(path, path) in self.item
            return present if tok == "attribute_exists" else not present
        left = self.operand()
        op = self.take()
        if op.upper() == "IN":
            self.take("(")
            options = [self.operand()]
            while self.peek() == ",":
                self.take()
                options.append(self.operand())
            self.take(")")
            return left in options
        right = self.operand()
        if left is None or right is None:
            return op == "<>" and left != right
        return {"=": left == right, "<>": left != right, "<": left < right,
                ">": left > right, "<=": left <= right, ">=": left >= right}[op]

    def conj(self):
        val = self.atom()
        while self.peek() and self.peek().upper() == "AND":
            self.take()
            val = self.atom() and val
        return val

    def expr(self):
        val = self.conj()
        while self.peek() and self.peek().upper() == "OR":
            self.take()
            val = self.conj() or val
        return val


def _check(expr, item, names, values):
    return expr is None or _Cond(expr, item, names, values).expr()


def _failed(code="ConditionalCheckFailedException", **extra):
    return ClientError({"Error": {"Code": code, "Message": "no"}, **extra}, "op")


def _apply_update(item, expr, names, values):
    names = names or {}
    for clause in re.split(r"\s+(?=SET |REMOVE )", expr.strip()):
        verb, _, rest = clause.partition(" ")
        for part in rest.split(","):
            part = part.strip()
            if verb == "SET":
                path, _, value = (s.strip() for s in part.partition("="))
                item[names.get(path, path)] = values[value]
            else:
                item.pop(names.get(part, part), None)


def _scan_match(cond, item):
    expr = cond.get_expression()
    op, vals = expr["operator"], expr["values"]
    if op == "AND":
        return all(_scan_match(v, item) for v in vals)
    if op == "OR":
        return any(_scan_match(v, item) for v in vals)
    attr = item.get(vals[0].name)
    if op == "begins_with":
        return isinstance(attr, str) and attr.startswith(vals[1])
    if op == "=":
        return attr == vals[1]
    raise NotImplementedError(op)


class FakeTable:
    name = "test-table"

    def __init__(self):
        self.items = {}
        table = self

        class _Client:
            def transact_write_items(self, TransactItems):
                reasons, ok = [], True
                for entry in TransactItems:
                    (kind, spec), = entry.items()
                    key = spec["Item"] if kind == "Put" else spec["Key"]
                    passed = _check(spec.get("ConditionExpression"),
                                    table.items.get(table._k(key)),
                                    spec.get("ExpressionAttributeNames"),
                                    spec.get("ExpressionAttributeValues"))
                    reasons.append({"Code": "None" if passed else "ConditionalCheckFailed"})
                    ok = ok and passed
                if not ok:
                    raise _failed("TransactionCanceledException",
                                  CancellationReasons=reasons)
                for entry in TransactItems:
                    (kind, spec), = entry.items()
                    if kind == "Put":
                        table.items[table._k(spec["Item"])] = dict(spec["Item"])
                    else:
                        item = table.items[table._k(spec["Key"])]
                        _apply_update(item, spec["UpdateExpression"],
                                      spec.get("ExpressionAttributeNames"),
                                      spec.get("ExpressionAttributeValues"))

        self.meta = type("Meta", (), {"client": _Client()})()

    @staticmethod
    def _k(key):
        return (key["PK"], key["SK"])

    def get_item(self, Key):
        item = self.items.get(self._k(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None,
                 ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        if not _check(ConditionExpression, self.items.get(self._k(Item)),
                      ExpressionAttributeNames, ExpressionAttributeValues):
            raise _failed()
        self.items[self._k(Item)] = dict(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        current = self.items.get(self._k(Key))
        if not _check(ConditionExpression, current, ExpressionAttributeNames,
                      ExpressionAttributeValues):
            raise _failed()
        item = current if current is not None else dict(Key)
        _apply_update(item, UpdateExpression, ExpressionAttributeNames,
                      ExpressionAttributeValues)
        self.items[self._k(Key)] = item
        return {"Attributes": dict(item)}

    def scan(self, FilterExpression=None, **_):
        return {"Items": [dict(i) for i in self.items.values()
                          if FilterExpression is None
                          or _scan_match(FilterExpression, i)]}


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    monkeypatch.setattr(sp, "PRICE_USD", Decimal("150"))
    return fake


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(sp, "_send", lambda to, subject, text, reply_to=None:
                        sent.append({"to": to, "subject": subject, "text": text}))
    return sent


def _copy(**overrides):
    return {"week_id": "2026-W44", "name": "Acme", "message": "We're hiring.",
            "url": "https://acme.example/jobs",
            "contact_email": "pat@acme.example", **overrides}


# ─── Booking window ─────────────────────────────────────────────────

def test_this_week_is_never_bookable():
    assert "2026-W43" not in sp.bookable_weeks(NOW)


def test_next_week_is_bookable_until_end_of_thursday():
    thursday_night = datetime(2026, 10, 22, 23, 59, tzinfo=ET)
    friday_morning = datetime(2026, 10, 23, 0, 0, tzinfo=ET)
    assert "2026-W44" in sp.bookable_weeks(thursday_night)
    assert "2026-W44" not in sp.bookable_weeks(friday_morning)
    assert "2026-W45" in sp.bookable_weeks(friday_morning)


def test_horizon_is_26_weeks():
    weeks = sp.bookable_weeks(NOW)
    assert weeks[0] == "2026-W44"
    # Mon Apr 19 2027, 26 weeks after W43 (2026 has 53 ISO weeks).
    assert weeks[-1] == "2027-W16"
    assert len(weeks) == 26


def test_cutoff_is_eastern_across_dst():
    # DST ends Nov 1 2026; W45's cutoff is Friday Oct 30 00:00 EDT.
    cutoff = sp.booking_cutoff("2026-W45")
    assert cutoff.utcoffset().total_seconds() == -4 * 3600
    # W46's is Nov 6 00:00 EST.
    assert sp.booking_cutoff("2026-W46").utcoffset().total_seconds() == -5 * 3600


# ─── Copy validation ────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", "x" * 61), ("message", "x" * 141),
    ("url", "http://acme.example"), ("url", "javascript:alert(1)"),
    ("url", 'https://acme.example/"onmouseover'), ("contact_email", "nope"),
])
def test_bad_copy_is_refused(field, value):
    with pytest.raises(sp.SponsorshipError):
        sp.validate_copy(_copy(**{field: value}))


def test_copy_is_flattened_to_one_line():
    assert sp.validate_copy(_copy(message="two\nlines\t here"))["message"] == "two lines here"


# ─── Holds ──────────────────────────────────────────────────────────

def test_hold_returns_paypal_fields(table):
    booking, fields = sp.create_hold(_copy(), now=NOW)
    assert booking["status"] == "held"
    assert fields["business"] == "ross@karchner.com"
    assert fields["amount"] == "150"
    assert fields["custom"] == f"2026-W44|{booking['booking_id']}"


def test_second_hold_on_a_week_is_refused(table):
    sp.create_hold(_copy(), now=NOW)
    with pytest.raises(sp.SponsorshipError) as exc:
        sp.create_hold(_copy(contact_email="other@example.com"), now=NOW)
    assert exc.value.status_code == 409


def test_booker_can_replace_their_own_hold(table):
    first, _ = sp.create_hold(_copy(), now=NOW)
    second, _ = sp.create_hold(
        _copy(message="Take two.", replace_booking_id=first["booking_id"]), now=NOW)
    assert second["booking_id"] != first["booking_id"]


def test_knowing_the_email_does_not_replace_a_hold(table):
    """The email is unverified: anyone who guessed it could otherwise swap
    out a hold mid-payment and turn the real booker's payment into a conflict."""
    first, _ = sp.create_hold(_copy(), now=NOW)
    with pytest.raises(sp.SponsorshipError) as exc:
        sp.create_hold(_copy(message="Mine now."), now=NOW)
    assert exc.value.status_code == 409
    with pytest.raises(sp.SponsorshipError):
        sp.create_hold(_copy(replace_booking_id="not-the-id"), now=NOW)
    assert sp.get_sponsorship("2026-W44")["booking_id"] == first["booking_id"]


def test_booking_id_cannot_replace_a_paid_booking(table, mail):
    first, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(first), verify=_verified)
    with pytest.raises(sp.SponsorshipError):
        sp.create_hold(_copy(replace_booking_id=first["booking_id"]), now=NOW)


def test_expired_hold_frees_the_week(table):
    sp.create_hold(_copy(), now=NOW)
    later = NOW_TS + sp.HOLD_SECONDS + 1
    booking, _ = sp.create_hold(_copy(contact_email="other@example.com"), now=later)
    assert booking["contact_email"] == "other@example.com"


def test_rejected_week_can_be_rebooked(table, mail):
    sp.manual_booking("2026-W44", _copy(), "admin", approve=False, now=NOW)
    sp.reject("2026-W44", "admin")
    sp.create_hold(_copy(contact_email="other@example.com"), now=NOW)


def test_holds_closed_when_price_is_zero(table, monkeypatch):
    monkeypatch.setattr(sp, "PRICE_USD", Decimal("0"))
    with pytest.raises(sp.SponsorshipError) as exc:
        sp.create_hold(_copy(), now=NOW)
    assert exc.value.status_code == 503


def test_week_outside_window_is_refused(table):
    with pytest.raises(sp.SponsorshipError):
        sp.create_hold(_copy(week_id="2026-W43"), now=NOW)


def test_availability_hides_taken_weeks(table):
    sp.create_hold(_copy(), now=NOW)
    weeks = {w["week_id"]: w["available"] for w in sp.availability(NOW)["weeks"]}
    assert weeks["2026-W44"] is False
    assert weeks["2026-W45"] is True


# ─── IPN ────────────────────────────────────────────────────────────

def _ipn(booking, **overrides):
    from urllib.parse import urlencode
    params = {"txn_id": "TX1", "payment_status": "Completed",
              "mc_gross": "150.00", "mc_currency": "USD",
              "receiver_email": "ross@karchner.com",
              "payer_email": "payer@example.com",
              "custom": f"{booking['week_id']}|{booking['booking_id']}",
              **overrides}
    return urlencode(params)


def _verified(_body):
    return True


def test_completed_payment_marks_booking_paid(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    assert sp.handle_ipn(_ipn(booking), verify=_verified) == "paid"
    stored = sp.get_sponsorship("2026-W44")
    assert stored["status"] == "paid"
    assert stored["txn_id"] == "TX1"
    assert {m["to"] for m in mail} == {"ross@karchner.com", "pat@acme.example"}


def test_unverified_ipn_changes_nothing(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    assert sp.handle_ipn(_ipn(booking), verify=lambda b: False) == "invalid"
    assert sp.get_sponsorship("2026-W44")["status"] == "held"


def test_duplicate_ipn_is_ignored(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(booking), verify=_verified)
    assert sp.handle_ipn(_ipn(booking), verify=_verified) == "duplicate"


@pytest.mark.parametrize("override,outcome", [
    ({"receiver_email": "someone@else.example"}, "wrong_receiver"),
    ({"mc_gross": "1.00"}, "wrong_amount"),
    ({"mc_currency": "EUR"}, "wrong_amount"),
])
def test_bad_payment_does_not_mark_paid(table, mail, override, outcome):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    assert sp.handle_ipn(_ipn(booking, **override), verify=_verified) == outcome
    assert sp.get_sponsorship("2026-W44")["status"] == "held"
    assert any(m["to"] == "ross@karchner.com" for m in mail)


def test_payment_after_hold_lost_is_a_conflict(table, mail):
    lost, _ = sp.create_hold(_copy(), now=NOW)
    later = NOW_TS + sp.HOLD_SECONDS + 1
    sp.create_hold(_copy(contact_email="other@example.com"), now=later)
    assert sp.handle_ipn(_ipn(lost), verify=_verified) == "conflict"
    assert sp.get_sponsorship("2026-W44")["contact_email"] == "other@example.com"
    assert any("CONFLICT" in m["subject"] for m in mail)


def test_late_payment_on_expired_but_untaken_hold_still_counts(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    # Hold lapsed, nobody else booked: the money is ours and so is the week.
    assert sp.handle_ipn(_ipn(booking), verify=_verified) == "paid"


def test_pending_then_completed(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    assert sp.handle_ipn(_ipn(booking, payment_status="Pending"),
                         verify=_verified) == "pending"
    assert sp.get_sponsorship("2026-W44")["status"] == "payment_pending"
    assert sp.handle_ipn(_ipn(booking), verify=_verified) == "paid"


@pytest.mark.parametrize("override", [
    {"mc_gross": "0.01"},
    {"mc_currency": "JPY", "mc_gross": "1", "pending_reason": "multi_currency"},
])
def test_underpaid_pending_payment_does_not_hold_the_week(table, mail, override):
    """payment_pending never lapses, so a cheap Pending payment must not be
    a way to lock a week."""
    booking, _ = sp.create_hold(_copy(), now=NOW)
    pending = _ipn(booking, payment_status="Pending", **override)
    assert sp.handle_ipn(pending, verify=_verified) == "wrong_amount"
    assert sp.get_sponsorship("2026-W44")["status"] == "held"


@pytest.mark.parametrize("final", ["Failed", "Denied", "Voided", "Expired"])
def test_pending_payment_that_never_clears_frees_the_week(table, mail, final):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(booking, payment_status="Pending"), verify=_verified)
    assert sp.handle_ipn(_ipn(booking, payment_status=final),
                         verify=_verified) == "payment_failed"
    assert sp.get_sponsorship("2026-W44")["status"] == "cancelled"
    sp.create_hold(_copy(contact_email="other@example.com"), now=NOW)


def test_failure_of_another_txn_leaves_pending_booking_alone(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(booking, payment_status="Pending"), verify=_verified)
    sp.handle_ipn(_ipn(booking, txn_id="TX9", payment_status="Failed"),
                  verify=_verified)
    assert sp.get_sponsorship("2026-W44")["status"] == "payment_pending"


def test_refund_marks_booking_refunded(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(booking), verify=_verified)
    sp.approve("2026-W44", "admin")
    refund = _ipn(booking, txn_id="TX2", parent_txn_id="TX1",
                  payment_status="Refunded", mc_gross="-150.00")
    assert sp.handle_ipn(refund, verify=_verified) == "refunded"
    assert sp.get_sponsorship("2026-W44")["status"] == "refunded"


def test_ipn_without_our_custom_field(table, mail):
    assert sp.handle_ipn("txn_id=X&payment_status=Completed",
                         verify=_verified) == "unmatched"


# ─── Review ─────────────────────────────────────────────────────────

def test_approve_with_edits(table, mail):
    booking, _ = sp.create_hold(_copy(), now=NOW)
    sp.handle_ipn(_ipn(booking), verify=_verified)
    approved = sp.approve("2026-W44", "admin", {"message": "Fixed typo."})
    assert approved["status"] == "approved"
    assert approved["message"] == "Fixed typo."
    assert mail[-1]["subject"].endswith("confirmed")


def test_cannot_approve_a_rejected_booking(table, mail):
    sp.manual_booking("2026-W44", _copy(), "admin", approve=False, now=NOW)
    sp.reject("2026-W44", "admin")
    with pytest.raises(sp.SponsorshipError) as exc:
        sp.approve("2026-W44", "admin")
    assert exc.value.status_code == 409


def test_manual_booking_ignores_window_but_not_holds(table):
    sp.manual_booking("2026-W43", _copy(week_id="2026-W43"), "admin", now=NOW)
    assert sp.get_sponsorship("2026-W43")["status"] == "approved"
    sp.create_hold(_copy(), now=NOW)
    with pytest.raises(sp.SponsorshipError):
        sp.manual_booking("2026-W44", _copy(), "admin", now=NOW)


def test_list_filters_by_status(table, mail):
    sp.manual_booking("2026-W44", _copy(), "admin", now=NOW)
    sp.create_hold(_copy(week_id="2026-W45"), now=NOW)
    assert [r["week_id"] for r in sp.list_sponsorships("approved")] == ["2026-W44"]
    assert len(sp.list_sponsorships()) == 2


# ─── Coupons ────────────────────────────────────────────────────────

def test_mint_makes_unambiguous_codes(table):
    codes = [c["code"] for c in sp.mint_coupons(5, note="DC Python")]
    assert len(set(codes)) == 5
    for code in codes:
        assert len(code) == sp.COUPON_LENGTH
        assert not set(code) & set("ILOU")


def test_coupon_books_week_as_paid(table, mail):
    code = sp.mint_coupons(1)[0]["code"]
    booking = sp.redeem_coupon_booking(_copy(), code.lower(), now=NOW)
    assert booking["status"] == "paid"
    assert booking["payment_method"] == "coupon"
    assert sp._get_coupon(code)["redeemed_week"] == "2026-W44"


def test_coupon_cannot_be_used_twice(table, mail):
    code = sp.mint_coupons(1)[0]["code"]
    sp.redeem_coupon_booking(_copy(), code, now=NOW)
    with pytest.raises(sp.SponsorshipError, match="coupon"):
        sp.redeem_coupon_booking(_copy(week_id="2026-W45"), code, now=NOW)


def test_coupon_is_untouched_when_week_is_taken(table, mail):
    code = sp.mint_coupons(1)[0]["code"]
    sp.create_hold(_copy(contact_email="other@example.com"), now=NOW)
    with pytest.raises(sp.SponsorshipError) as exc:
        sp.redeem_coupon_booking(_copy(), code, now=NOW)
    assert exc.value.status_code == 409
    assert "redeemed_week" not in sp._get_coupon(code)


def test_rejecting_a_coupon_booking_frees_the_coupon(table, mail):
    code = sp.mint_coupons(1)[0]["code"]
    sp.redeem_coupon_booking(_copy(), code, now=NOW)
    sp.reject("2026-W44", "admin")
    assert "redeemed_week" not in sp._get_coupon(code)
    sp.redeem_coupon_booking(_copy(week_id="2026-W45"), code, now=NOW)


@pytest.mark.parametrize("code", ["", "short", "NOTAREALCODE", "0000000000"])
def test_unknown_coupon_is_refused(table, code):
    with pytest.raises(sp.SponsorshipError, match="coupon"):
        sp.redeem_coupon_booking(_copy(), code, now=NOW)


def test_revoked_coupon_is_refused(table):
    code = sp.mint_coupons(1)[0]["code"]
    sp.revoke_coupon(code)
    with pytest.raises(sp.SponsorshipError, match="coupon"):
        sp.redeem_coupon_booking(_copy(), code, now=NOW)


def test_expired_coupon_is_refused(table):
    code = sp.mint_coupons(1, expires_on="2026-10-19")[0]["code"]
    with pytest.raises(sp.SponsorshipError, match="coupon"):
        sp.redeem_coupon_booking(_copy(), code, now=NOW)


def test_coupon_valid_on_its_expiry_day(table, mail):
    code = sp.mint_coupons(1, expires_on="2026-10-20")[0]["code"]
    sp.redeem_coupon_booking(_copy(), code, now=NOW)


# ─── Config drift ───────────────────────────────────────────────────

def test_site_price_matches_cdk_price():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    with open(os.path.join(root, "cdk_next", "config.py")) as f:
        cdk_price = re.search(r'^SPONSOR_WEEK_PRICE_USD = "([^"]*)"', f.read(), re.M)
    with open(os.path.join(root, "site", "config.yaml")) as f:
        site_price = re.search(r'^sponsorship_price_usd: "([^"]*)"', f.read(), re.M)
    assert cdk_price and site_price
    assert Decimal(cdk_price.group(1)) == Decimal(site_price.group(1))
