"""Tests for magic-link submission tokens.

The KMS calls are faked with a deterministic local HMAC so the crypto contract
(what is signed, what invalidates a signature) is exercised without AWS.

Run: python -m pytest test_magic_link.py
"""
import hashlib
import hmac as _hmac
import os
from base64 import urlsafe_b64encode

import pytest

os.environ.setdefault("SUBMIT_KEY_ID", "test-key")
os.environ.setdefault("BASE_URL", "https://dctech.events")

import magic_link  # noqa: E402

SECRET = b"test-secret"


class FakeKms:
    """Deterministic stand-in for KMS HMAC."""

    def __init__(self):
        self.key = SECRET

    def generate_mac(self, Message, KeyId, MacAlgorithm):
        return {"Mac": _hmac.new(self.key, Message, hashlib.sha512).digest()}

    def verify_mac(self, Message, KeyId, MacAlgorithm, Mac):
        expected = _hmac.new(self.key, Message, hashlib.sha512).digest()
        return {"MacValid": _hmac.compare_digest(expected, Mac)}


@pytest.fixture(autouse=True)
def fake_kms(monkeypatch):
    monkeypatch.setattr(magic_link, "kms", FakeKms())
    monkeypatch.setattr(magic_link, "SUBMIT_KEY_ID", "test-key")


# ── Email validation ───────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "a@b.co", "First.Last+tag@example.org", "x@sub.domain.example.com",
])
def test_valid_emails(email):
    assert magic_link.is_valid_email(email)


@pytest.mark.parametrize("email", [
    "", "   ", "nope", "no@domain", "@example.com", "a b@example.com",
    "two@@example.com", None, "a@" + "x" * 300 + ".com",
])
def test_invalid_emails(email):
    assert not magic_link.is_valid_email(email)


def test_email_is_normalized_to_lowercase():
    assert magic_link.normalize_email("  Foo@Example.COM ") == "foo@example.com"


# ── Round trip ─────────────────────────────────────────────────────

def test_token_round_trip():
    ts, sig = magic_link.generate_token("user@example.com")
    ok, reason = magic_link.verify_token("user@example.com", ts, sig)
    assert ok and reason is None


def test_token_is_case_insensitive_on_email():
    ts, sig = magic_link.generate_token("User@Example.com")
    assert magic_link.verify_token("user@example.com", ts, sig)[0]


# ── Tampering ──────────────────────────────────────────────────────

def test_token_for_one_email_does_not_work_for_another():
    ts, sig = magic_link.generate_token("victim@example.com")
    ok, _ = magic_link.verify_token("attacker@example.com", ts, sig)
    assert not ok


def test_editing_the_timestamp_invalidates_the_signature():
    # The timestamp is inside the signed message, so a client cannot extend
    # its own token's life by bumping the value in the query string.
    ts, sig = magic_link.generate_token("user@example.com")
    ok, _ = magic_link.verify_token("user@example.com", ts + 1, sig)
    assert not ok


def test_garbage_signature_is_rejected():
    ts, _ = magic_link.generate_token("user@example.com")
    for bad in ["", "!!!!", "abc", urlsafe_b64encode(b"x" * 64).decode()]:
        assert not magic_link.verify_token("user@example.com", ts, bad)[0]


# ── Expiry ─────────────────────────────────────────────────────────

def test_expired_token_is_rejected_with_a_useful_message():
    import time as _time
    old = int(_time.time()) - magic_link.TOKEN_TTL_SECONDS - 10
    ts, sig = magic_link.generate_token("user@example.com", timestamp=old)
    ok, reason = magic_link.verify_token("user@example.com", ts, sig)
    assert not ok
    assert "expired" in reason.lower()


def test_token_just_inside_the_window_still_works():
    import time as _time
    recent = int(_time.time()) - magic_link.TOKEN_TTL_SECONDS + 60
    ts, sig = magic_link.generate_token("user@example.com", timestamp=recent)
    assert magic_link.verify_token("user@example.com", ts, sig)[0]


def test_far_future_timestamp_is_rejected():
    import time as _time
    future = int(_time.time()) + 86400
    ts, sig = magic_link.generate_token("user@example.com", timestamp=future)
    ok, _ = magic_link.verify_token("user@example.com", ts, sig)
    assert not ok


def test_non_numeric_timestamp_is_rejected():
    ts, sig = magic_link.generate_token("user@example.com")
    assert not magic_link.verify_token("user@example.com", "abc", sig)[0]


# ── Link building / parsing ────────────────────────────────────────

def test_build_link_round_trips_through_decode():
    ts, sig = magic_link.generate_token("user@example.com")
    link = magic_link.build_link("user@example.com", ts, sig)

    assert link.startswith("https://dctech.events/edit/submit-event.html?")
    from urllib.parse import parse_qs, urlparse
    params = parse_qs(urlparse(link).query)
    assert magic_link.decode_email_param(params["e"][0]) == "user@example.com"
    assert params["t"][0] == str(ts)
    assert params["s"][0] == sig


def test_decode_email_param_tolerates_garbage():
    assert magic_link.decode_email_param("!!!not-base64!!!") == ""
    assert magic_link.decode_email_param("") == ""
    assert magic_link.decode_email_param(None) == ""


def test_token_from_request_reads_short_and_long_field_names():
    ts, sig = magic_link.generate_token("user@example.com")
    encoded = urlsafe_b64encode(b"user@example.com").decode().rstrip("=")

    short = magic_link.token_from_request(
        {"mlt_e": encoded, "mlt_t": ts, "mlt_s": sig})
    assert short == ("user@example.com", ts, sig)

    long = magic_link.token_from_request(
        {"mlt_email": "User@Example.com", "mlt_ts": ts, "mlt_sig": sig})
    assert long == ("user@example.com", ts, sig)


def test_token_from_request_returns_blanks_when_absent():
    assert magic_link.token_from_request({}) == ("", "", "")


def test_unconfigured_key_refuses_to_verify(monkeypatch):
    monkeypatch.setattr(magic_link, "SUBMIT_KEY_ID", "")
    ok, reason = magic_link.verify_token("user@example.com", 123, "sig")
    assert not ok and "configured" in reason.lower()
