"""Approval must publish what was submitted.

Two bugs lost data here: the web approve routes overwrote a draft's categories
with [] whenever the request carried none, and the draft read path was a
whitelist that dropped end_date, end_time, city, state, all_day and
fallback_url. Both paths (admin routes and MCP approve_submission) now share
db.merge_draft_for_approval and read drafts whole.

Run: python -m pytest test_draft_approval.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: t)
    monkeypatch.setattr(db, "valid_category_slugs", lambda: {"ai", "security", "quantum"})
    return t


def _submit_multi_day(**extra):
    form = {
        "title": "BSides NoVA", "date": "2026-11-14", "end_date": "2026-11-15",
        "timing": "allday", "city": "Reston", "state": "VA",
        "location": "Reston Town Center", "categories": ["security"], **extra,
    }
    draft, err = db.build_event_draft_data(form)
    assert err is None
    return db.create_draft("event", draft, "someone@example.com")


def test_a_draft_reads_back_with_every_field_it_was_stored_with(table):
    draft = db.get_draft(_submit_multi_day())
    assert draft["end_date"] == "2026-11-15"
    assert draft["city"] == "Reston" and draft["state"] == "VA"
    assert draft["all_day"] is True
    assert draft["categories"] == ["security"]


def test_a_draft_read_leaks_no_table_keys_or_account_ids(table):
    draft_id = _submit_multi_day()
    table.items[(f"DRAFT#{draft_id}", "META")]["submitter_id"] = "cognito-sub"
    draft = db.get_draft(draft_id)
    assert not [k for k in draft if k in ("PK", "SK", "submitter_id") or k.startswith("GSI")]


def test_group_fallback_url_survives_the_read(table):
    draft_id = db.create_draft("group", {"name": "G", "fallback_url": "https://g.example"},
                               "someone@example.com")
    assert db.get_draft(draft_id)["fallback_url"] == "https://g.example"


def test_submit_to_published_event_keeps_the_multi_day_range(table):
    draft_id = _submit_multi_day()
    merged = db.merge_draft_for_approval(db.get_draft(draft_id), {})
    db.promote_draft(draft_id, "event", merged)
    event = table.items[(f"EVENT#{draft_id}", "META")]
    assert event["end_date"] == "2026-11-15"
    assert (event["city"], event["state"], event["all_day"]) == ("Reston", "VA", True)
    assert event["categories"] == ["security"]


def test_end_time_is_published_when_the_draft_has_one(table):
    draft_id = db.create_draft(
        "event", {"title": "T", "date": "2026-11-14", "time": "18:00", "end_time": "20:00"},
        "someone@example.com")
    db.promote_draft(draft_id, "event", db.get_draft(draft_id))
    assert table.items[(f"EVENT#{draft_id}", "META")]["end_time"] == "20:00"


# ── category overrides ────────────────────────────────────────────────

def test_a_request_without_categories_keeps_the_drafts(table):
    draft = db.get_draft(_submit_multi_day())
    # What the web form sends when no category box is ticked: no key at all.
    assert db.merge_draft_for_approval(draft, {"title": "BSides NoVA"})["categories"] == ["security"]


def test_a_single_slug_string_replaces_the_drafts_categories(table):
    draft = db.get_draft(_submit_multi_day())
    assert db.merge_draft_for_approval(draft, {"categories": "ai"})["categories"] == ["ai"]


def test_a_list_replaces_the_drafts_categories(table):
    draft = db.get_draft(_submit_multi_day())
    merged = db.merge_draft_for_approval(draft, {"categories": ["ai", "quantum"]})
    assert merged["categories"] == ["ai", "quantum"]


def test_an_explicit_empty_value_clears_categories(table):
    draft = db.get_draft(_submit_multi_day())
    assert db.merge_draft_for_approval(draft, {"categories": ""})["categories"] == []


def test_an_unknown_category_slug_is_refused(table):
    draft = db.get_draft(_submit_multi_day())
    with pytest.raises(ValueError, match="Unknown category"):
        db.merge_draft_for_approval(draft, {"categories": ["nonsense"]})


def test_other_overrides_apply_and_none_values_do_not(table):
    draft = db.get_draft(_submit_multi_day())
    merged = db.merge_draft_for_approval(draft, {"title": "Renamed", "city": None})
    assert merged["title"] == "Renamed" and merged["city"] == "Reston"


def test_merging_does_not_mutate_the_draft_or_overrides(table):
    draft = db.get_draft(_submit_multi_day())
    overrides = {"categories": "ai"}
    db.merge_draft_for_approval(draft, overrides)
    assert draft["categories"] == ["security"] and overrides == {"categories": "ai"}
