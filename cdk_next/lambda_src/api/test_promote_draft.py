"""Tests for the shared draft-promotion logic.

This is the one implementation behind both the REST approve routes and the MCP
approve_submission tool, so a regression here changes what "approved" means in
both places at once.

Run: python -m pytest test_promote_draft.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    recorded = {"groups": [], "events": []}
    monkeypatch.setattr(db, "put_group",
                        lambda slug, data: recorded["groups"].append((slug, data)))
    monkeypatch.setattr(db, "promote_draft_to_event",
                        lambda merged: recorded["events"].append(merged) or merged["id"])
    return recorded


# ── Slugs ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Python DC", "python-dc"),
    ("  Spaced  Out  ", "spaced-out"),
    ("Mixed CASE Name", "mixed-case-name"),
    ("Symbols!@#$%Here", "symbols-here"),
    ("DC |> Elixir", "dc-elixir"),
])
def test_slugify(name, expected):
    assert db.slugify(name) == expected


def test_slugify_handles_empty_and_none():
    assert db.slugify("") == ""
    assert db.slugify(None) == ""


# ── Event promotion ────────────────────────────────────────────────

def test_event_promotion_uses_the_draft_id_as_guid(calls):
    result = db.promote_draft("abc123", "event", {"title": "T"})
    assert result == "abc123"
    assert calls["events"][0]["id"] == "abc123"


def test_event_promotion_keeps_an_explicit_id(calls):
    # setdefault, not overwrite: a caller that already chose a guid wins.
    db.promote_draft("abc123", "event", {"title": "T", "id": "chosen"})
    assert calls["events"][0]["id"] == "chosen"


def test_event_promotion_does_not_mutate_the_caller_s_dict(calls):
    merged = {"title": "T"}
    db.promote_draft("abc123", "event", merged)
    assert "id" not in merged


# ── Group promotion ────────────────────────────────────────────────

def test_group_promotion_slugifies_the_name(calls):
    result = db.promote_draft("d1", "group", {"name": "Python DC",
                                              "website": "https://x.com"})
    assert result == "python-dc"
    slug, data = calls["groups"][0]
    assert slug == "python-dc"
    assert data["name"] == "Python DC"
    assert data["active"] is True


def test_group_promotion_falls_back_to_draft_id_without_a_name(calls):
    result = db.promote_draft("d1", "group", {"website": "https://x.com"})
    assert result == "d1"


def test_group_promotion_accepts_either_ical_field(calls):
    db.promote_draft("d1", "group", {"name": "A", "ical_url": "https://f/a.ics"})
    assert calls["groups"][0][1]["ical"] == "https://f/a.ics"

    db.promote_draft("d2", "group", {"name": "B", "ical": "https://f/b.ics"})
    assert calls["groups"][1][1]["ical"] == "https://f/b.ics"


def test_group_promotion_prefers_ical_over_ical_url(calls):
    db.promote_draft("d1", "group", {"name": "A", "ical": "https://f/pref.ics",
                                     "ical_url": "https://f/other.ics"})
    assert calls["groups"][0][1]["ical"] == "https://f/pref.ics"


def test_group_promotion_omits_absent_optional_fields(calls):
    db.promote_draft("d1", "group", {"name": "A", "website": "https://x.com"})
    data = calls["groups"][0][1]
    assert "ical" not in data
    assert "fallback_url" not in data
    assert "categories" not in data


def test_group_promotion_carries_categories_and_fallback(calls):
    db.promote_draft("d1", "group", {
        "name": "A", "website": "https://x.com",
        "categories": ["ai"], "fallback_url": "https://x.com/events"})
    data = calls["groups"][0][1]
    assert data["categories"] == ["ai"]
    assert data["fallback_url"] == "https://x.com/events"


def test_unknown_draft_type_is_treated_as_an_event(calls):
    # draft_type defaults to 'event' upstream; anything not 'group' publishes
    # as an event rather than silently doing nothing.
    db.promote_draft("abc", "something-else", {"title": "T"})
    assert calls["events"] and not calls["groups"]
