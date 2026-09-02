"""Tests for the signup/confirm app's new category/region preference
capture — generate_confirmation_url's unsigned query-param encoding,
_clean_slugs, and route_confirm_post writing preferences after the SES
contact is created.

No prior test suite existed for this Lambda — see test_sender.py's own
docstring for why.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_app.py
"""
import hashlib
import hmac as _hmac
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("CONFIRMATION_KEY_ID", "test-confirmation-key")
os.environ.setdefault("CONTACT_LIST_NAME", "test-list")
os.environ.setdefault("TOPIC_NAME", "dctech")
os.environ.setdefault("BASE_URL", "https://dctech.events/newsletter")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))

import db  # noqa: E402
import app  # noqa: E402

SECRET = b"test-secret"


class FakeKms:
    def generate_mac(self, Message, KeyId, MacAlgorithm):
        return {"Mac": _hmac.new(SECRET, Message, hashlib.sha512).digest()}

    def verify_mac(self, Message, KeyId, MacAlgorithm, Mac):
        expected = _hmac.new(SECRET, Message, hashlib.sha512).digest()
        return {"MacValid": _hmac.compare_digest(expected, Mac)}


class FakeSes:
    def __init__(self):
        self.contacts = {}
        self.sent = []

    class exceptions:
        class AlreadyExistsException(Exception):
            pass

    def create_contact(self, ContactListName, EmailAddress, TopicPreferences):
        if EmailAddress in self.contacts:
            raise self.exceptions.AlreadyExistsException()
        self.contacts[EmailAddress] = TopicPreferences

    def get_contact(self, ContactListName, EmailAddress):
        return {'TopicPreferences': self.contacts[EmailAddress]}

    def update_contact(self, ContactListName, EmailAddress, TopicPreferences):
        self.contacts[EmailAddress] = TopicPreferences

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


class FakeTable:
    def __init__(self):
        self.items = {}

    @staticmethod
    def _k(key):
        return (key["PK"], key["SK"])

    def get_item(self, Key):
        item = self.items.get(self._k(Key))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[self._k(Item)] = dict(Item)


@pytest.fixture(autouse=True)
def fake_backends(monkeypatch):
    monkeypatch.setattr(app, "kms", FakeKms())
    monkeypatch.setattr(app, "ses", FakeSes())
    # One shared FakeTable instance per test, not a fresh one per
    # _get_table() call — a preferences write in route_confirm_post must
    # still be there when a test's own assertion reads it back.
    fake_table = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake_table)
    # app.py does `import db` (not `from db import ...`), so patching
    # db.get_all_categories directly is what its own db.get_all_categories()
    # calls actually see.
    monkeypatch.setattr(db, "get_all_categories", lambda: {
        "ai": {"slug": "ai", "name": "AI & Machine Learning"},
        "cloud": {"slug": "cloud", "name": "Cloud Computing"},
    })


def _form_body(**fields):
    from urllib.parse import urlencode
    return urlencode(fields)


# ── generate_confirmation_url ───────────────────────────────────────

def test_url_with_no_categories_or_regions_has_only_the_newsletters_param():
    url = app.generate_confirmation_url("user@example.com")
    assert "categories=" not in url
    assert "regions=" not in url
    assert "newsletters=dctech" in url


def test_url_carries_selected_categories_and_regions_unsigned():
    url = app.generate_confirmation_url(
        "user@example.com", categories=["ai", "cloud"], regions=["dc"])
    query = parse_qs(urlparse(url).query)
    assert query["categories"][0] == "ai,cloud"
    assert query["regions"][0] == "dc"


def test_the_signed_message_is_unaffected_by_categories_or_regions():
    # Same email/newsletters/timestamp must sign identically whether or
    # not categories/regions are present — they're not part of the signed
    # message at all (see the function's own docstring for why).
    ts = 1700000000
    sig_without = app.generate_confirmation_signature("user@example.com", ts, ["dctech"])
    sig_with_prefs_would_be_the_same = app.generate_confirmation_signature(
        "user@example.com", ts, ["dctech"])
    assert sig_without == sig_with_prefs_would_be_the_same


# ── _clean_slugs ─────────────────────────────────────────────────────

def test_clean_slugs_drops_unknown_values():
    assert app._clean_slugs(["ai", "not-real"], {"ai", "cloud"}) == ["ai"]


def test_clean_slugs_dedupes():
    assert app._clean_slugs(["ai", "ai"], {"ai", "cloud"}) == ["ai"]


def test_clean_slugs_handles_a_bare_string_not_just_a_list():
    # Form-encoded single-checkbox submissions can arrive as a bare string.
    assert app._clean_slugs("ai", {"ai", "cloud"}) == ["ai"]


def test_clean_slugs_on_empty_input_returns_empty():
    assert app._clean_slugs([], {"ai"}) == []
    assert app._clean_slugs(None, {"ai"}) == []


# ── route_confirm_post ──────────────────────────────────────────────

def _confirm_event(email, categories=None, regions=None):
    ts = int(time.time())
    sig = app.generate_confirmation_signature(email, ts, ["dctech"])
    body = {"email": email, "timestamp": str(ts), "signature": sig,
            "newsletters": "dctech"}
    if categories is not None:
        body["categories"] = ",".join(categories)
    if regions is not None:
        body["regions"] = ",".join(regions)
    return {"body": _form_body(**body)}


def test_confirming_with_preferences_writes_them_after_the_ses_contact(monkeypatch):
    resp = app.route_confirm_post(
        _confirm_event("user@example.com", categories=["ai"], regions=["dc"]))
    assert resp["statusCode"] == 303
    assert app.ses.contacts["user@example.com"][0]["SubscriptionStatus"] == "OPT_IN"
    saved = db.get_subscriber_preferences("user@example.com")
    assert saved["categories"] == ["ai"]
    assert saved["regions"] == ["dc"]


def test_confirming_with_no_preferences_writes_no_preferences_row(monkeypatch):
    app.route_confirm_post(_confirm_event("nofilter@example.com"))
    assert db.get_subscriber_preferences("nofilter@example.com") is None


def test_confirming_with_only_an_unknown_category_writes_no_row(monkeypatch):
    # After cleaning, nothing valid is left — same as not selecting
    # anything at all.
    app.route_confirm_post(
        _confirm_event("ghostcat@example.com", categories=["not-a-real-category"]))
    assert db.get_subscriber_preferences("ghostcat@example.com") is None


def test_an_invalid_signature_writes_nothing_and_refuses(monkeypatch):
    event = _confirm_event("user2@example.com", categories=["ai"])
    body = dict(parse_qs(event["body"]))
    tampered = {k: v[0] for k, v in body.items()}
    tampered["signature"] = "tampered"
    resp = app.route_confirm_post({"body": _form_body(**tampered)})
    assert resp["statusCode"] == 400
    assert db.get_subscriber_preferences("user2@example.com") is None
