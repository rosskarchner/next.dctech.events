"""Tests for the MCP overlay + QA-run tools.

The QA agent writes overlays straight to production with no pull request in
between, so revert_qa_run is the only thing standing between a bad weekly run
and hand-repairing every event it touched. These tests are mostly about that.

Run: python -m pytest test_overlay_tools.py
"""
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import server  # noqa: E402

set_overlay = server.set_overlay
get_event = server.get_event
list_qa_run = server.list_qa_run
revert_qa_run = server.revert_qa_run
list_pending_qa = server.list_pending_qa
resolve_qa_review = server.resolve_qa_review


def rendered(store, guid):
    """The overlay as calgen would see it — private bookkeeping stripped.

    Assertions go through this rather than comparing the stored map directly:
    an overlay also carries `_rev`, `_edited_by`, `_edited_at` and friends, and
    a revert deliberately leaves those behind as the audit trail. What these
    tests are about is which *overlay fields* survive.
    """
    return server.db.public_overlay(store[guid].get("overrides"))


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for the EVENT# items db.* reads and writes."""
    events = {
        "g1": {"guid": "g1", "title": "NoVABeerSec - June 23rd", "date": "2026-06-23",
               "time": "18:00", "group": "NoVABeerSec", "description": "Beers."},
        "g2": {"guid": "g2", "title": "Same Talk, Reposted", "date": "2026-06-24",
               "time": "19:00", "group": "Other Group"},
    }

    monkeypatch.setattr(server.db, "get_event_from_config",
                        lambda guid: dict(events[guid]) if guid in events else None)
    monkeypatch.setattr(server.db, "get_all_events",
                        lambda include_past=False: [dict(e) for e in events.values()])

    # **kw absorbs expect_overrides_rev: the real update_event takes it to make
    # the write conditional, and an in-memory store has no race to lose.
    def _update(guid, data, overrides=None, **kw):
        events[guid]["overrides"] = overrides

    monkeypatch.setattr(server.db, "update_event", _update)

    def _by_status(status, limit=None):
        return [dict(e) for e in events.values()
                if e.get("review_status") == status]

    def _set_status(guid, status):
        events[guid]["review_status"] = status

    monkeypatch.setattr(server.db, "get_events_by_review_status", _by_status)
    monkeypatch.setattr(server.db, "set_event_review_status", _set_status)
    return events


# ── The QC work queue ──────────────────────────────────────────────

def test_list_pending_qa_returns_only_queued_events(store):
    store["g1"]["review_status"] = "pending_qa"
    store["g2"]["review_status"] = "approved"

    assert [e["guid"] for e in list_pending_qa()] == ["g1"]


def test_list_pending_qa_returns_the_compact_projection(store):
    store["g1"]["review_status"] = "pending_qa"
    # description is deliberately absent — that's what get_event is for.
    assert "description" not in list_pending_qa()[0]


def test_resolve_qa_review_clears_the_event_from_the_queue(store):
    store["g1"]["review_status"] = "pending_qa"

    resolve_qa_review("g1", "approved")

    assert store["g1"]["review_status"] == "approved"
    assert list_pending_qa() == []


def test_resolve_qa_review_can_flag_for_a_human(store):
    store["g1"]["review_status"] = "pending_qa"
    assert resolve_qa_review("g1", "flagged")["review_status"] == "flagged"


def test_resolve_qa_review_rejects_an_arbitrary_status(store):
    # Guards GSI5 against statuses nothing queries for.
    with pytest.raises(ValueError, match="must be"):
        resolve_qa_review("g1", "pending_discovery_review")


def test_resolve_qa_review_unknown_event_raises(store):
    with pytest.raises(ValueError, match="No such event"):
        resolve_qa_review("nope", "approved")


# ── Overlay writes ─────────────────────────────────────────────────

def test_set_overlay_merges_without_dropping_existing_fields(store):
    set_overlay("g1", {"title": "NoVABeerSec"}, "trimmed date")
    set_overlay("g1", {"location": "Starr Hill, Tysons VA"}, "found venue")

    overlay = store["g1"]["overrides"]
    assert overlay["title"] == "NoVABeerSec"
    assert overlay["location"] == "Starr Hill, Tysons VA"


def test_set_overlay_rejects_protected_fields(store):
    with pytest.raises(ValueError, match="protected"):
        set_overlay("g1", {"group": "Somebody Else"}, "nope")
    assert "overrides" not in store["g1"]


def test_set_overlay_rejects_private_bookkeeping_keys(store):
    # Otherwise a caller could forge a run stamp and hijack a revert.
    with pytest.raises(ValueError, match="reserved"):
        set_overlay("g1", {"_qa_run": {"run_id": "forged"}}, "nope")


def test_set_overlay_hides_bookkeeping_from_the_returned_overlay(store):
    result = set_overlay("g1", {"hidden": True}, "out of area", run_id="r1")
    assert result["overlay"] == {"hidden": True}


def test_set_overlay_unknown_event_raises(store):
    with pytest.raises(ValueError, match="No such event"):
        set_overlay("nope", {"hidden": True}, "x")


# ── get_event ──────────────────────────────────────────────────────

def test_get_event_returns_description(store):
    # The whole reason this tool exists: get_events' projection omits it.
    assert get_event("g1")["description"] == "Beers."


def test_get_event_unknown_raises(store):
    with pytest.raises(ValueError, match="No such event"):
        get_event("nope")


# ── Run listing ────────────────────────────────────────────────────

def test_list_qa_run_reports_only_that_run(store):
    set_overlay("g1", {"title": "NoVABeerSec"}, "trimmed", run_id="r1")
    set_overlay("g2", {"hidden": True}, "out of area", run_id="r2")

    listed = list_qa_run("r1")
    assert [row["guid"] for row in listed] == ["g1"]
    assert listed[0]["applied"] == {"title": "NoVABeerSec"}
    assert listed[0]["comment"] == "trimmed"


def test_list_qa_run_ignores_hand_written_overlays(store):
    set_overlay("g1", {"title": "By Hand"}, "manual fix")  # no run_id
    assert list_qa_run("r1") == []


# ── Revert ─────────────────────────────────────────────────────────

def test_revert_removes_fields_the_run_introduced(store):
    set_overlay("g1", {"title": "NoVABeerSec", "hidden": True}, "qc", run_id="r1")

    result = revert_qa_run("r1")

    assert result["reverted"] == 1
    assert rendered(store, "g1") == {}


def test_revert_restores_a_value_the_run_overwrote(store):
    set_overlay("g1", {"title": "Human Title"}, "by hand")
    set_overlay("g1", {"title": "Agent Title"}, "qc", run_id="r1")

    revert_qa_run("r1")

    # The human's title comes back — reverting is a restore, not a delete.
    assert store["g1"]["overrides"]["title"] == "Human Title"


def test_revert_leaves_overlay_fields_the_run_never_touched(store):
    set_overlay("g1", {"location": "Set By Hand"}, "by hand")
    set_overlay("g1", {"title": "Agent Title"}, "qc", run_id="r1")

    revert_qa_run("r1")

    assert rendered(store, "g1") == {"location": "Set By Hand"}


def test_revert_uses_the_earliest_snapshot_within_a_run(store):
    set_overlay("g1", {"title": "Original"}, "by hand")
    set_overlay("g1", {"title": "First Pass"}, "qc", run_id="r1")
    set_overlay("g1", {"title": "Second Pass"}, "qc again", run_id="r1")

    revert_qa_run("r1")

    # Not "First Pass" — a second write in the same run must not overwrite the
    # snapshot taken before the run started.
    assert store["g1"]["overrides"]["title"] == "Original"


def test_revert_spans_every_event_in_the_run(store):
    set_overlay("g1", {"title": "A"}, "qc", run_id="r1")
    set_overlay("g2", {"duplicate_of": "g1"}, "qc", run_id="r1")

    assert revert_qa_run("r1")["reverted"] == 2
    assert rendered(store, "g1") == {}
    assert rendered(store, "g2") == {}


def test_revert_leaves_other_runs_alone(store):
    set_overlay("g1", {"title": "A"}, "qc", run_id="r1")
    set_overlay("g2", {"hidden": True}, "qc", run_id="r2")

    revert_qa_run("r1")

    assert store["g2"]["overrides"]["hidden"] is True


def test_revert_of_an_unknown_run_is_a_no_op(store):
    assert revert_qa_run("never-ran") == {"run_id": "never-ran",
                                          "reverted": 0, "events": []}


def test_revert_is_idempotent(store):
    set_overlay("g1", {"hidden": True}, "qc", run_id="r1")
    revert_qa_run("r1")
    # The stamp is gone, so a second revert finds nothing to undo.
    assert revert_qa_run("r1")["reverted"] == 0
    assert rendered(store, "g1") == {}


# ── update_single_event is not the editorial path ───────────────────


def test_update_single_event_refuses_an_ical_event(store, monkeypatch):
    """The aggregator rewrites an iCal row from the feed every four hours, so
    an edit here would report success and then vanish. Before this guard it
    was a silent no-op that looked like a win."""
    store["g1"]["source"] = "ical"
    monkeypatch.setattr(server.db, "put_event",
                        lambda *a, **kw: pytest.fail("should not have written"))

    with pytest.raises(ValueError, match="use set_overlay|Use set_overlay"):
        server.update_single_event("g1", {"title": "Nope"})


def test_update_single_event_still_edits_a_manual_event(store, monkeypatch):
    store["g1"]["source"] = "manual"
    written = {}
    monkeypatch.setattr(server.db, "put_event",
                        lambda guid, data, **kw: written.update({guid: data}))

    server.update_single_event("g1", {"title": "Corrected"})

    assert written["g1"]["title"] == "Corrected"
