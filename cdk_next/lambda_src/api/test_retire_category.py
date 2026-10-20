"""Tests for db.retire_category.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_retire_category.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402

CATEGORIES = {"tech-culture": {}, "social": {}, "software-development": {}}


@pytest.fixture
def world(monkeypatch):
    state = {
        "groups": {
            "bcc": {"id": "bcc", "name": "BCC",
                    "categories": ["software-development", "tech-culture"]},
            "faith": {"id": "faith", "name": "Faith", "categories": ["tech-culture"],
                      "category_rules": [{"phrase": "x", "categories": ["tech-culture"]},
                                         {"phrase": "y", "categories": ["social", "tech-culture"]}]},
            "other": {"id": "other", "name": "Other", "categories": ["social"]},
        },
        "events": [
            {"PK": "EVENT#e1", "categories": ["social", "tech-culture"]},
            {"PK": "EVENT#e2", "categories": ["social"],
             "overrides": {"categories": ["software-development", "tech-culture"]}},
            {"PK": "EVENT#e3", "categories": [],
             "overrides": {"categories": ["tech-culture"]}},
        ],
        "subscribers": [
            {"PK": "SUBSCRIBER#a@x", "categories": ["tech-culture"], "regions": ["dc"]},
            {"PK": "SUBSCRIBER#b@x", "categories": ["social", "tech-culture"]},
        ],
        "recurring": [{"id": "r1", "categories": ["tech-culture", "social"]},
                      {"id": "r2", "categories": ["social"]}],
        "puts": {}, "updates": {}, "overlays": [], "merged": {}, "prefs": {},
        "deleted": [],
    }
    scans = iter([state["events"], state["subscribers"]])

    class Table:
        def update_item(self, Key, ExpressionAttributeValues, **kw):
            state["updates"][Key["PK"]] = ExpressionAttributeValues[":c"]

    monkeypatch.setattr(db, "get_all_categories", lambda: CATEGORIES)
    monkeypatch.setattr(db, "_get_table", lambda: Table())
    monkeypatch.setattr(db, "_scan_all", lambda table, **kw: next(scans))
    monkeypatch.setattr(db, "get_all_groups",
                        lambda: [dict(g) for g in state["groups"].values()])
    monkeypatch.setattr(db, "put_group",
                        lambda slug, data: state["puts"].__setitem__(slug, data))
    monkeypatch.setattr(db, "set_event_overlay",
                        lambda guid, fields, comment=None, **kw:
                        state["overlays"].append((guid, fields, kw.get("clear"))))
    monkeypatch.setattr(db, "get_all_recurring_events", lambda: state["recurring"])
    monkeypatch.setattr(db, "merge_recurring_event_fields",
                        lambda slug, f: state["merged"].__setitem__(slug, f))
    monkeypatch.setattr(db, "put_subscriber_preferences",
                        lambda email, c, r: state["prefs"].__setitem__(email, (c, r)))
    monkeypatch.setattr(db, "delete_category", state["deleted"].append)
    return state


def test_retire_strips_everywhere_and_moves_subscribers(world):
    out = db.retire_category("tech-culture", subscribers_move_to="social")

    assert world["puts"]["bcc"]["categories"] == ["software-development"]
    assert world["puts"]["faith"]["categories"] == []
    assert world["puts"]["faith"]["category_rules"] == [
        {"phrase": "y", "categories": ["social"]}]
    assert "other" not in world["puts"]
    assert world["updates"] == {"EVENT#e1": ["social"]}
    assert world["overlays"] == [
        ("e2", {"categories": ["software-development"]}, None),
        ("e3", {}, ("categories",)),
    ]
    assert world["merged"] == {"r1": {"categories": ["social"]}}
    assert world["prefs"] == {"a@x": (["social"], ["dc"]), "b@x": (["social"], [])}
    assert world["deleted"] == ["tech-culture"]
    assert out["subscribers"] == 2 and out["groups"] == ["bcc", "faith"]


def test_retire_without_move_just_drops(world):
    db.retire_category("tech-culture")
    assert world["prefs"]["a@x"] == ([], ["dc"])


def test_retire_rejects_unknown(world):
    with pytest.raises(ValueError):
        db.retire_category("nope")
    with pytest.raises(ValueError):
        db.retire_category("tech-culture", subscribers_move_to="nope")
    assert world["deleted"] == []
