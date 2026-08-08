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


class FakeTable:
    """In-memory stand-in for the DynamoDB table."""

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
