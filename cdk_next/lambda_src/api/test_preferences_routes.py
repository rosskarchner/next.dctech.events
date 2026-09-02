"""Tests for the subscriber category/region preferences routes.

Crypto correctness (purpose separation, TTLs) is test_magic_link.py's job;
these cover the HTTP shape — that a valid prefs-purpose token gets the
current selections plus the full catalog, an invalid/wrong-purpose token is
refused, and a save only ever persists real, currently-existing slugs.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_preferences_routes.py
"""
import json
import os
from base64 import urlsafe_b64encode

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402
import handler  # noqa: E402
import magic_link  # noqa: E402
import routes.preferences as preferences  # noqa: E402


def _b64_email(email):
    return urlsafe_b64encode(email.encode()).decode().rstrip("=")


def request(path, method="GET", body=None, query=None):
    return {
        "path": path,
        "httpMethod": method,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
        "requestContext": {},
        "headers": {"Content-Type": "application/json"} if body is not None else {},
    }


def call(path, method="GET", **kw):
    response = handler.lambda_handler(request(path, method, **kw), None)
    try:
        payload = json.loads(response["body"]) if response.get("body") else {}
    except (ValueError, TypeError):
        payload = {"error": response.get("body")}
    return response["statusCode"], payload


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


@pytest.fixture
def store(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    # routes/preferences.py does `from db import get_all_categories`, so it
    # holds its own reference — patching db.get_all_categories alone
    # wouldn't reach the route's calls to it.
    categories = {
        "ai": {"slug": "ai", "name": "AI & Machine Learning", "description": ""},
        "cloud": {"slug": "cloud", "name": "Cloud Computing", "description": ""},
    }
    monkeypatch.setattr(preferences, "get_all_categories", lambda: categories)
    return fake


@pytest.fixture
def verified_prefs_link(monkeypatch):
    """Bypass real KMS verification but still require purpose='prefs' —
    these tests cover routing/storage, not crypto (that's
    test_magic_link.py's job)."""
    def fake_verify(email, timestamp, signature, purpose='submit', ttl_seconds=None):
        if purpose != 'prefs':
            return False, 'Invalid preferences link'
        return True, None
    monkeypatch.setattr(magic_link, "verify_token", fake_verify)


# ── GET /api/preferences ────────────────────────────────────────────


def test_a_valid_token_returns_selections_and_the_full_catalog(store, verified_prefs_link):
    db.put_subscriber_preferences("user@example.com", ["ai"], ["dc"])
    status, payload = call(
        "/api/preferences",
        query={"e": _b64_email("user@example.com"), "t": "1", "s": "sig"})
    assert status == 200
    assert payload["email"] == "user@example.com"
    assert payload["categories"] == ["ai"]
    assert payload["regions"] == ["dc"]
    assert set(payload["all_categories"].keys()) == {"ai", "cloud"}
    assert set(payload["all_regions"].keys()) == {"dc", "md", "va"}


def test_a_subscriber_with_no_preferences_set_gets_empty_selections(store, verified_prefs_link):
    status, payload = call(
        "/api/preferences",
        query={"e": _b64_email("fresh@example.com"), "t": "1", "s": "sig"})
    assert status == 200
    assert payload["categories"] == []
    assert payload["regions"] == []


def test_an_invalid_token_is_refused(store):
    # No verified_prefs_link fixture — real magic_link.verify_token runs and
    # rejects the fake signature.
    status, _ = call(
        "/api/preferences",
        query={"e": _b64_email("user@example.com"), "t": "1", "s": "bogus"})
    assert status == 401


def test_a_real_submit_purpose_token_is_rejected_end_to_end(store, monkeypatch):
    # No monkeypatched verify_token here — real KMS-backed (faked at the
    # boto3 client level, like test_magic_link.py) generate/verify, proving
    # the route really does ask for purpose='prefs' specifically, not just
    # any valid token.
    import hashlib
    import hmac as _hmac

    class FakeKms:
        def generate_mac(self, Message, KeyId, MacAlgorithm):
            return {"Mac": _hmac.new(b"test-secret", Message, hashlib.sha512).digest()}

        def verify_mac(self, Message, KeyId, MacAlgorithm, Mac):
            expected = _hmac.new(b"test-secret", Message, hashlib.sha512).digest()
            return {"MacValid": _hmac.compare_digest(expected, Mac)}

    monkeypatch.setattr(magic_link, "kms", FakeKms())
    monkeypatch.setattr(magic_link, "SUBMIT_KEY_ID", "test-key")
    # purpose='prefs' signs with its own key (see magic_link._key_id_for) —
    # without this, verify_token would refuse as "not configured" rather
    # than the "wrong purpose" rejection this test is actually after.
    monkeypatch.setattr(magic_link, "PREFS_KEY_ID", "test-prefs-key")

    ts, sig = magic_link.generate_token("user@example.com", purpose="submit")
    status, payload = call(
        "/api/preferences",
        query={"e": _b64_email("user@example.com"), "t": str(ts), "s": sig})
    assert status == 401
    assert "preferences link" in payload["error"].lower()


# ── POST /api/preferences ───────────────────────────────────────────


def _prefs_body(email="user@example.com", categories=None, regions=None):
    return {
        "mlt_email": email, "mlt_ts": "1", "mlt_sig": "sig",
        "categories": categories or [], "regions": regions or [],
    }


def test_saving_valid_selections_persists_them(store, verified_prefs_link):
    status, payload = call(
        "/api/preferences", "POST",
        body=_prefs_body(categories=["ai"], regions=["dc", "va"]))
    assert status == 200
    assert payload["categories"] == ["ai"]
    assert payload["regions"] == ["dc", "va"]
    saved = db.get_subscriber_preferences("user@example.com")
    assert saved["categories"] == ["ai"]
    assert saved["regions"] == ["dc", "va"]


def test_saving_an_empty_selection_clears_preferences_to_unfiltered(store, verified_prefs_link):
    db.put_subscriber_preferences("user@example.com", ["ai"], ["dc"])
    call("/api/preferences", "POST", body=_prefs_body(categories=[], regions=[]))
    saved = db.get_subscriber_preferences("user@example.com")
    assert saved["categories"] == []
    assert saved["regions"] == []


def test_a_nonexistent_category_slug_is_silently_dropped(store, verified_prefs_link):
    status, payload = call(
        "/api/preferences", "POST",
        body=_prefs_body(categories=["ai", "not-a-real-category"], regions=[]))
    assert status == 200
    assert payload["categories"] == ["ai"]


def test_a_nonexistent_region_slug_is_silently_dropped(store, verified_prefs_link):
    status, payload = call(
        "/api/preferences", "POST",
        body=_prefs_body(categories=[], regions=["dc", "not-a-real-region"]))
    assert status == 200
    assert payload["regions"] == ["dc"]


def test_duplicate_slugs_are_deduped(store, verified_prefs_link):
    status, payload = call(
        "/api/preferences", "POST",
        body=_prefs_body(categories=["ai", "ai", "ai"], regions=[]))
    assert status == 200
    assert payload["categories"] == ["ai"]


def test_an_invalid_token_is_refused_on_save(store):
    status, _ = call(
        "/api/preferences", "POST",
        body={"mlt_email": "user@example.com", "mlt_ts": "1", "mlt_sig": "bogus",
              "categories": ["ai"], "regions": []})
    assert status == 401
    assert db.get_subscriber_preferences("user@example.com") is None


def test_saving_does_not_touch_ses_subscription_status(store, verified_prefs_link, monkeypatch):
    # This route must be scoped to preferences only — never call out to SES.
    called = []
    monkeypatch.setattr("db.subscribe_to_newsletter", lambda *a, **k: called.append(a))
    call("/api/preferences", "POST", body=_prefs_body(categories=["ai"]))
    assert called == []
