"""Tests for the two-week re-check tools (next_dctech_events-ba5).

Run: python -m pytest test_recheck_tools.py
"""
import hashlib
import json
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import db  # noqa: E402
import server  # noqa: E402

due = server.list_events_due_for_check
check = server.check_event_source
mark = server.mark_events_checked

QUANTUM_URL = "https://www.meetup.com/washington-quantum-computing-meetup/events/316452554/"


def _page(ld):
    return ('<html><head><script type="application/ld+json">'
            f'{json.dumps(ld)}</script></head></html>')


ONLINE = {
    "@type": "Event",
    "name": "High-Rate and Computationally-Efficient Seedless Extractors",
    "startDate": "2026-10-12T10:00:00-04:00",
    "eventStatus": "https://schema.org/EventScheduled",
    "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
    "location": {"@type": "VirtualLocation", "url": QUANTUM_URL},
}

QUANTUM = {
    "guid": "40f4", "source": "ical", "url": QUANTUM_URL,
    "title": "High-Rate and Computationally-Efficient Seedless Extractors",
    "date": "2026-10-12", "time": "10:00",
    "location": "Washington, DC", "location_type": "physical",
}


class _Resp:
    def __init__(self, text, status_code=200, url=None):
        self.text, self.status_code, self.url = text, status_code, url


@pytest.fixture
def cache_writes(monkeypatch):
    writes = []
    monkeypatch.setattr(server.db, "put_jsonld_cache_entry",
                        lambda h, e: writes.append((h, e)))
    return writes


def _serve(monkeypatch, event, resp):
    monkeypatch.setattr(server.db, "get_event_from_config",
                        lambda g: dict(event) if g == event["guid"] else None)
    calls = []

    def _get(url, **kw):
        calls.append(url)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(server, "safe_get", _get)
    return calls


# ── list_events_due_for_check ────────────────────────────────────────

def test_due_list_filters_and_counts(monkeypatch):
    events = [
        {"guid": "never", "url": "u", "date": "2026-10-13", "source": "ical"},
        {"guid": "stale", "url": "u", "date": "2026-10-14", "source": "manual",
         "last_checked": "2026-01-01T00:00:00Z"},
        {"guid": "fresh", "url": "u", "date": "2026-10-15", "source": "ical",
         "last_checked": "2999-01-01T00:00:00Z"},
        {"guid": "hidden", "url": "u", "date": "2026-10-16", "hidden": True},
        {"guid": "dup", "url": "u", "date": "2026-10-16",
         "overrides": {"duplicate_of": "never"}},
        {"guid": "overlay-hidden", "url": "u", "date": "2026-10-16",
         "overrides": {"hidden": True}},
        {"guid": "no-url", "date": "2026-10-16"},
    ]
    monkeypatch.setattr(server.db, "get_all_events", lambda: events)

    result = due()
    assert [e["guid"] for e in result["events"]] == ["never", "stale"]
    assert result["due_total"] == 2
    assert "last_checked" in result["events"][1]

    limited = due(limit=1)
    assert [e["guid"] for e in limited["events"]] == ["never"]
    assert limited["due_total"] == 2

    assert [e["guid"] for e in due(source="manual")["events"]] == ["stale"]


# ── check_event_source ───────────────────────────────────────────────

def test_quantum_meetup_reads_as_virtual_and_refreshes_cache(monkeypatch, cache_writes):
    _serve(monkeypatch, QUANTUM, _Resp(_page(ONLINE), url=QUANTUM_URL))

    result = check("40f4")

    assert result["http_status"] == 200
    assert result["page"]["is_virtual"] is True
    assert result["diffs"] == [
        {"field": "location_type", "stored": "physical", "page": "virtual"}]
    assert result["cache_refreshed"] is True
    (url_hash, entry), = cache_writes
    assert url_hash == hashlib.md5(QUANTUM_URL.encode()).hexdigest()
    assert entry["is_virtual"] is True and entry["fetched_at"]


def test_page_silent_on_format_is_not_a_location_type_diff(monkeypatch, cache_writes):
    # AFCEA Bethesda's summit pages: a name-only Event block, no location or
    # attendance mode, for an event held in a hotel ballroom
    # (next_dctech_events-ahp).
    _serve(monkeypatch, QUANTUM, _Resp(_page({"@type": "Event", "name": QUANTUM["title"]})))

    result = check("40f4")

    assert result["page"]["is_virtual"] is False
    assert result["diffs"] == []


def test_overlay_already_fixing_it_is_not_a_diff(monkeypatch, cache_writes):
    event = {**QUANTUM, "overrides": {"location_type": "virtual",
                                      "title": "Seedless Extractors (polished)",
                                      "_comment": "x"}}
    _serve(monkeypatch, event, _Resp(_page(ONLINE)))

    result = check("40f4")

    # location_type compares against the overlay, title against the source.
    assert result["diffs"] == []
    assert result["stored"]["title"] == "Seedless Extractors (polished)"


def test_cancelled_moved_and_retitled(monkeypatch, cache_writes):
    page = {**ONLINE, "name": "Seedless Extractors (new title)",
            "eventStatus": "https://schema.org/EventCancelled",
            "startDate": "2026-10-19T18:00:00Z",
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "location": {"@type": "Place", "name": "Venue",
                         "address": {"addressLocality": "Washington"}}}
    _serve(monkeypatch, QUANTUM, _Resp(_page(page)))

    fields = {d["field"]: d["page"] for d in check("40f4")["diffs"]}

    assert fields == {"cancelled": True, "title": "Seedless Extractors (new title)",
                      "date": "2026-10-19", "time": "14:00"}


def test_postponed_is_reported(monkeypatch, cache_writes):
    _serve(monkeypatch, QUANTUM, _Resp(_page(
        {**ONLINE, "eventStatus": "https://schema.org/EventPostponed"})))
    fields = [d["field"] for d in check("40f4")["diffs"]]
    assert "event_status" in fields


def test_gone_page_and_fetch_errors_write_no_cache(monkeypatch, cache_writes):
    _serve(monkeypatch, QUANTUM, _Resp("not found", status_code=404))
    gone = check("40f4")
    assert gone["diffs"] == [{"field": "page_gone", "stored": None, "page": 404}]
    assert gone["cache_refreshed"] is False

    _serve(monkeypatch, QUANTUM, ConnectionError("blocked"))
    failed = check("40f4")
    assert "blocked" in failed["error"]

    # 200 but no Event JSON-LD (a bot interstitial): never overwrite the cache.
    _serve(monkeypatch, QUANTUM, _Resp("<html>are you human?</html>"))
    assert check("40f4")["page"] is None
    assert cache_writes == []


@pytest.mark.parametrize("event", [
    {**QUANTUM, "source": "manual"},
    {**QUANTUM, "url": "https://www.eventbrite.com/e/1"},
    {**QUANTUM, "overrides": {"url": "https://www.meetup.com/other/events/2/"}},
])
def test_cache_written_only_for_entries_the_aggregator_reads(monkeypatch, cache_writes, event):
    _serve(monkeypatch, event, _Resp(_page(ONLINE)))
    assert check("40f4")["cache_refreshed"] is False
    assert cache_writes == []


def test_unknown_guid_404s(monkeypatch):
    monkeypatch.setattr(server.db, "get_event_from_config", lambda g: None)
    with pytest.raises(Exception):
        check("nope")


# ── mark_events_checked ──────────────────────────────────────────────

def test_mark_validates_before_writing(monkeypatch):
    writes = []
    monkeypatch.setattr(server.db, "mark_event_checked",
                        lambda *a: writes.append(a) or True)
    with pytest.raises(ValueError):
        mark([{"guid": "a", "result": "ok"}, {"guid": "b", "result": "looks fine"}])
    assert writes == []


def test_mark_reports_missing(monkeypatch):
    monkeypatch.setattr(server.db, "mark_event_checked",
                        lambda guid, result, note: guid != "gone")
    result = mark([{"guid": "a", "result": "ok"},
                   {"guid": "gone", "result": "fetch_failed", "note": "403"}])
    assert result == {"marked": ["a"], "missing": ["gone"]}


def test_recheck_tools_are_on_the_scheduled_agent_route():
    # Loaded by path: on this test run's sys.path, `handler` is the api's.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mcp_handler", os.path.join(os.path.dirname(__file__), "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    assert {"list_events_due_for_check", "check_event_source",
            "mark_events_checked"} <= handler.SCHEDULED_AGENT_TOOLS
