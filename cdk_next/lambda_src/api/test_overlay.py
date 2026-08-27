"""Tests for the shared overlay writer.

This is the one implementation behind both the MCP set_overlay tool and the
/edit UI's overlay routes, so a regression here changes what an edit means in
both places at once. The MCP-facing behaviour is covered from the other side in
mcp/test_overlay_tools.py; what is tested here is the machinery those tools did
not have before the lift — the field allowlist, value validation, per-field
provenance, and the conditional write that stops two writers clobbering each
other.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_overlay.py
"""
import os

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402

CATEGORIES = {"ai": {"name": "AI"}, "cloud": {"name": "Cloud"}}


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for the EVENT# items, with a real CAS check.

    The conditional write is the point of most of these tests, so the fake
    enforces it rather than accepting every write like the MCP fixture does.
    """
    events = {
        "g1": {"guid": "g1", "title": "Feed Title", "date": "2026-06-23",
               "time": "18:00", "group": "NoVABeerSec"},
        "g2": {"guid": "g2", "title": "Other Event", "date": "2026-06-24",
               "time": "19:00", "group": "Other Group"},
    }

    def _conditional_failure():
        return ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException",
                       "Message": "the conditional request failed"}},
            "UpdateItem",
        )

    def _update(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        if expect_overrides_rev is not db._UNSET:
            stored = (events[guid].get("overrides") or {}).get("_rev")
            expected = expect_overrides_rev
            if (expected is None and stored is not None) or \
               (expected is not None and stored != expected):
                raise _conditional_failure()
        events[guid]["overrides"] = overrides
        events[guid]["_gsi1"] = (data.get("date"), data.get("time"))

    monkeypatch.setattr(db, "get_event_from_config",
                        lambda guid: dict(events[guid]) if guid in events else None)
    monkeypatch.setattr(db, "get_all_events",
                        lambda include_past=False: [dict(e) for e in events.values()])
    monkeypatch.setattr(db, "update_event", _update)
    monkeypatch.setattr(db, "get_all_categories", lambda: dict(CATEGORIES))
    return events


def overlay_of(store, guid):
    return store[guid].get("overrides") or {}


# ── The field allowlist ────────────────────────────────────────────
# Before the lift there was only a denylist, so `time`, `url`, `description`
# and even a misspelling were all writable and would sit in the overlay
# doing nothing.


def test_an_editable_field_is_written(store):
    db.set_event_overlay("g1", {"title": "Corrected"}, "why")
    assert overlay_of(store, "g1")["title"] == "Corrected"


@pytest.mark.parametrize("field", sorted(db.OVERLAY_PROTECTED_FIELDS))
def test_every_protected_field_is_refused(store, field):
    with pytest.raises(ValueError, match="protected"):
        db.set_event_overlay("g1", {field: "x"}, "nope")


def test_a_misspelled_field_is_refused_rather_than_silently_stored(store):
    with pytest.raises(ValueError, match="Not an overlay-editable field"):
        db.set_event_overlay("g1", {"titel": "typo"}, "nope")


def test_a_private_key_cannot_be_forged(store):
    # Otherwise a caller could plant a run stamp and hijack a revert.
    with pytest.raises(ValueError, match="reserved"):
        db.set_event_overlay("g1", {"_qa_run": {"run_id": "forged"}}, "nope")


def test_clear_is_checked_against_the_same_allowlist(store):
    with pytest.raises(ValueError, match="protected"):
        db.set_event_overlay("g1", {}, "nope", clear=("source",))


def test_an_unknown_event_raises(store):
    with pytest.raises(ValueError, match="No such event"):
        db.set_event_overlay("nope", {"hidden": True}, "x")


# ── Value validation ───────────────────────────────────────────────


def test_a_known_category_is_accepted(store):
    db.set_event_overlay("g1", {"categories": ["ai", "cloud"]}, "why")
    assert overlay_of(store, "g1")["categories"] == ["ai", "cloud"]


def test_an_invented_category_slug_is_refused(store):
    # A slug that does not exist renders as nothing at all.
    with pytest.raises(ValueError, match="Unknown category slug"):
        db.set_event_overlay("g1", {"categories": ["ai", "nonsense"]}, "why")


def test_a_duplicate_of_pointing_at_a_real_event_is_accepted(store):
    db.set_event_overlay("g2", {"duplicate_of": "g1"}, "same event")
    assert overlay_of(store, "g2")["duplicate_of"] == "g1"


def test_a_dangling_duplicate_of_is_refused(store):
    # calgen ignores a duplicate_of whose target is not in the corpus, so the
    # merge would look done and the event would keep showing.
    with pytest.raises(ValueError, match="No such event to merge into"):
        db.set_event_overlay("g1", {"duplicate_of": "ghost"}, "merge")


def test_an_event_cannot_be_a_duplicate_of_itself(store):
    with pytest.raises(ValueError, match="cannot be a duplicate of itself"):
        db.set_event_overlay("g1", {"duplicate_of": "g1"}, "merge")


def test_clearing_duplicate_of_needs_no_target(store):
    db.set_event_overlay("g2", {"duplicate_of": "g1"}, "merge")
    db.set_event_overlay("g2", {}, "not a duplicate", clear=("duplicate_of",))
    assert "duplicate_of" not in overlay_of(store, "g2")


# ── GSI1 ───────────────────────────────────────────────────────────


def test_the_write_carries_the_events_real_date_and_time(store):
    # update_event rewrites GSI1 from these unconditionally; omitting them
    # writes 'DATE#' and corrupts the index.
    db.set_event_overlay("g1", {"hidden": True}, "why")
    assert store["g1"]["_gsi1"] == ("2026-06-23", "18:00")


def test_an_event_with_no_time_still_gets_a_usable_gsi1_key(store):
    store["g1"]["time"] = None
    db.set_event_overlay("g1", {"hidden": True}, "why")
    assert store["g1"]["_gsi1"] == ("2026-06-23", "00:00")


# ── Provenance ─────────────────────────────────────────────────────


def test_a_hand_edit_records_who_made_it(store):
    db.set_event_overlay("g1", {"title": "By Hand"}, "why",
                         actor="ross@example.com")
    book = db.overlay_bookkeeping(overlay_of(store, "g1"))
    assert book["edited_by"] == "ross@example.com"
    assert book["edited_at"].endswith("Z")
    assert book["run_id"] is None


def test_an_agent_run_is_attributed_to_the_agent(store):
    db.set_event_overlay("g1", {"hidden": True}, "out of area", run_id="r1")
    book = db.overlay_bookkeeping(overlay_of(store, "g1"))
    assert book["edited_by"] == "agent:qa"
    assert book["run_id"] == "r1"


def test_per_field_provenance_distinguishes_the_two_authors(store):
    # The question a reviewer actually asks: which half of this overlay was
    # the agent's?
    db.set_event_overlay("g1", {"location": "Arlington, VA"}, "agent fix",
                         run_id="r1")
    db.set_event_overlay("g1", {"title": "Human Title"}, "my fix",
                         actor="ross@example.com")
    edits = db.overlay_bookkeeping(overlay_of(store, "g1"))["field_edits"]
    assert edits["location"]["by"] == "agent:qa"
    assert edits["title"]["by"] == "ross@example.com"


def test_clearing_a_field_drops_its_provenance(store):
    db.set_event_overlay("g1", {"title": "x"}, "why", actor="a@b.c")
    db.set_event_overlay("g1", {}, "revert", clear=("title",))
    assert "title" not in db.overlay_bookkeeping(overlay_of(store, "g1"))["field_edits"]


def test_bookkeeping_never_leaks_into_the_rendered_overlay(store):
    db.set_event_overlay("g1", {"title": "x"}, "why", actor="a@b.c", run_id="r1")
    assert db.public_overlay(overlay_of(store, "g1")) == {"title": "x"}


# ── Revisions and the conditional write ────────────────────────────


def test_the_first_write_is_revision_one(store):
    result = db.set_event_overlay("g1", {"title": "x"}, "why")
    assert result["rev"] == 1


def test_each_write_bumps_the_revision(store):
    db.set_event_overlay("g1", {"title": "x"}, "why")
    result = db.set_event_overlay("g1", {"location": "y"}, "why",
                                  expected_rev=1)
    assert result["rev"] == 2


def test_editing_from_a_stale_form_conflicts_rather_than_clobbering(store):
    db.set_event_overlay("g1", {"title": "first"}, "why")       # rev 1
    db.set_event_overlay("g1", {"location": "second"}, "why", expected_rev=1)

    # A second browser still holding rev 1 must not win.
    with pytest.raises(db.OverlayConflict) as caught:
        db.set_event_overlay("g1", {"title": "stale"}, "why", expected_rev=1)

    assert caught.value.current_rev == 2
    assert caught.value.current_overlay["title"] == "first"
    assert overlay_of(store, "g1")["title"] == "first"


def test_the_conflict_carries_the_current_overlay_for_a_diff(store):
    db.set_event_overlay("g1", {"title": "theirs"}, "why")
    with pytest.raises(db.OverlayConflict) as caught:
        db.set_event_overlay("g1", {"title": "mine"}, "why", expected_rev=None)
    assert caught.value.guid == "g1"
    assert caught.value.current_overlay == {"title": "theirs"}


def test_expecting_no_overlay_succeeds_when_there_is_none(store):
    db.set_event_overlay("g1", {"title": "x"}, "why", expected_rev=None)
    assert overlay_of(store, "g1")["title"] == "x"


def test_an_unconditional_write_merges_instead_of_conflicting(store):
    # The agent and bulk paths pass no expected_rev: a field-level merge is
    # exactly the intended outcome and there is no human to consult.
    db.set_event_overlay("g1", {"title": "human"}, "why", actor="a@b.c")
    db.set_event_overlay("g1", {"hidden": True}, "agent", run_id="r1")
    rendered = db.public_overlay(overlay_of(store, "g1"))
    assert rendered == {"title": "human", "hidden": True}


def test_an_unconditional_write_retries_a_lost_race(store, monkeypatch):
    """The first attempt loses, the retry re-reads and wins."""
    real = db.update_event
    calls = {"n": 0}

    def flaky(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate someone else landing a write in between.
            real("g1", data, {"_rev": 9, "title": "theirs"},
                 expect_overrides_rev=db._UNSET)
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "lost"}}, "UpdateItem")
        return real(guid, data, overrides,
                    expect_overrides_rev=expect_overrides_rev)

    monkeypatch.setattr(db, "update_event", flaky)
    db.set_event_overlay("g1", {"hidden": True}, "agent")

    # The winner's field survived and ours was merged on top of it.
    rendered = db.public_overlay(overlay_of(store, "g1"))
    assert rendered == {"title": "theirs", "hidden": True}
    assert calls["n"] == 2


def test_a_non_conditional_error_is_not_swallowed(store, monkeypatch):
    def broken(*a, **kw):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "slow down"}}, "UpdateItem")

    monkeypatch.setattr(db, "update_event", broken)
    with pytest.raises(ClientError):
        db.set_event_overlay("g1", {"hidden": True}, "why")


# ── effective_event ────────────────────────────────────────────────


def test_the_overlay_wins_over_the_record(store):
    db.set_event_overlay("g1", {"title": "Overlaid"}, "why")
    effective = db.effective_event(db.get_event_from_config("g1"))
    assert effective["title"] == "Overlaid"


def test_a_field_with_no_overlay_falls_back_to_the_record(store):
    db.set_event_overlay("g1", {"hidden": True}, "why")
    assert db.effective_event(db.get_event_from_config("g1"))["title"] == "Feed Title"


def test_effective_event_ignores_junk_outside_the_allowlist(store):
    # A field written before the allowlist existed must not change what a
    # caller sees.
    store["g1"]["overrides"] = {"group": "Hijacked", "_rev": 1}
    assert db.effective_event(store["g1"])["group"] == "NoVABeerSec"


def test_effective_event_on_an_event_with_no_overlay(store):
    assert db.effective_event(store["g1"])["title"] == "Feed Title"


# ── clear_event_overlay ────────────────────────────────────────────


def test_clearing_removes_every_rendered_field(store):
    db.set_event_overlay("g1", {"title": "x", "hidden": True}, "why")
    db.clear_event_overlay("g1", actor="a@b.c", expected_rev=1)
    assert db.public_overlay(overlay_of(store, "g1")) == {}


def test_clearing_keeps_an_audit_trail(store):
    # Deliberately not `overrides = {}`: who cleared it survives, and both
    # public_overlay and the exporter read an all-private map as no overlay.
    db.set_event_overlay("g1", {"title": "x"}, "why")
    db.clear_event_overlay("g1", actor="ross@example.com", expected_rev=1)
    book = db.overlay_bookkeeping(overlay_of(store, "g1"))
    assert book["edited_by"] == "ross@example.com"
    assert book["rev"] == 2


def test_clearing_drops_the_run_stamp(store):
    # There is nothing left for a revert to restore.
    db.set_event_overlay("g1", {"hidden": True}, "why", run_id="r1")
    db.clear_event_overlay("g1", expected_rev=1)
    assert db.overlay_bookkeeping(overlay_of(store, "g1"))["run_id"] is None
    assert db.describe_qa_run("r1") == []


def test_clearing_conflicts_on_a_stale_revision(store):
    db.set_event_overlay("g1", {"title": "x"}, "why")
    with pytest.raises(db.OverlayConflict):
        db.clear_event_overlay("g1", expected_rev=None)


# ── QA runs ────────────────────────────────────────────────────────


def test_a_run_is_described_by_what_it_wrote(store):
    db.set_event_overlay("g1", {"title": "Agent"}, "trimmed", run_id="r1")
    row = db.describe_qa_run("r1")[0]
    assert row["guid"] == "g1"
    assert row["applied"] == {"title": "Agent"}
    assert row["comment"] == "trimmed"
    assert row["removes"] == ["title"]


def test_a_hand_edit_is_not_part_of_any_run(store):
    db.set_event_overlay("g1", {"title": "By Hand"}, "why", actor="a@b.c")
    assert db.describe_qa_run("r1") == []


def test_revert_records_who_reverted(store):
    db.set_event_overlay("g1", {"hidden": True}, "why", run_id="r1")
    db.revert_qa_run("r1", actor="ross@example.com")
    assert db.overlay_bookkeeping(
        overlay_of(store, "g1"))["edited_by"] == "ross@example.com"


def test_revert_bumps_the_revision_rather_than_resetting_it(store):
    # A stale form open across a revert must still conflict.
    db.set_event_overlay("g1", {"hidden": True}, "why", run_id="r1")
    db.revert_qa_run("r1")
    assert db.overlay_bookkeeping(overlay_of(store, "g1"))["rev"] == 2


# ── Bulk moderation ────────────────────────────────────────────────
# These used to write top-level columns, which did nothing at all for iCal
# events — the source that makes up most of the calendar.


def test_bulk_hide_writes_an_overlay_not_a_column(store):
    db.bulk_delete_events(["g1", "g2"], "ross@example.com")
    assert overlay_of(store, "g1")["hidden"] is True
    assert overlay_of(store, "g2")["hidden"] is True
    # The top-level column is untouched: for an iCal event it never reaches
    # calgen, so writing it would be theatre.
    assert "hidden" not in {k: v for k, v in store["g1"].items()
                            if k != "overrides"}


def test_bulk_hide_reports_what_it_did(store):
    result = db.bulk_delete_events(["g1", "ghost"], "ross@example.com")
    assert result["updated"] == 1
    assert result["failed"][0]["guid"] == "ghost"


def test_one_bad_guid_does_not_sink_the_batch(store):
    db.bulk_delete_events(["ghost", "g1"], "ross@example.com")
    assert overlay_of(store, "g1")["hidden"] is True


def test_bulk_hide_is_attributed_to_the_person(store):
    db.bulk_delete_events(["g1"], "ross@example.com")
    book = db.overlay_bookkeeping(overlay_of(store, "g1"))
    assert book["edited_by"] == "ross@example.com"
    assert book["run_id"] is None


def test_bulk_unhide_clears_rather_than_setting_false(store):
    # Falling back to the source beats carrying an override that agrees.
    db.bulk_delete_events(["g1"], "a@b.c")
    db.bulk_unhide_events(["g1"], "a@b.c")
    assert "hidden" not in db.public_overlay(overlay_of(store, "g1"))


def test_bulk_hide_writes_the_real_gsi1_key(store):
    # The old version passed no date and wrote the literal 'DATE#'.
    db.bulk_delete_events(["g1"], "a@b.c")
    assert store["g1"]["_gsi1"] == ("2026-06-23", "18:00")


def test_bulk_set_category_keeps_the_ones_already_there(store):
    db.set_event_overlay("g1", {"categories": ["ai"]}, "first")
    db.bulk_set_category(["g1"], "cloud", "a@b.c")
    assert overlay_of(store, "g1")["categories"] == ["ai", "cloud"]


def test_bulk_set_category_is_idempotent(store):
    db.bulk_set_category(["g1"], "ai", "a@b.c")
    result = db.bulk_set_category(["g1"], "ai", "a@b.c")
    assert result["updated"] == 0
    assert overlay_of(store, "g1")["categories"] == ["ai"]


def test_bulk_set_category_reads_through_the_overlay(store):
    # Not the record: the aggregator resets the record's categories from the
    # group every four hours, so the overlay is the live value.
    store["g1"]["categories"] = ["stale-from-group"]
    db.set_event_overlay("g1", {"categories": ["ai"]}, "corrected")
    db.bulk_set_category(["g1"], "cloud", "a@b.c")
    assert overlay_of(store, "g1")["categories"] == ["ai", "cloud"]


def test_bulk_set_category_refuses_an_invented_slug(store):
    result = db.bulk_set_category(["g1"], "nonsense", "a@b.c")
    assert result["updated"] == 0
    assert "Unknown category slug" in result["failed"][0]["error"]


def test_bulk_combine_points_children_at_the_canonical_event(store):
    db.bulk_combine_events(["g2"], "g1", "a@b.c")
    assert overlay_of(store, "g2")["duplicate_of"] == "g1"


def test_bulk_combine_also_hides_the_children(store):
    # Redundant at render, but if the canonical later leaves its feed the
    # dangling duplicate_of stops being honoured and the child would resurrect.
    db.bulk_combine_events(["g2"], "g1", "a@b.c")
    assert overlay_of(store, "g2")["hidden"] is True


def test_bulk_combine_never_merges_the_canonical_into_itself(store):
    result = db.bulk_combine_events(["g1", "g2"], "g1", "a@b.c")
    assert result["updated"] == 1
    assert "duplicate_of" not in overlay_of(store, "g1")


def test_bulk_combine_refuses_a_target_that_does_not_exist(store):
    result = db.bulk_combine_events(["g2"], "ghost", "a@b.c")
    assert result["updated"] == 0
    assert "No such event to merge into" in result["failed"][0]["error"]


def test_bulk_unmerge_undoes_both_fields(store):
    db.bulk_combine_events(["g2"], "g1", "a@b.c")
    db.bulk_unmerge_events(["g2"], "a@b.c")
    assert db.public_overlay(overlay_of(store, "g2")) == {}


# ── Value types ────────────────────────────────────────────────────
# The name allowlist does not constrain values. Two manual events were found in
# production carrying {'title': True, 'hidden': True, 'categories': True} —
# checkbox values from the old admin form, inert only because overlays did not
# apply to manual events then (next_dctech_events-9jk).


def test_the_exact_junk_found_in_production_is_now_refused(store):
    with pytest.raises(ValueError, match="title must be a string"):
        db.set_event_overlay("g1", {"title": True, "hidden": True,
                                    "categories": True}, "why")


def test_a_boolean_is_not_accepted_for_a_string_field(store):
    # bool is not a str subclass, but spelling this out guards the reverse
    # mistake of accepting True for a text field via truthiness.
    with pytest.raises(ValueError, match="not true/false"):
        db.set_event_overlay("g1", {"location": True}, "why")


def test_a_string_is_not_accepted_for_a_boolean_field(store):
    # The form-encoded "true" that started all this.
    with pytest.raises(ValueError, match="hidden must be true or false"):
        db.set_event_overlay("g1", {"hidden": "true"}, "why")


def test_categories_must_be_a_list(store):
    with pytest.raises(ValueError, match="categories must be a list"):
        db.set_event_overlay("g1", {"categories": "ai"}, "why")


def test_categories_must_hold_strings(store):
    with pytest.raises(ValueError, match="list of strings"):
        db.set_event_overlay("g1", {"categories": ["ai", 7]}, "why")


def test_a_number_is_not_accepted_for_a_string_field(store):
    with pytest.raises(ValueError, match="title must be a string"):
        db.set_event_overlay("g1", {"title": 42}, "why")


def test_none_is_allowed_and_means_no_value(store):
    # Distinct from clear(), which removes the key; a stored None is how a
    # caller says "override this to empty".
    db.set_event_overlay("g1", {"location": None}, "no venue given")
    assert overlay_of(store, "g1")["location"] is None


def test_well_typed_values_still_pass(store):
    db.set_event_overlay("g1", {"title": "Real Title", "hidden": True,
                                "all_day": False, "categories": ["ai"]},
                         "why")
    rendered = db.public_overlay(overlay_of(store, "g1"))
    assert rendered == {"title": "Real Title", "hidden": True,
                        "all_day": False, "categories": ["ai"]}


def test_every_editable_field_has_a_declared_type(store):
    # A field added to the allowlist without a type would silently accept
    # anything, which is the hole this closes.
    assert set(db.OVERLAY_EDITABLE_FIELDS) == set(db._OVERLAY_FIELD_TYPES)
