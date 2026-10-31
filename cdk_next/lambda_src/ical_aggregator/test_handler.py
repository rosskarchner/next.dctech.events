"""Tests for the aggregator's handling of re-check state (next_dctech_events-ba5).

Run from packages/calgen's environment (the aggregator imports calgen):
  uv run --project packages/calgen --with boto3 --with pytest \
      python -m pytest cdk_next/lambda_src/ical_aggregator/test_handler.py
"""
import json
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
sys.path.insert(0, os.path.dirname(__file__))

import handler  # noqa: E402

GROUP = {"id": "grp", "name": "Group", "website": "https://g", "categories": ["quantum"]}
EVENT = {"title": "Talk", "date": "2999-01-01", "time": "18:00",
         "url": "https://www.meetup.com/g/events/1/", "location": "DC",
         "location_type": "physical"}


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    state = {"puts": [], "existing": {}}
    monkeypatch.setattr(handler, "ICAL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(handler.db, "get_ical_cache", lambda gid: {"events": [EVENT]})
    monkeypatch.setattr(handler.db, "get_event_from_config",
                        lambda guid: state["existing"].get("only"))
    monkeypatch.setattr(handler.db, "put_event",
                        lambda guid, data, **kw: state["puts"].append(data))
    return state


def _existing(**extra):
    return {**EVENT, "group": "Group", "group_id": "grp", "group_website": "https://g",
            "categories": ["quantum"], "source": "ical", "review_status": "approved",
            "createdAt": "2026-09-07T00:00:00Z", **extra}


def test_refresh_keeps_recheck_stamps(fake_db):
    fake_db["existing"]["only"] = _existing(
        location_type="virtual",  # pretend the feed changed this field
        last_checked="2026-10-12T00:00:00Z", last_check_result="refreshed",
        last_check_note="moved online")

    handler._persist_group(GROUP, [EVENT])

    (written,) = fake_db["puts"]
    assert written["last_checked"] == "2026-10-12T00:00:00Z"
    assert written["last_check_result"] == "refreshed"
    assert written["last_check_note"] == "moved online"


def test_unchanged_event_with_stamps_is_not_rewritten(fake_db):
    fake_db["existing"]["only"] = _existing(
        last_checked="2026-10-12T00:00:00Z", last_check_result="ok")

    _, writes = handler._persist_group(GROUP, [EVENT])

    assert fake_db["puts"] == []
    assert writes == 0


class _Table:
    def __init__(self, item):
        self.item, self.put = item, None

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}

    def put_item(self, Item):
        self.put = Item


def test_jsonld_persist_keeps_the_newer_read(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "JSON_LD_CACHE_DIR", str(tmp_path))
    # This run's copy, seeded before check_event_source refreshed "q".
    (tmp_path / "q.json").write_text(json.dumps(
        {"is_virtual": False, "cancelled": False}))
    (tmp_path / "new.json").write_text(json.dumps(
        {"is_virtual": False, "cancelled": False, "fetched_at": "2026-10-12T09:00:00Z"}))
    table = _Table({"entries": {
        "q": {"is_virtual": True, "cancelled": False, "fetched_at": "2026-10-12T08:00:00Z"},
    }})
    monkeypatch.setattr(handler.db, "_get_table", lambda: table)

    handler._persist_jsonld_cache()

    entries = handler.db._to_plain(table.put["entries"])
    assert entries["q"]["is_virtual"] is True       # MCP's fresh read survived
    assert entries["new"]["fetched_at"] == "2026-10-12T09:00:00Z"


def test_jsonld_persist_prefers_our_newer_read(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "JSON_LD_CACHE_DIR", str(tmp_path))
    (tmp_path / "q.json").write_text(json.dumps(
        {"is_virtual": False, "cancelled": True, "fetched_at": "2026-10-12T10:00:00Z"}))
    table = _Table({"entries": {
        "q": {"is_virtual": False, "cancelled": False, "fetched_at": "2026-10-12T08:00:00Z"},
    }})
    monkeypatch.setattr(handler.db, "_get_table", lambda: table)

    handler._persist_jsonld_cache()

    assert handler.db._to_plain(table.put["entries"])["q"]["cancelled"] is True


def test_skip_phrases_keep_matching_events_out(fake_db, monkeypatch):
    monkeypatch.setattr(handler.db, "put_ical_cache", lambda *a: None)
    members = {**EVENT, "title": "STATION DC Mafia (Members Only)",
               "url": "https://luma.com/x"}
    group = {**GROUP, "skip_phrases": ["members only"]}

    fresh, _ = handler._persist_group(group, [EVENT, members])

    assert [p["title"] for p in fake_db["puts"]] == ["Talk"]
    # Not in fresh, so _prune_stale_ical_events deletes a row written earlier.
    assert len(fresh) == 1


def test_category_rules_set_stored_categories(fake_db, monkeypatch):
    monkeypatch.setattr(handler.db, "put_ical_cache", lambda *a: None)
    anime = {**EVENT, "title": "Anime Night", "url": "https://www.meetup.com/g/events/2/"}
    group = {**GROUP, "category_rules": [{"phrase": "anime", "categories": ["social"]}]}

    handler._persist_group(group, [EVENT, anime])

    assert [p["categories"] for p in fake_db["puts"]] == [["quantum"], ["social"]]


# ── Today's events stay all day even after the feed drops them ──────

TODAY = "2026-10-31"


def _ev(title, date, time="10:00", **kw):
    return {"title": title, "date": date, "time": time,
            "url": f"https://www.meetup.com/g/events/{title}/", "location": "DC",
            "location_type": "physical", **kw}


@pytest.fixture
def cache_db(monkeypatch, tmp_path):
    state = {"cache": None, "written": None, "puts": []}
    monkeypatch.setattr(handler, "ICAL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(handler, "_local_today", lambda: TODAY)
    monkeypatch.setattr(handler.db, "get_ical_cache", lambda gid: state["cache"])
    monkeypatch.setattr(handler.db, "put_ical_cache",
                        lambda gid, events, meta: state.update(written=events))
    monkeypatch.setattr(handler.db, "get_event_from_config", lambda guid: None)
    monkeypatch.setattr(handler.db, "put_event",
                        lambda guid, data, **kw: state["puts"].append(data["title"]))
    return state


def test_an_event_from_today_missing_from_the_feed_is_carried_and_not_pruned(cache_db):
    morning, evening = _ev("Morning", TODAY), _ev("Evening", TODAY, "19:00")
    cache_db["cache"] = {"events": [morning, evening]}
    fresh, _ = handler._persist_group(GROUP, [evening])  # feed dropped Morning
    assert [e["title"] for e in cache_db["written"]] == ["Evening", "Morning"]
    assert handler._event_key(morning) in fresh  # so _prune_stale_ical_events keeps it


def test_a_future_event_missing_from_the_feed_is_still_dropped(cache_db):
    gone = _ev("Cancelled", "2026-11-05")
    cache_db["cache"] = {"events": [gone]}
    fresh, _ = handler._persist_group(GROUP, [])
    assert cache_db["written"] == []
    assert handler._event_key(gone) not in fresh


def test_a_multiday_event_under_way_is_carried(cache_db):
    conf = _ev("Conference", "2026-10-30", "", end_date="2026-11-01")
    cache_db["cache"] = {"events": [conf]}
    fresh, _ = handler._persist_group(GROUP, [])
    assert cache_db["written"] is None  # carried as-is: the cache is unchanged
    assert handler._event_key(conf) in fresh


def test_yesterdays_event_is_not_carried(cache_db):
    cache_db["cache"] = {"events": [_ev("Yesterday", "2026-10-30")]}
    handler._persist_group(GROUP, [])
    assert cache_db["written"] == []


def test_an_event_still_in_the_feed_is_not_duplicated(cache_db):
    ev = _ev("Morning", TODAY)
    cache_db["cache"] = {"events": [ev]}
    handler._persist_group(GROUP, [ev])
    assert cache_db["written"] is None  # unchanged cache: no write at all


def test_local_today_is_eastern_not_utc(monkeypatch):
    # 01:00 UTC on Nov 1 is still Oct 31 in DC.
    from datetime import datetime as real_dt, timezone

    class FixedNow(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 1, 1, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(handler, "datetime", FixedNow)
    assert handler._local_today() == "2026-10-31"
