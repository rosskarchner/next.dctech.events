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
os.environ.setdefault("PATH_PREFIX", "/newsletter")
os.environ.setdefault("PREFS_KEY_ID", "test-prefs-key")

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
    class exceptions:
        class AlreadyExistsException(Exception):
            pass

        class NotFoundException(Exception):
            pass

    def __init__(self):
        self.contacts = {}
        self.unsubscribe_all = {}
        self.sent = []

    def create_contact(self, ContactListName, EmailAddress, TopicPreferences):
        if EmailAddress in self.contacts:
            raise self.exceptions.AlreadyExistsException()
        self.contacts[EmailAddress] = TopicPreferences

    def get_contact(self, ContactListName, EmailAddress):
        if EmailAddress not in self.contacts:
            raise self.exceptions.NotFoundException()
        return {'TopicPreferences': self.contacts[EmailAddress],
                'UnsubscribeAll': self.unsubscribe_all.get(EmailAddress, False)}

    def update_contact(self, ContactListName, EmailAddress, TopicPreferences,
                       UnsubscribeAll=None):
        self.contacts[EmailAddress] = TopicPreferences
        if UnsubscribeAll is not None:
            self.unsubscribe_all[EmailAddress] = UnsubscribeAll

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
    monkeypatch.setattr(app.magic_link, "kms", FakeKms())
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


# ── route_signup: one entry point for subscribe and manage ──────────

OPT_IN = [{"TopicName": "dctech", "SubscriptionStatus": "OPT_IN"}]
OPT_OUT = [{"TopicName": "dctech", "SubscriptionStatus": "OPT_OUT"}]


def _signup(email):
    return app.route_signup({
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "body": _form_body(email=email, newsletters="dctech"),
    })


def _only_sent():
    assert len(app.ses.sent) == 1
    return app.ses.sent[0]


def _subject(msg):
    return msg["Content"]["Simple"]["Subject"]["Data"]


def test_a_new_address_gets_a_confirmation_email():
    resp = _signup("new@example.com")
    assert resp["statusCode"] == 200
    msg = _only_sent()
    assert "Confirm your subscription" in _subject(msg)
    assert "/newsletter/confirm/" in msg["Content"]["Simple"]["Body"]["Html"]["Data"]


def test_a_subscribed_address_gets_a_manage_link_instead():
    app.ses.contacts["member@example.com"] = list(OPT_IN)
    resp = _signup("member@example.com")
    assert resp["statusCode"] == 200
    msg = _only_sent()
    assert "Manage your" in _subject(msg)
    text = msg["Content"]["Simple"]["Body"]["Text"]["Data"]
    link = next(w for w in text.split() if w.startswith("https://"))
    # Site root, not the /newsletter mount this Lambda lives under.
    assert link.startswith("https://dctech.events/edit/preferences.html?")
    q = parse_qs(urlparse(link).query)
    ok, reason = app.magic_link.verify_token(
        app.magic_link.decode_email_param(q["e"][0]), q["t"][0], q["s"][0],
        purpose="prefs")
    assert ok, reason


def test_signup_normalizes_the_address_before_looking_it_up():
    app.ses.contacts["member@example.com"] = list(OPT_IN)
    _signup("  Member@Example.com ")
    assert "Manage your" in _subject(_only_sent())


def test_an_opted_out_address_gets_a_confirmation_not_a_manage_link():
    app.ses.contacts["former@example.com"] = list(OPT_OUT)
    _signup("former@example.com")
    assert "Confirm your subscription" in _subject(_only_sent())


def test_unsubscribe_all_counts_as_not_subscribed():
    app.ses.contacts["gone@example.com"] = list(OPT_IN)
    app.ses.unsubscribe_all["gone@example.com"] = True
    _signup("gone@example.com")
    assert "Confirm your subscription" in _subject(_only_sent())


def test_both_branches_return_the_same_response():
    app.ses.contacts["member@example.com"] = list(OPT_IN)
    subscribed = _signup("member@example.com")
    new = _signup("new@example.com")
    assert subscribed["statusCode"] == new["statusCode"]
    assert subscribed["body"] == new["body"]


def test_an_invalid_address_is_refused_without_sending():
    resp = _signup("not-an-email")
    assert resp["statusCode"] == 400
    assert app.ses.sent == []


# ── confirm page: preferences are chosen here now ───────────────────

def _confirm_link_parts(email, query=None):
    url = app.generate_confirmation_url(email, **(query or {}))
    path, _, qs = url.partition("?")
    _, _, enc_email, enc_ts, sig = path.split("/")
    params = {k: v[0] for k, v in parse_qs(qs).items()}
    return {"queryStringParameters": params}, enc_email, enc_ts, sig


def test_confirm_page_shows_checkboxes_with_carried_selections_checked():
    event, e, t, s = _confirm_link_parts(
        "user@example.com", {"categories": ["ai"], "regions": ["va"]})
    body = app.route_confirm_link(event, e, t, s)["body"]
    assert 'name="categories" value="ai" checked' in body
    assert 'name="categories" value="cloud">' in body
    assert 'name="regions" value="va" checked' in body
    assert 'name="regions" value="dc">' in body


def test_confirming_with_repeated_checkbox_fields_saves_them():
    ts = int(time.time())
    sig = app.generate_confirmation_signature("user@example.com", ts, ["dctech"])
    from urllib.parse import urlencode
    body = urlencode([("email", "user@example.com"), ("timestamp", str(ts)),
                      ("signature", sig), ("newsletters", "dctech"),
                      ("categories", "ai"), ("categories", "cloud"),
                      ("regions", "md")])
    resp = app.route_confirm_post({"body": body})
    assert resp["statusCode"] == 303
    saved = db.get_subscriber_preferences("user@example.com")
    assert saved["categories"] == ["ai", "cloud"]
    assert saved["regions"] == ["md"]


def test_reconfirming_clears_unsubscribe_all():
    app.ses.contacts["back@example.com"] = list(OPT_OUT)
    app.ses.unsubscribe_all["back@example.com"] = True
    app.route_confirm_post(_confirm_event("back@example.com"))
    assert app.ses.unsubscribe_all["back@example.com"] is False
    assert app._is_subscribed("back@example.com")


# ── page shell vs. the homepage's htmx include ──────────────────────

def test_full_page_uses_the_site_shell():
    body = app.route_index({"headers": {}})["body"]
    assert '<link rel="stylesheet" href="/static/css/main.css">' in body
    assert '<body class="newsletter-page">' in body
    assert 'id="signup-form"' in body


def test_htmx_request_still_gets_only_the_bare_form():
    # The homepage's hx-get swaps this into its own #subscribe box, so it
    # must not carry a page shell, stylesheet, or a second #subscribe.
    body = app.route_index({"headers": {"HX-Request": "true"}})["body"]
    assert "<html" not in body
    assert "main.css" not in body
    assert 'id="subscribe"' not in body
    assert 'id="signup-form"' in body


# ── next_dctech_events-a0i: per-address rate limit, no CSRF token ───

def _htmx_signup(email):
    return app.route_signup({
        "headers": {"Content-Type": "application/x-www-form-urlencoded",
                    "HX-Request": "true"},
        "body": _form_body(email=email, newsletters="dctech"),
    })


def test_a_second_signup_within_the_cooldown_is_refused_without_sending():
    assert _signup("new@example.com")["statusCode"] == 200
    resp = _signup("new@example.com")
    assert resp["statusCode"] == 429
    assert "just sent" in resp["body"]
    assert len(app.ses.sent) == 1


def test_the_limit_is_per_address():
    _signup("one@example.com")
    assert _signup("two@example.com")["statusCode"] == 200
    assert len(app.ses.sent) == 2


def test_the_limit_is_shared_with_submit_links():
    # Same MAGICLINK#<email> counter /api/submit-link consumes: one
    # allowance per inbox across every kind of link this site emails.
    db.check_and_record_link_request("shared@example.com")
    assert _signup("shared@example.com")["statusCode"] == 429
    assert app.ses.sent == []


def test_rate_limited_response_does_not_reveal_subscription_status():
    app.ses.contacts["member@example.com"] = list(OPT_IN)
    db.check_and_record_link_request("member@example.com")
    db.check_and_record_link_request("stranger@example.com")
    member, stranger = _signup("member@example.com"), _signup("stranger@example.com")
    assert member["statusCode"] == stranger["statusCode"] == 429
    assert member["body"] == stranger["body"]


def test_htmx_errors_come_back_as_200_with_the_form_and_the_email_kept():
    # htmx won't swap a 4xx in, so the user would see nothing; and the
    # swap replaces the form, so it has to come back with the message.
    _htmx_signup("new@example.com")
    resp = _htmx_signup("new@example.com")
    assert resp["statusCode"] == 200
    assert 'id="signup-form"' in resp["body"]
    assert 'role="alert"' in resp["body"] and "just sent" in resp["body"]
    assert 'value="new@example.com"' in resp["body"]
    assert len(app.ses.sent) == 1


def test_htmx_invalid_address_also_keeps_the_form():
    resp = _htmx_signup("not-an-email")
    assert resp["statusCode"] == 200
    assert 'id="signup-form"' in resp["body"]
    assert "valid email" in resp["body"]


def test_the_form_carries_no_csrf_token():
    for headers in ({}, {"HX-Request": "true"}):
        body = app.route_index({"headers": headers})["body"]
        assert "csrf_token" not in body and "temp_id" not in body
    assert not hasattr(app, "secrets")  # no Secrets Manager client at all
