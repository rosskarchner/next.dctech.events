"""Tests for the new-event social queue worker.

Run: python -m pytest test_app.py

event_utils.py and networks.py are only copied alongside app.py at build
time (build_lambdas.sh) — for local testing, pull them straight from their
source directories instead of requiring a build first. event_utils.py comes
from packages/calgen, not lambda_src/api: the api Lambdas' own event_utils.py
is a stripped-down stub with only calculate_event_hash, not slugify/
event_slug/has_specific_time.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("MASTODON_SECRET_NAME", "test/mastodon")
os.environ.setdefault("BLUESKY_SECRET_NAME", "test/bluesky")
os.environ.setdefault("SITE_BASE_URL", "https://dctech.events")

# Appended, not inserted at 0: this directory's own app.py (already first on
# sys.path via pytest's rootdir insertion) must still win an `import app`.
sys.path.append(os.path.join(os.path.dirname(__file__),
                              "..", "..", "..", "packages", "calgen", "src", "calgen"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "social_publisher"))

import app  # noqa: E402


def _event(**overrides):
    item = {
        "guid": "a1b2c3d4e5f6",
        "title": "DC Python Monthly Meetup",
        "date": "2026-10-01",
        "time": "18:30",
    }
    item.update(overrides)
    return item


# ── composing ────────────────────────────────────────────────────────


def test_format_when_includes_a_specific_time():
    assert app._format_when(_event()) == "Thursday, October 1, 2026 at 6:30 PM"


def test_format_when_omits_the_all_day_sentinel():
    assert app._format_when(_event(time="00:00")) == "Thursday, October 1, 2026"


def test_format_when_omits_a_missing_time():
    event = _event()
    del event["time"]
    assert app._format_when(event) == "Thursday, October 1, 2026"


def test_compose_includes_title_when_and_group():
    url = "https://dctech.events/events/2026-10-01-dc-python-monthly-meetup-a1b2c3d4/"
    text = app.compose_event_post(_event(group="DC Python"), url, 500)
    assert text == (
        "DC Python Monthly Meetup\n\n"
        "Thursday, October 1, 2026 at 6:30 PM\n"
        "Hosted by DC Python\n\n" + url
    )


def test_compose_omits_group_when_absent():
    # Manual/submitted events carry no group field at all.
    url = "https://dctech.events/events/2026-10-01-dc-python-monthly-meetup-a1b2c3d4/"
    text = app.compose_event_post(_event(), url, 500)
    assert "Hosted by" not in text
    assert text.endswith(url)


def test_compose_drops_detail_before_the_link():
    url = "https://dctech.events/events/2026-10-01-dc-python-monthly-meetup-a1b2c3d4/"
    text = app.compose_event_post(
        _event(title="T" * 240, group="G" * 200), url, app.networks.BLUESKY_CHAR_LIMIT
    )
    assert len(text) <= app.networks.BLUESKY_CHAR_LIMIT
    assert text.endswith(url)


def test_compose_never_truncates_the_url():
    url = "https://dctech.events/events/2026-10-01-dc-python-monthly-meetup-a1b2c3d4/"
    text = app.compose_event_post(_event(title="word " * 200), url, 100)
    assert len(text) <= 100
    assert text.endswith(url)


# ── past-event check ─────────────────────────────────────────────────


def test_future_timed_event_is_not_past():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert app._is_past(_event(date="2026-10-01", time="18:30"), now=now) is False


def test_a_timed_event_earlier_today_is_past():
    now = datetime(2026, 10, 1, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    assert app._is_past(_event(date="2026-10-01", time="18:30"), now=now) is True


def test_an_all_day_event_today_is_not_past():
    now = datetime(2026, 10, 1, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    assert app._is_past(_event(date="2026-10-01", time="00:00"), now=now) is False


def test_an_all_day_event_yesterday_is_past():
    now = datetime(2026, 10, 1, 1, 0, tzinfo=ZoneInfo("America/New_York"))
    assert app._is_past(_event(date="2026-09-30", time="00:00"), now=now) is True


def test_an_event_with_no_date_is_treated_as_past():
    event = _event()
    del event["date"]
    assert app._is_past(event) is True


# ── the queue drain, against a fake table ───────────────────────────


def _split_top_level(text, sep=","):
    """str.split(sep), but ignoring `sep` inside parentheses."""
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


class _FakeTable:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def _key(self, k):
        return (k["PK"], k["SK"])

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": item} if item else {}

    def query(self, IndexName, KeyConditionExpression, ScanIndexForward=True, Limit=None):
        pending = [i for i in self.items.values() if i.get("GSI1PK") == "SOCIALQUEUE#PENDING"]
        pending.sort(key=lambda i: i.get("GSI1SK", ""), reverse=not ScanIndexForward)
        return {"Items": pending[:Limit] if Limit else pending}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                     ExpressionAttributeValues=None, ReturnValues=None):
        item = self.items.setdefault(self._key(Key), dict(Key))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        if "REMOVE" in UpdateExpression:
            set_part, remove_part = UpdateExpression.split("REMOVE", 1)
            for attr in remove_part.split(","):
                attr = attr.strip()
                item.pop(names.get(attr, attr), None)
        else:
            set_part = UpdateExpression

        set_part = set_part.replace("SET", "", 1).strip()
        if set_part:
            for assignment in _split_top_level(set_part):
                assignment = assignment.strip()
                if not assignment:
                    continue
                if "if_not_exists" in assignment:
                    left = assignment.split("=")[0].strip()
                    field = names.get(left, left)
                    current = item.get(field, values[":zero"])
                    item[field] = current + values[":one"]
                else:
                    left, right = assignment.split("=")
                    field = names.get(left.strip(), left.strip())
                    item[field] = values[right.strip()]

        return {"Attributes": item}


def _queued(guid, **overrides):
    item = {
        "PK": f"SOCIALQUEUE#{guid}", "SK": "META",
        "GSI1PK": "SOCIALQUEUE#PENDING", "GSI1SK": "2026-09-01T00:00:00Z",
        "event_guid": guid, "status": "pending", "attempts": 0,
    }
    item.update(overrides)
    return item


def test_process_one_returns_empty_when_the_queue_is_empty():
    table = _FakeTable()
    assert app._process_one(table) == {"status": "empty"}


def test_process_one_skips_a_deleted_event():
    table = _FakeTable({("SOCIALQUEUE#abc", "META"): _queued("abc")})
    result = app._process_one(table)
    assert result["status"] == "skipped"
    assert result["reason"] == "event no longer exists"
    assert "GSI1PK" not in table.items[("SOCIALQUEUE#abc", "META")]


def test_process_one_skips_a_past_event(monkeypatch):
    monkeypatch.setattr(app, "_is_past", lambda event, now=None: True)
    table = _FakeTable({
        ("SOCIALQUEUE#abc", "META"): _queued("abc"),
        ("EVENT#abc", "META"): {"PK": "EVENT#abc", "SK": "META", **_event()},
    })
    result = app._process_one(table)
    assert result["status"] == "skipped"
    assert result["reason"] == "event date has passed"
    assert table.items[("SOCIALQUEUE#abc", "META")]["status"] == "skipped"


def test_process_one_posts_to_both_networks(monkeypatch):
    monkeypatch.setattr(app, "_is_past", lambda event, now=None: False)
    monkeypatch.setattr(app.networks, "mastodon_post",
                         lambda secret, text, idempotency_key=None:
                         {"id": "1", "url": "https://dmv.community/@techevents/1"})
    monkeypatch.setattr(app.networks, "bluesky_post",
                         lambda secret, text, **kw:
                         {"uri": "at://did/app.bsky.feed.post/1",
                          "cid": "c", "url": "https://bsky.app/x"})
    monkeypatch.setattr(app, "_secret", lambda name: {})

    table = _FakeTable({
        ("SOCIALQUEUE#abc", "META"): _queued("abc"),
        ("EVENT#abc", "META"): {"PK": "EVENT#abc", "SK": "META", **_event()},
    })
    result = app._process_one(table)
    assert result["status"] == "posted"
    item = table.items[("SOCIALQUEUE#abc", "META")]
    assert item["status"] == "posted"
    assert item["mastodon_id"] == "1"
    assert item["bluesky_uri"] == "at://did/app.bsky.feed.post/1"
    assert "GSI1PK" not in item


def test_process_one_retries_only_the_network_that_failed(monkeypatch):
    monkeypatch.setattr(app, "_is_past", lambda event, now=None: False)
    monkeypatch.setattr(app.networks, "bluesky_post",
                         lambda secret, text, **kw:
                         {"uri": "at://did/app.bsky.feed.post/1",
                          "cid": "c", "url": "https://bsky.app/x"})
    monkeypatch.setattr(app, "_secret", lambda name: {})

    table = _FakeTable({
        # Mastodon already succeeded on a previous hourly attempt.
        ("SOCIALQUEUE#abc", "META"): _queued("abc", mastodon_id="1",
                                              mastodon_url="https://dmv.community/@techevents/1"),
        ("EVENT#abc", "META"): {"PK": "EVENT#abc", "SK": "META", **_event()},
    })
    result = app._process_one(table)
    assert result["status"] == "posted"


def test_process_one_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(app, "_is_past", lambda event, now=None: False)

    def _boom(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(app.networks, "mastodon_post", _boom)
    monkeypatch.setattr(app.networks, "bluesky_post", _boom)
    monkeypatch.setattr(app, "_secret", lambda name: {})

    table = _FakeTable({
        ("SOCIALQUEUE#abc", "META"): _queued("abc", attempts=app.MAX_ATTEMPTS - 1),
        ("EVENT#abc", "META"): {"PK": "EVENT#abc", "SK": "META", **_event()},
    })
    result = app._process_one(table)
    assert result["status"] == "failed"
    item = table.items[("SOCIALQUEUE#abc", "META")]
    assert item["status"] == "failed"
    assert "GSI1PK" not in item
