"""Tests for magic-link send throttling.

/api/submit-link is unauthenticated and mails whatever address it is handed,
so these cover the difference between "usable" and "open relay for filling a
stranger's inbox".

Run: python -m pytest test_rate_limit.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


from test_sponsorships import FakeTable  # noqa: E402  (honours conditions)


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    return fake


def test_first_request_is_allowed(table):
    allowed, retry = db.check_and_record_link_request("a@example.com", now=1000)
    assert allowed and retry == 0


def test_immediate_second_request_is_throttled(table):
    db.check_and_record_link_request("a@example.com", now=1000)
    allowed, retry = db.check_and_record_link_request("a@example.com", now=1010)

    assert not allowed
    assert 0 < retry <= db.MAGIC_LINK_COOLDOWN_SECONDS


def test_request_after_cooldown_is_allowed(table):
    db.check_and_record_link_request("a@example.com", now=1000)
    now = 1000 + db.MAGIC_LINK_COOLDOWN_SECONDS
    allowed, _ = db.check_and_record_link_request("a@example.com", now=now)
    assert allowed


def test_daily_cap_stops_inbox_flooding(table):
    now = 1000
    for i in range(db.MAGIC_LINK_MAX_PER_DAY):
        allowed, _ = db.check_and_record_link_request("v@example.com", now=now)
        assert allowed, f"send {i + 1} should be allowed"
        now += db.MAGIC_LINK_COOLDOWN_SECONDS

    # Cooldown satisfied, but the daily allowance is gone.
    allowed, retry = db.check_and_record_link_request("v@example.com", now=now)
    assert not allowed
    assert retry > 0


def test_allowance_resets_after_a_day(table):
    now = 1000
    for _ in range(db.MAGIC_LINK_MAX_PER_DAY):
        db.check_and_record_link_request("v@example.com", now=now)
        now += db.MAGIC_LINK_COOLDOWN_SECONDS

    assert not db.check_and_record_link_request("v@example.com", now=now)[0]
    allowed, _ = db.check_and_record_link_request("v@example.com", now=1000 + 86400)
    assert allowed


def test_limits_are_per_address(table):
    db.check_and_record_link_request("a@example.com", now=1000)
    # A throttled address must not throttle everyone else.
    allowed, _ = db.check_and_record_link_request("b@example.com", now=1001)
    assert allowed


def test_throttled_request_does_not_consume_allowance(table):
    db.check_and_record_link_request("a@example.com", now=1000)
    for t in range(1001, 1010):
        db.check_and_record_link_request("a@example.com", now=t)

    # Only the first send counted, so the rest of the day is still available.
    now = 1000 + db.MAGIC_LINK_COOLDOWN_SECONDS
    for _ in range(db.MAGIC_LINK_MAX_PER_DAY - 1):
        allowed, _ = db.check_and_record_link_request("a@example.com", now=now)
        assert allowed
        now += db.MAGIC_LINK_COOLDOWN_SECONDS


def test_record_carries_a_ttl_beyond_the_window(table):
    db.check_and_record_link_request("a@example.com", now=1000)
    item = table.items[("MAGICLINK#a@example.com", "META")]
    # TTL must outlive the counting window, or expiry silently resets the cap.
    assert item["ttl"] > item["window_start"] + 86400


# ─── Public write-path rate limiting (submissions/corrections) ────

def test_write_first_request_is_allowed(table):
    allowed, retry = db.check_and_record_write("magiclink:a@example.com", "submission", now=1000)
    assert allowed and retry == 0


def test_write_cap_stops_token_replay_spam(table):
    now = 1000
    for i in range(db.WRITE_RATE_LIMIT_MAX_PER_WINDOW):
        allowed, _ = db.check_and_record_write("magiclink:a@example.com", "submission", now=now)
        assert allowed, f"write {i + 1} should be allowed"

    allowed, retry = db.check_and_record_write("magiclink:a@example.com", "submission", now=now)
    assert not allowed
    assert retry > 0


def test_write_allowance_resets_after_the_window(table):
    now = 1000
    for _ in range(db.WRITE_RATE_LIMIT_MAX_PER_WINDOW):
        db.check_and_record_write("magiclink:a@example.com", "submission", now=now)

    assert not db.check_and_record_write("magiclink:a@example.com", "submission", now=now)[0]
    allowed, _ = db.check_and_record_write(
        "magiclink:a@example.com", "submission", now=now + db.WRITE_RATE_LIMIT_WINDOW_SECONDS)
    assert allowed


def test_write_limits_are_per_submitter(table):
    for _ in range(db.WRITE_RATE_LIMIT_MAX_PER_WINDOW):
        db.check_and_record_write("magiclink:a@example.com", "submission", now=1000)

    # A throttled submitter must not throttle everyone else.
    allowed, _ = db.check_and_record_write("magiclink:b@example.com", "submission", now=1000)
    assert allowed


def test_write_limits_are_per_action(table):
    """A submitter maxed out on submissions can still file a correction —
    they're different abuse surfaces with their own budgets, not one shared
    pool that would let one endpoint's spam lock out the other."""
    for _ in range(db.WRITE_RATE_LIMIT_MAX_PER_WINDOW):
        db.check_and_record_write("magiclink:a@example.com", "submission", now=1000)

    allowed, _ = db.check_and_record_write("magiclink:a@example.com", "correction", now=1000)
    assert allowed


def test_write_record_carries_a_ttl_beyond_the_window(table):
    db.check_and_record_write("magiclink:a@example.com", "submission", now=1000)
    item = table.items[("WRITERATE#submission#magiclink:a@example.com", "META")]
    assert item["ttl"] > item["window_start"] + db.WRITE_RATE_LIMIT_WINDOW_SECONDS


def test_write_concurrent_requests_cannot_overshoot_the_cap(table, monkeypatch):
    """A request that read the counter just before another request bumped it
    must not write a stale count back: that was how a burst got through."""
    who = "ip:203.0.113.9"
    for _ in range(db.WRITE_RATE_LIMIT_MAX_PER_WINDOW - 1):
        db.check_and_record_write(who, "sponsorship", now=1000)

    real_get = table.get_item
    raced = []

    def get_then_lose_the_race(Key):
        response = real_get(Key)
        if not raced:
            raced.append(True)
            # A concurrent request takes the last slot between our read and write.
            db.check_and_record_write(who, "sponsorship", now=1000)
        return response

    monkeypatch.setattr(table, "get_item", get_then_lose_the_race)
    allowed, retry = db.check_and_record_write(who, "sponsorship", now=1000)
    assert not allowed and retry > 0
    item = table.items[(f"WRITERATE#sponsorship#{who}", "META")]
    assert item["count"] == db.WRITE_RATE_LIMIT_MAX_PER_WINDOW


def test_write_limit_can_be_set_per_call(table):
    for _ in range(3):
        assert db.check_and_record_write("ip:x", "sponsorship_hold", now=1000, limit=3)[0]
    assert not db.check_and_record_write("ip:x", "sponsorship_hold", now=1000, limit=3)[0]
