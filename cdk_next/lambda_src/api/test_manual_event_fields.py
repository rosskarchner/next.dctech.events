"""Tests for update_manual_event_fields — the direct (non-overlay) end_date
patch that backs the /edit UI's multi-day control.

Not the overlay system: date/end_date are OVERLAY_PROTECTED_FIELDS on
purpose (see test_overlay.py), because they are structural, not
presentational. This is the same "correct what the author got wrong" idea
mcp/server.py's update_single_event already offers agents, narrowed to
end_date only and reachable over HTTP for a moderator's Cognito session.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_manual_event_fields.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    events = {
        "man1": {"guid": "man1", "title": "Manual Event", "date": "2026-09-11",
                 "time": "12:00", "source": "manual"},
        "sub1": {"guid": "sub1", "title": "Submitted Event", "date": "2026-09-12",
                 "time": "12:00", "source": "submitted"},
        "ical1": {"guid": "ical1", "title": "Feed Event", "date": "2026-09-10",
                  "time": "18:00", "source": "ical", "group": "Rust DC"},
    }

    def _update(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        events[guid].update(data)

    monkeypatch.setattr(db, "get_event_from_config",
                        lambda g: dict(events[g]) if g in events else None)
    monkeypatch.setattr(db, "update_event", _update)
    return events


def test_sets_end_date_on_a_manual_event(store):
    updated = db.update_manual_event_fields("man1", {"end_date": "2026-09-13"})
    assert updated["end_date"] == "2026-09-13"
    assert store["man1"]["end_date"] == "2026-09-13"


def test_sets_end_date_on_a_submitted_event(store):
    updated = db.update_manual_event_fields("sub1", {"end_date": "2026-09-14"})
    assert updated["end_date"] == "2026-09-14"


def test_clearing_end_date_reverts_to_single_day(store):
    db.update_manual_event_fields("man1", {"end_date": "2026-09-13"})
    updated = db.update_manual_event_fields("man1", {"end_date": ""})
    assert updated["end_date"] == ""


def test_ical_events_are_refused(store):
    with pytest.raises(ValueError, match="iCal event"):
        db.update_manual_event_fields("ical1", {"end_date": "2026-09-11"})


def test_missing_event_is_refused(store):
    with pytest.raises(ValueError, match="No such event"):
        db.update_manual_event_fields("nope", {"end_date": "2026-09-13"})


def test_end_date_before_the_start_date_is_refused(store):
    with pytest.raises(ValueError, match="cannot be before"):
        db.update_manual_event_fields("man1", {"end_date": "2026-09-10"})


def test_end_date_equal_to_the_start_date_is_allowed(store):
    # Not multi-day, but not an error either — a moderator clearing a stray
    # end_date back to the event's own date should not be refused.
    updated = db.update_manual_event_fields("man1", {"end_date": "2026-09-11"})
    assert updated["end_date"] == "2026-09-11"


def test_a_malformed_date_is_refused(store):
    with pytest.raises(ValueError, match="ISO-8601"):
        db.update_manual_event_fields("man1", {"end_date": "September 13"})


def test_an_unknown_field_is_refused(store):
    with pytest.raises(ValueError, match="Not an editable field here"):
        db.update_manual_event_fields("man1", {"title": "New title"})
