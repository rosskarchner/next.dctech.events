"""Tests for the weekly /updates publisher.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_app.py
"""
import os
from datetime import date

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import app  # noqa: E402


@pytest.mark.parametrize("day,expected_start", [
    (date(2026, 8, 3), date(2026, 8, 3)),   # Monday itself
    (date(2026, 8, 6), date(2026, 8, 3)),   # midweek
    (date(2026, 8, 9), date(2026, 8, 3)),   # Sunday, still the same week
])
def test_week_bounds(day, expected_start):
    start, end = app._week_bounds(day)
    assert start == expected_start
    assert end == expected_start.replace(day=expected_start.day + 6)
    assert (end - start).days == 6


def test_week_id_matches_calgen_format():
    assert app._week_id(date(2026, 8, 3)) == "2026-W32"
    # Zero-padded, so it round-trips through calgen's parse_week_identifier.
    assert app._week_id(date(2026, 1, 5)) == "2026-W02"


def test_iso_year_rollover_uses_iso_year_not_calendar_year():
    # 2025-12-29 is a Monday in ISO week 2026-W01.
    assert app._week_id(date(2025, 12, 29)) == "2026-W01"


def test_events_for_week_selects_only_overlapping():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [
        {"title": "Before", "date": "2026-08-02"},
        {"title": "Monday", "date": "2026-08-03"},
        {"title": "Sunday", "date": "2026-08-09"},
        {"title": "After", "date": "2026-08-10"},
    ]
    got = [e["title"] for e in app._events_for_week(events, week_start, week_end)]
    assert got == ["Monday", "Sunday"]


def test_multi_day_event_spanning_into_week_is_included():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [
        # Starts before the week but runs into it.
        {"title": "Conference", "date": "2026-08-01", "end_date": "2026-08-05"},
        # Starts inside the week and runs past it.
        {"title": "Summit", "date": "2026-08-08", "end_date": "2026-08-12"},
        # Entirely before.
        {"title": "Old", "date": "2026-07-20", "end_date": "2026-07-22"},
    ]
    got = [e["title"] for e in app._events_for_week(events, week_start, week_end)]
    assert got == ["Conference", "Summit"]


def test_events_sorted_by_date_then_time():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [
        {"title": "C", "date": "2026-08-05", "time": "09:00"},
        {"title": "B", "date": "2026-08-03", "time": "19:00"},
        {"title": "A", "date": "2026-08-03", "time": "08:00"},
    ]
    got = [e["title"] for e in app._events_for_week(events, week_start, week_end)]
    assert got == ["A", "B", "C"]


def test_event_missing_time_sorts_without_error():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [
        {"title": "Timed", "date": "2026-08-03", "time": "10:00"},
        {"title": "Untimed", "date": "2026-08-03"},
    ]
    got = [e["title"] for e in app._events_for_week(events, week_start, week_end)]
    assert got == ["Untimed", "Timed"]


def test_malformed_events_are_skipped():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [
        {"title": "No date"},
        {"title": "Bad date", "date": "not-a-date"},
        {"title": "Null date", "date": None},
        {"title": "Good", "date": "2026-08-04"},
    ]
    got = [e["title"] for e in app._events_for_week(events, week_start, week_end)]
    assert got == ["Good"]


def test_bad_end_date_falls_back_to_start_date():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [{"title": "Odd", "date": "2026-08-04", "end_date": "garbage"}]
    got = app._events_for_week(events, week_start, week_end)
    assert [e["title"] for e in got] == ["Odd"]


def test_snapshot_keeps_only_known_fields():
    week_start, week_end = date(2026, 8, 3), date(2026, 8, 9)
    events = [{
        "title": "Meetup",
        "date": "2026-08-04",
        "url": "https://example.com",
        "group": "Python DC",
        "internal_note": "should not be snapshotted",
        "guid": "abc123",
    }]
    snapshot = app._events_for_week(events, week_start, week_end)[0]
    assert set(snapshot) == {"title", "date", "url", "group"}
