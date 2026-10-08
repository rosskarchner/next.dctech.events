"""The review-queue query: dropping events that have already happened.

list_pending_qa used to return the whole pending_qa queue oldest-first,
including events dated before today. iCal events enter the queue as pending_qa
and only resolve_qa_review takes them out, so the stale tail grew every day and
filled the front of every bounded batch (on 2026-10-07, 13 of the first 15).

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_review_status_query.py
"""
import os
from datetime import datetime, timezone

import pytest
from boto3.dynamodb.conditions import ConditionExpressionBuilder

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
    """Records the query and answers it from a sorted list, honouring the
    sort-key lower bound the way GSI5 would, so the tests exercise the key
    condition itself and not just that some argument was passed."""

    def __init__(self, items):
        self.items = sorted(items, key=lambda i: i["GSI5SK"])
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        built = ConditionExpressionBuilder().build_expression(
            kwargs["KeyConditionExpression"], is_key_condition=True)
        values = list(built.attribute_value_placeholders.values())
        partition, lower = values[0], (values[1] if len(values) > 1 else None)
        rows = [i for i in self.items
                if i["GSI5PK"] == partition
                and (lower is None or i["GSI5SK"] >= lower)]
        if "Limit" in kwargs:
            rows = rows[:kwargs["Limit"]]
        return {"Items": rows}


def _item(guid, date):
    return {"PK": f"EVENT#{guid}", "SK": "META", "guid": guid, "date": date,
            "GSI5PK": "REVIEW#pending_qa", "GSI5SK": f"{date}#18:00"}


@pytest.fixture
def table(monkeypatch):
    t = FakeTable([_item("old1", "2026-09-29"), _item("old2", "2026-10-06"),
                   _item("today", "2026-10-07"), _item("soon", "2026-10-08"),
                   _item("later", "2027-03-31"),
                   {**_item("other", "2026-10-08"),
                    "GSI5PK": "REVIEW#flagged"}])
    monkeypatch.setattr(db, "_get_table", lambda: t)
    monkeypatch.setattr(db, "_local_today", lambda: "2026-10-07")
    return t


def guids(events):
    return [e["guid"] for e in events]


def test_past_events_are_left_out_when_asked(table):
    got = db.get_events_by_review_status("pending_qa", include_past=False)
    assert guids(got) == ["today", "soon", "later"]


def test_an_event_dated_today_is_not_past(table):
    got = db.get_events_by_review_status("pending_qa", include_past=False)
    assert "today" in guids(got)


def test_the_default_still_returns_everything_for_other_callers(table):
    # The admin /edit listing calls this without include_past and has its own
    # idea of what to show; the new option must not change it.
    got = db.get_events_by_review_status("pending_qa")
    assert guids(got) == ["old1", "old2", "today", "soon", "later"]


def test_include_past_true_returns_the_stale_tail_too(table):
    got = db.get_events_by_review_status("pending_qa", include_past=True)
    assert guids(got)[:2] == ["old1", "old2"]


def test_the_cutoff_is_part_of_the_key_condition_not_a_filter(table):
    """A FilterExpression runs after the page is read, so with a limit the
    query would still walk the whole stale tail. It has to be a key condition."""
    db.get_events_by_review_status("pending_qa", include_past=False)
    call = table.calls[0]
    assert "FilterExpression" not in call
    built = ConditionExpressionBuilder().build_expression(
        call["KeyConditionExpression"], is_key_condition=True)
    assert "REVIEW#pending_qa" in built.attribute_value_placeholders.values()
    assert "2026-10-07" in built.attribute_value_placeholders.values()


def test_a_limit_counts_live_events_not_stale_ones(table):
    # The failure this bead fixed: limit=2 used to come back as the two oldest,
    # both already past.
    got = db.get_events_by_review_status("pending_qa", limit=2, include_past=False)
    assert guids(got) == ["today", "soon"]


def test_other_review_statuses_are_not_mixed_in(table):
    got = db.get_events_by_review_status("pending_qa", include_past=False)
    assert "other" not in guids(got)


# ── "today" is the site's date, not the server's ───────────────────────

def test_today_follows_eastern_time_not_utc(monkeypatch):
    # 02:00 UTC on Oct 12 is 10 PM on Oct 11 in New York: still the 11th there.
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
            return moment.astimezone(tz) if tz else moment

    monkeypatch.setattr(db, "datetime", Frozen)
    assert db._local_today() == "2026-10-11"


def test_today_rolls_over_after_eastern_midnight(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)  # 1 AM EDT
            return moment.astimezone(tz) if tz else moment

    monkeypatch.setattr(db, "datetime", Frozen)
    assert db._local_today() == "2026-10-12"
