"""Tests for public event corrections.

A correction is a pending CORRECTION# proposal, applied to an event's overlay
only once a moderator approves it — approval is a thin wrapper around
set_event_overlay, so it must behave exactly like a hand edit once approved.
The field allowlist is the one genuinely new rule this feature adds: an iCal
event may only have description/location/url corrected (its date/time stay
locked to the feed, which OVERLAY_PROTECTED_FIELDS already enforces
unconditionally for every source — see test_overlay.py), while a manual or
submitted event may also have time/end_time corrected.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_corrections.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
    """In-memory stand-in for CORRECTION# storage: put/get/update/query.

    query() only needs to resolve the simple `Key(name).eq(value)` conditions
    this module actually issues (GSI1PK/GSI3PK equality) — boto3's Equals
    condition exposes its attribute name and target value directly, so no
    real expression evaluation is needed. update_item() only needs to handle
    the simple "SET a = :a, b = :b" expressions this module writes.
    """

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

    def query(self, KeyConditionExpression, **kwargs):
        attr_name = KeyConditionExpression._values[0].name
        target = KeyConditionExpression._values[1]
        rows = [i for i in self.items.values() if i.get(attr_name) == target]
        return {"Items": rows}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None):
        item = self.items[self._k(Key)]
        names = ExpressionAttributeNames or {}
        set_clause = UpdateExpression.split("SET ", 1)[1]
        for assignment in set_clause.split(", "):
            lhs, rhs = (part.strip() for part in assignment.split("="))
            field = names.get(lhs, lhs)
            item[field] = ExpressionAttributeValues[rhs]


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    return fake


@pytest.fixture
def events(monkeypatch):
    """The EVENT# side: same shape as test_overlay.py's `store`, so
    set_event_overlay (called by approve_correction) behaves identically to
    a hand edit — including its conditional-write check.
    """
    store = {
        "ical1": {"guid": "ical1", "title": "iCal Event", "date": "2026-06-23",
                  "time": "18:00", "source": "ical", "group": "Some Group"},
        "manual1": {"guid": "manual1", "title": "Manual Event",
                    "date": "2026-06-24", "time": "19:00", "source": "manual"},
    }

    def _update(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        store[guid]["overrides"] = overrides

    monkeypatch.setattr(db, "get_event_from_config",
                        lambda guid: dict(store[guid]) if guid in store else None)
    monkeypatch.setattr(db, "update_event", _update)
    return store


def overlay_of(events, guid):
    return events[guid].get("overrides") or {}


# ── The field allowlist ────────────────────────────────────────────


def test_ical_events_may_only_correct_description_location_url():
    assert db.correction_allowed_fields("ical") == ("description", "location", "url")


def test_manual_and_submitted_events_may_also_correct_time():
    for source in ("manual", "submitted"):
        allowed = db.correction_allowed_fields(source)
        assert "time" in allowed and "end_time" in allowed


def test_an_unknown_source_falls_back_to_the_narrow_ical_set():
    assert db.correction_allowed_fields("something-new") == \
        db.correction_allowed_fields("ical")


def test_a_time_correction_against_an_ical_event_is_refused():
    with pytest.raises(ValueError, match="Not correctable"):
        db.check_correction_fields("ical", {"time": "19:00"})


def test_a_time_correction_against_a_manual_event_is_accepted():
    db.check_correction_fields("manual", {"time": "19:00"})  # does not raise


def test_description_location_url_are_always_correctable():
    db.check_correction_fields(
        "ical", {"description": "x", "location": "y", "url": "z"})


def test_a_bad_value_type_is_still_refused_for_an_allowed_field():
    # Proves check_correction_fields actually calls _check_overlay_types,
    # not just an allowlist of names.
    with pytest.raises(ValueError, match="must be a string"):
        db.check_correction_fields("ical", {"description": True})


def test_date_needs_no_special_handling_it_is_already_universally_protected():
    # date/end_date are in OVERLAY_PROTECTED_FIELDS for every source; a
    # correction never even offers them (see routes/corrections.py), and
    # set_event_overlay would refuse them anyway if one were smuggled through.
    assert "date" not in db.correction_allowed_fields("manual")
    assert "date" not in db.correction_allowed_fields("ical")


# ── create_correction / lookups ────────────────────────────────────


def test_create_correction_against_an_unknown_event_raises(events):
    with pytest.raises(ValueError, match="No such event"):
        db.create_correction("ghost", {"description": "x"}, "why", "a@b.c")


def test_create_correction_snapshots_the_events_source_and_title(table, events):
    correction_id = db.create_correction(
        "ical1", {"description": "x"}, "wrong description", "a@b.c")
    correction = db.get_correction(correction_id)
    assert correction["target_source"] == "ical"
    assert correction["target_title"] == "iCal Event"
    assert correction["status"] == "pending"
    assert correction["reason"] == "wrong description"


def test_get_corrections_by_status_does_not_pick_up_a_comingled_draft(table, events):
    # DRAFT# items use the identical GSI1PK = STATUS#{status} convention —
    # this is the regression test for that collision.
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending",
    })
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    pending = db.get_corrections_by_status("pending")

    assert [c["id"] for c in pending] == [correction_id]


def test_get_corrections_by_submitter_does_not_pick_up_a_comingled_draft(table, events):
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI3PK": "USER#a@b.c", "GSI3SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending",
    })
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    mine = db.get_corrections_by_submitter("a@b.c")

    assert [c["id"] for c in mine] == [correction_id]


def test_get_correction_on_an_unknown_id_returns_none(table):
    assert db.get_correction("ghost123") is None


# ── The collision runs both directions ─────────────────────────────
# Found live: a correction created during testing showed up in the DRAFT#
# queue as an "Untitled" row with no details, and rejecting it 404'd ("Draft
# not found") because get_draft looks it up at PK=DRAFT#{id}, which doesn't
# exist for a correction stored at PK=CORRECTION#{id}. get_corrections_by_*
# already filtered out DRAFT# items; get_drafts_by_* needed the same filter
# against CORRECTION# items and didn't have it.


def test_get_drafts_by_status_does_not_pick_up_a_comingled_correction(table, events):
    db.create_correction("manual1", {"time": "20:00"}, "wrong time", "a@b.c")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending", "title": "Real Draft",
    })

    pending = db.get_drafts_by_status("pending")

    assert [d["id"] for d in pending] == ["abc12345"]


def test_get_drafts_by_submitter_does_not_pick_up_a_comingled_correction(table, events):
    db.create_correction("manual1", {"time": "20:00"}, "wrong time", "a@b.c")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI3PK": "USER#a@b.c", "GSI3SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending", "title": "Real Draft",
    })

    mine = db.get_drafts_by_submitter("a@b.c")

    assert [d["id"] for d in mine] == ["abc12345"]


# ── approve_correction ─────────────────────────────────────────────


def test_approving_merges_the_fields_into_the_events_overlay(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    db.approve_correction(correction_id, "admin@example.com")

    assert overlay_of(events, "manual1")["time"] == "20:00"
    assert db.get_correction(correction_id)["status"] == "approved"


def test_approval_comment_names_the_submitter_and_reason(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "organizer moved it", "reporter@example.com")

    db.approve_correction(correction_id, "admin@example.com")

    assert overlay_of(events, "manual1")["_comment"] == (
        "Public correction from reporter@example.com: organizer moved it")
    assert overlay_of(events, "manual1")["_edited_by"] == "admin@example.com"


def test_approving_an_unknown_correction_raises(table):
    with pytest.raises(ValueError, match="No such correction"):
        db.approve_correction("ghost123", "admin@example.com")


def test_approving_an_already_resolved_correction_raises_and_stays_resolved(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "why", "a@b.c")
    db.approve_correction(correction_id, "admin@example.com")

    with pytest.raises(ValueError, match="already approved"):
        db.approve_correction(correction_id, "admin@example.com")


def test_approving_against_a_deleted_target_event_raises_and_stays_pending(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "why", "a@b.c")
    del events["manual1"]

    with pytest.raises(ValueError, match="no longer exists"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


def test_approving_after_the_events_source_narrowed_raises_and_stays_pending(table, events):
    # Submitted with a time change while the event was still 'manual'; an
    # admin re-sources it to 'ical' before anyone reviews the correction.
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "why", "a@b.c")
    events["manual1"]["source"] = "ical"

    with pytest.raises(ValueError, match="Not correctable"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


# ── reject_correction ──────────────────────────────────────────────


def test_rejecting_marks_status_with_no_overlay_write(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "why", "a@b.c")

    db.reject_correction(correction_id, "admin@example.com", "not a real issue")

    correction = db.get_correction(correction_id)
    assert correction["status"] == "rejected"
    assert correction["rejection_reason"] == "not a real issue"
    assert overlay_of(events, "manual1") == {}


def test_rejecting_an_unknown_correction_raises(table):
    with pytest.raises(ValueError, match="No such correction"):
        db.reject_correction("ghost123", "admin@example.com")


def test_rejecting_an_already_resolved_correction_raises(table, events):
    correction_id = db.create_correction(
        "manual1", {"time": "20:00"}, "why", "a@b.c")
    db.reject_correction(correction_id, "admin@example.com")

    with pytest.raises(ValueError, match="already rejected"):
        db.reject_correction(correction_id, "admin@example.com")
