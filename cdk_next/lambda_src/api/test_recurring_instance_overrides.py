"""Tests for per-occurrence recurring-event overrides: the storage layer
approved recurring_instance corrections write to (db.py's
set_recurring_instance_override/get_recurring_instance_override(s)).

Stored as PK=RECURRING#{slug}, SK=OVERRIDE#{date} — the series' own
partition, not a new top-level entity — keyed by (slug, date), never by an
occurrence's rendered guid (see set_recurring_instance_override's docstring
for why that guid isn't a safe key).

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_recurring_instance_overrides.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
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

    def delete_item(self, Key):
        self.items.pop(self._k(Key), None)

    @staticmethod
    def _matches(item, cond):
        name = cond._values[0].name
        if type(cond).__name__ == "BeginsWith":
            return str(item.get(name, "")).startswith(cond._values[1])
        return item.get(name) == cond._values[1]

    def query(self, KeyConditionExpression, **kwargs):
        cond = KeyConditionExpression
        if type(cond).__name__ == "And":
            left, right = cond._values
            rows = [i for i in self.items.values()
                   if self._matches(i, left) and self._matches(i, right)]
        else:
            rows = [i for i in self.items.values() if self._matches(i, cond)]
        return {"Items": rows}


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    return fake


def test_set_and_get_a_single_override(table):
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"location": "New Place"}, "why",
        actor="admin@example.com")

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")

    assert override["location"] == "New Place"
    assert override["_edited_by"] == "admin@example.com"
    assert override["_comment"] == "why"


def test_get_an_unset_override_returns_none(table):
    assert db.get_recurring_instance_override("weekly-thing", "2026-06-08") is None


def test_setting_twice_merges_fields_rather_than_replacing(table):
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"location": "New Place"})
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"description": "also fixed"})

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")

    assert override["location"] == "New Place"
    assert override["description"] == "also fixed"


def test_setting_twice_overwrites_a_shared_field(table):
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"location": "First"})
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"location": "Second"})

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")

    assert override["location"] == "Second"


def test_a_missing_date_raises(table):
    with pytest.raises(ValueError, match="date is required"):
        db.set_recurring_instance_override("weekly-thing", "", {"location": "x"})


def test_get_recurring_instance_overrides_returns_all_dates_for_a_series(table):
    db.set_recurring_instance_override("weekly-thing", "2026-06-08", {"location": "A"})
    db.set_recurring_instance_override("weekly-thing", "2026-06-15", {"location": "B"})

    overrides = db.get_recurring_instance_overrides("weekly-thing")

    assert set(overrides) == {"2026-06-08", "2026-06-15"}
    assert overrides["2026-06-08"]["location"] == "A"
    assert overrides["2026-06-15"]["location"] == "B"


def test_overrides_for_different_series_do_not_collide(table):
    db.set_recurring_instance_override("weekly-thing", "2026-06-08", {"location": "A"})
    db.set_recurring_instance_override("monthly-thing", "2026-06-08", {"location": "B"})

    assert set(db.get_recurring_instance_overrides("weekly-thing")) == {"2026-06-08"}
    assert db.get_recurring_instance_overrides("weekly-thing")["2026-06-08"]["location"] == "A"
    assert db.get_recurring_instance_overrides("monthly-thing")["2026-06-08"]["location"] == "B"


def test_get_recurring_instance_overrides_is_empty_for_an_unknown_series(table):
    assert db.get_recurring_instance_overrides("ghost-series") == {}


def test_an_unsafe_url_is_sanitized_the_same_as_put_recurring_event(table):
    db.set_recurring_instance_override(
        "weekly-thing", "2026-06-08", {"url": "javascript:alert(1)"})

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")

    assert override["url"] == ""


def test_delete_recurring_instance_override(table):
    db.set_recurring_instance_override("weekly-thing", "2026-06-08", {"location": "A"})
    db.delete_recurring_instance_override("weekly-thing", "2026-06-08")

    assert db.get_recurring_instance_override("weekly-thing", "2026-06-08") is None
