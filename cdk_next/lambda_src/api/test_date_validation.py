"""Dates and times must be ISO before they reach the table.

DynamoDB sort keys are strings and put_event builds GSI1SK/GSI4SK from an
event's date, so a free-text date does not merely look wrong — it sorts wrong.
Two rows holding 'June 6th 2025' sorted as though the year were "June", and
`date >= '2026-08-27'` was true for them because "J" comes after "2". Any range
query over GSI4 is silently misled by one (next_dctech_events-eex).

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_date_validation.py
"""
import os
from datetime import date as date_type

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


# ── Dates ──────────────────────────────────────────────────────────


def test_an_iso_date_passes_through():
    assert db.validate_event_date("2026-09-08") == "2026-09-08"


def test_a_real_date_object_is_formatted():
    assert db.validate_event_date(date_type(2026, 9, 8)) == "2026-09-08"


def test_the_exact_string_found_in_production_is_refused():
    with pytest.raises(ValueError, match="ISO-8601"):
        db.validate_event_date("June 6th 2025")


@pytest.mark.parametrize("bad", [
    "6/6/2025", "2025-6-6", "June 6 2025", "next Tuesday",
    "2026-09-08T18:00", "20260908", "tomorrow",
])
def test_other_free_text_dates_are_refused(bad):
    with pytest.raises(ValueError):
        db.validate_event_date(bad)


def test_a_date_that_looks_iso_but_is_not_real_is_refused():
    # Matches the pattern; February has no 30th.
    with pytest.raises(ValueError, match="not a real date"):
        db.validate_event_date("2026-02-30")


def test_empty_and_none_pass_through_for_optional_fields():
    assert db.validate_event_date(None) is None
    assert db.validate_event_date("") == ""


def test_the_error_names_the_field():
    with pytest.raises(ValueError, match="end_date must be"):
        db.validate_event_date("whenever", "end_date")


def test_the_error_shows_what_was_sent():
    with pytest.raises(ValueError, match="June 6th 2025"):
        db.validate_event_date("June 6th 2025")


def test_surrounding_whitespace_is_tolerated():
    assert db.validate_event_date("  2026-09-08  ") == "2026-09-08"


# ── Times ──────────────────────────────────────────────────────────


def test_a_24_hour_time_passes_through():
    assert db.validate_event_time("18:30") == "18:30"


def test_the_shape_found_on_the_bad_rows_is_refused():
    with pytest.raises(ValueError, match="HH:MM"):
        db.validate_event_time("6pm")


@pytest.mark.parametrize("bad", ["6:30 PM", "6:30pm", "1830", "18:30:00", "noon"])
def test_other_free_text_times_are_refused(bad):
    with pytest.raises(ValueError):
        db.validate_event_time(bad)


def test_an_impossible_clock_time_is_refused():
    with pytest.raises(ValueError, match="not a real time"):
        db.validate_event_time("25:00")
    with pytest.raises(ValueError, match="not a real time"):
        db.validate_event_time("12:75")


def test_no_time_is_valid():
    # An all-day event has none, and calgen writes '' for a midnight start.
    assert db.validate_event_time(None) is None
    assert db.validate_event_time("") == ""


def test_midnight_and_end_of_day_are_valid():
    assert db.validate_event_time("00:00") == "00:00"
    assert db.validate_event_time("23:59") == "23:59"


# ── The write boundary ─────────────────────────────────────────────


@pytest.fixture
def written(monkeypatch):
    """put_event is the invariant: every EVENT# write goes through it."""
    calls = []

    class _Table:
        def put_item(self, **kw):
            calls.append(kw["Item"])

    monkeypatch.setattr(db, "_get_table", lambda: _Table())
    return calls


def test_put_event_refuses_a_free_text_date(written):
    with pytest.raises(ValueError, match="ISO-8601"):
        db.put_event("g1", {"title": "Technology CFO Awards",
                            "date": "June 6th 2025", "time": "6pm"})
    assert written == []


def test_put_event_refuses_a_free_text_time(written):
    with pytest.raises(ValueError, match="HH:MM"):
        db.put_event("g1", {"title": "x", "date": "2026-09-08", "time": "6pm"})
    assert written == []


def test_put_event_accepts_a_well_formed_event(written):
    db.put_event("g1", {"title": "x", "date": "2026-09-08", "time": "18:30"})
    assert written[0]["GSI1PK"] == "DATE#2026-09-08"
    assert written[0]["GSI4SK"].startswith("2026-09-08")


def test_put_event_normalizes_a_date_object(written):
    db.put_event("g1", {"title": "x", "date": date_type(2026, 9, 8)})
    assert written[0]["date"] == "2026-09-08"


def test_put_event_still_accepts_an_event_with_no_time(written):
    # The aggregator writes '' for a midnight start; all-day events have none.
    db.put_event("g1", {"title": "x", "date": "2026-09-08", "time": ""})
    assert written[0]["GSI1SK"] == "TIME#00:00"


def test_put_event_does_not_mutate_the_callers_dict(written):
    data = {"title": "x", "date": date_type(2026, 9, 8)}
    db.put_event("g1", data)
    assert data["date"] == date_type(2026, 9, 8)


# ── The submission boundary ────────────────────────────────────────


def test_the_draft_builder_rejects_a_free_text_date():
    draft, error = db.build_event_draft_data(
        {"title": "Technology CFO Awards", "date": "June 6th 2025"})
    assert draft is None
    assert "ISO-8601" in error


def test_the_draft_builder_rejects_a_free_text_time():
    draft, error = db.build_event_draft_data(
        {"title": "x", "date": "2026-09-08", "time": "6pm"})
    assert draft is None
    assert "HH:MM" in error


def test_the_draft_builder_returns_a_message_rather_than_raising():
    # Its contract is (draft, error) and both callers render the error to
    # whoever submitted the form.
    result = db.build_event_draft_data({"title": "x", "date": "nonsense"})
    assert isinstance(result, tuple) and result[0] is None


def test_the_draft_builder_still_accepts_a_good_submission():
    draft, error = db.build_event_draft_data(
        {"title": "Real Event", "date": "2026-09-08", "time": "18:30"})
    assert error is None
    assert (draft["date"], draft["time"]) == ("2026-09-08", "18:30")


def test_the_draft_builders_am_pm_widget_still_produces_a_valid_time():
    # The form's hour/minute/ampm selects are assembled into HH:MM, which must
    # then survive the new check.
    draft, error = db.build_event_draft_data(
        {"title": "x", "date": "2026-09-08", "timing": "specific",
         "time_hour": "6", "time_minute": "30", "time_ampm": "PM"})
    assert error is None
    assert draft["time"] == "18:30"


# ── Multi-day events carry a per-day time map ──────────────────────
# A conference can start at 14:00 on its first day and 10:00 on the rest.
# calgen renders that shape deliberately (app.py's isinstance(original_time,
# dict)), so the validator must not refuse it. One such row is live —
# "Data Center World 2026" — and an earlier version of this check would have
# blocked every future write to it.


def test_a_per_day_time_map_is_accepted():
    value = {"2026-04-21": "14:00", "2026-04-22": "10:00",
             "2026-04-23": "10:00"}
    assert db.validate_event_time(dict(value)) == value


def test_each_time_in_the_map_is_still_checked():
    with pytest.raises(ValueError, match=r"time\[2026-04-22\]"):
        db.validate_event_time({"2026-04-22": "6pm"})


def test_the_maps_keys_are_checked_as_dates():
    with pytest.raises(ValueError, match="time key must be"):
        db.validate_event_time({"April 22": "10:00"})


def test_an_empty_map_is_accepted():
    assert db.validate_event_time({}) == {}


def test_put_event_writes_a_multi_day_event(written):
    db.put_event("g1", {"title": "Data Center World 2026",
                        "date": "2026-04-20", "end_date": "2026-04-23",
                        "time": {"2026-04-21": "14:00", "2026-04-22": "10:00"}})
    assert written[0]["time"] == {"2026-04-21": "14:00", "2026-04-22": "10:00"}
    # put_event already unpacks the map for the sort key rather than
    # stringifying it — it takes the first time, not literal dict syntax.
    assert written[0]["GSI1SK"] == "TIME#14:00"
