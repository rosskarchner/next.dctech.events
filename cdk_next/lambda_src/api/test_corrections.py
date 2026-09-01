"""Tests for public corrections: events, recurring series, and recurring
occurrences.

A correction is a pending CORRECTION# proposal, applied to its target only
once a moderator approves it. `target_type` decides the apply path:
'event' merges into the event's overlay via the existing set_event_overlay
(so it must behave exactly like a hand edit once approved); 'recurring_series'
merges directly onto the RECURRING#{slug} record's top-level fields, the same
full-replace update_recurring_event already performs by hand;
'recurring_instance' writes a scoped RECURRING#{slug}/OVERRIDE#{date} row.

The field allowlist is the one genuinely new rule events add: an iCal event
may only have description/location/url corrected (its date/time stay locked
to the feed, which OVERLAY_PROTECTED_FIELDS already enforces unconditionally
for every source — see test_overlay.py), while a manual or submitted event
may also have time/end_time corrected. Recurring targets (series or
instance) have one allowlist regardless of source, since there's no
ical/manual/submitted split for a recurring definition.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_corrections.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
    """In-memory stand-in for CORRECTION#/RECURRING# storage: put/get/query/
    update.

    query() resolves either a bare `Key(name).eq(value)` condition (GSI1PK/
    GSI3PK equality) or a compound `Key(...).eq(...) & Key(...).begins_with(...)`
    (used by get_recurring_instance_overrides's PK/SK query) — boto3's
    Equals/BeginsWith/And conditions expose their operands directly, so no
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


@pytest.fixture
def recurring(monkeypatch):
    """The RECURRING# side: get_recurring_event/put_recurring_event, so
    merge_recurring_event_fields (called by approve_correction for
    recurring_series) behaves like the MCP update_recurring_event tool would.
    """
    store = {
        "weekly-thing": {
            "id": "weekly-thing", "title": "Weekly Thing",
            "date": "2026-06-01", "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "time": "18:00", "location": "Old Place",
            "url": "https://example.com/weekly",
        },
    }

    def _put(slug, data):
        merged = dict(data)
        merged["id"] = slug
        store[slug] = merged
        return slug

    monkeypatch.setattr(db, "get_recurring_event",
                        lambda slug: dict(store[slug]) if slug in store else None)
    monkeypatch.setattr(db, "put_recurring_event", _put)
    return store


def overlay_of(events, guid):
    return events[guid].get("overrides") or {}


# ── The field allowlist — events ─────────────────────────────────────


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
        db.check_correction_fields("event", {"time": "19:00"}, source="ical")


def test_a_time_correction_against_a_manual_event_is_accepted():
    db.check_correction_fields("event", {"time": "19:00"}, source="manual")  # no raise


def test_description_location_url_are_always_correctable():
    db.check_correction_fields(
        "event", {"description": "x", "location": "y", "url": "z"}, source="ical")


def test_a_bad_value_type_is_still_refused_for_an_allowed_field():
    # Proves check_correction_fields actually calls _check_overlay_types,
    # not just an allowlist of names.
    with pytest.raises(ValueError, match="must be a string"):
        db.check_correction_fields("event", {"description": True}, source="ical")


def test_date_needs_no_special_handling_it_is_already_universally_protected():
    # date/end_date are in OVERLAY_PROTECTED_FIELDS for every source; a
    # correction never even offers them (see routes/corrections.py), and
    # set_event_overlay would refuse them anyway if one were smuggled through.
    assert "date" not in db.correction_allowed_fields("manual")
    assert "date" not in db.correction_allowed_fields("ical")


# ── The field allowlist — recurring series/instance ──────────────────


def test_recurring_series_may_correct_description_location_url_time():
    assert set(db.correction_allowed_fields_for_target("recurring_series")) == \
        {"description", "location", "url", "time"}


def test_recurring_instance_has_the_same_allowlist_as_the_series():
    assert db.correction_allowed_fields_for_target("recurring_instance") == \
        db.correction_allowed_fields_for_target("recurring_series")


def test_recurring_series_may_not_correct_rrule_or_categories():
    with pytest.raises(ValueError, match="Not correctable"):
        db.check_correction_fields("recurring_series", {"rrule": "FREQ=DAILY"})
    with pytest.raises(ValueError, match="Not correctable"):
        db.check_correction_fields("recurring_series", {"categories": ["ai"]})


def test_recurring_series_has_no_end_time_field():
    assert "end_time" not in db.correction_allowed_fields_for_target("recurring_series")


# ── create_correction — events ───────────────────────────────────────


def test_create_correction_against_an_unknown_event_raises(events):
    with pytest.raises(ValueError, match="No such event"):
        db.create_correction("event", "ghost", {"description": "x"}, "why", "a@b.c")


def test_create_correction_snapshots_the_events_source_and_title(table, events):
    correction_id = db.create_correction(
        "event", "ical1", {"description": "x"}, "wrong description", "a@b.c")
    correction = db.get_correction(correction_id)
    assert correction["target_type"] == "event"
    assert correction["target_id"] == "ical1"
    assert correction["target_source"] == "ical"
    assert correction["target_title"] == "iCal Event"
    assert correction["status"] == "pending"
    assert correction["reason"] == "wrong description"


def test_create_correction_rejects_an_unknown_target_type(table, events):
    with pytest.raises(ValueError, match="Unknown correction target type"):
        db.create_correction("bogus", "ical1", {"description": "x"}, "why", "a@b.c")


def test_get_corrections_by_status_does_not_pick_up_a_comingled_draft(table, events):
    # DRAFT# items use the identical GSI1PK = STATUS#{status} convention —
    # this is the regression test for that collision.
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending",
    })
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    pending = db.get_corrections_by_status("pending")

    assert [c["id"] for c in pending] == [correction_id]


def test_get_corrections_by_submitter_does_not_pick_up_a_comingled_draft(table, events):
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI3PK": "USER#a@b.c", "GSI3SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending",
    })
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    mine = db.get_corrections_by_submitter("a@b.c")

    assert [c["id"] for c in mine] == [correction_id]


def test_get_correction_on_an_unknown_id_returns_none(table):
    assert db.get_correction("ghost123") is None


def test_legacy_correction_rows_without_target_type_are_read_back_as_event_type(table):
    # Rows written before target_type/target_id existed carried only
    # target_guid — no migration script, backfilled at read time.
    table.put_item(Item={
        "PK": "CORRECTION#legacy01", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "target_guid": "ical1", "target_source": "ical", "target_title": "iCal Event",
        "fields": {"description": "x"}, "reason": "why",
        "submitter_email": "a@b.c", "created_at": "2026-01-01T00:00:00Z",
        "status": "pending",
    })
    correction = db.get_correction("legacy01")
    assert correction["target_type"] == "event"
    assert correction["target_id"] == "ical1"


# ── create_correction — recurring series/instance ────────────────────


def test_create_correction_against_an_unknown_recurring_series_raises(recurring):
    with pytest.raises(ValueError, match="No such recurring event"):
        db.create_correction(
            "recurring_series", "ghost-series", {"location": "x"}, "why", "a@b.c")


def test_create_correction_against_a_recurring_instance_requires_target_date(recurring):
    with pytest.raises(ValueError, match="target_date is required"):
        db.create_correction(
            "recurring_instance", "weekly-thing", {"location": "x"}, "why", "a@b.c")


def test_create_correction_against_a_recurring_instance_with_a_bad_date_raises(recurring):
    with pytest.raises(ValueError, match="ISO-8601"):
        db.create_correction(
            "recurring_instance", "weekly-thing", {"location": "x"}, "why", "a@b.c",
            target_date="not-a-date")


def test_create_correction_snapshots_the_series_title(table, recurring):
    correction_id = db.create_correction(
        "recurring_series", "weekly-thing", {"location": "New Place"},
        "venue changed", "a@b.c")
    correction = db.get_correction(correction_id)
    assert correction["target_type"] == "recurring_series"
    assert correction["target_id"] == "weekly-thing"
    assert correction["target_title"] == "Weekly Thing"
    assert "target_date" not in correction


def test_create_correction_against_an_instance_stores_the_date(table, recurring):
    correction_id = db.create_correction(
        "recurring_instance", "weekly-thing", {"description": "moved this week"},
        "why", "a@b.c", target_date="2026-06-08")
    correction = db.get_correction(correction_id)
    assert correction["target_type"] == "recurring_instance"
    assert correction["target_date"] == "2026-06-08"


# ── approve_correction — event (unchanged path) ──────────────────────


def test_approving_merges_the_fields_into_the_events_overlay(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "wrong time", "a@b.c")

    db.approve_correction(correction_id, "admin@example.com")

    assert overlay_of(events, "manual1")["time"] == "20:00"
    assert db.get_correction(correction_id)["status"] == "approved"


def test_approval_comment_names_the_submitter_and_reason(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "organizer moved it",
        "reporter@example.com")

    db.approve_correction(correction_id, "admin@example.com")

    assert overlay_of(events, "manual1")["_comment"] == (
        "Public correction from reporter@example.com: organizer moved it")
    assert overlay_of(events, "manual1")["_edited_by"] == "admin@example.com"


def test_approving_an_unknown_correction_raises(table):
    with pytest.raises(ValueError, match="No such correction"):
        db.approve_correction("ghost123", "admin@example.com")


def test_approving_an_already_resolved_correction_raises_and_stays_resolved(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "why", "a@b.c")
    db.approve_correction(correction_id, "admin@example.com")

    with pytest.raises(ValueError, match="already approved"):
        db.approve_correction(correction_id, "admin@example.com")


def test_approving_against_a_deleted_target_event_raises_and_stays_pending(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "why", "a@b.c")
    del events["manual1"]

    with pytest.raises(ValueError, match="no longer exists"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


def test_approving_after_the_events_source_narrowed_raises_and_stays_pending(table, events):
    # Submitted with a time change while the event was still 'manual'; an
    # admin re-sources it to 'ical' before anyone reviews the correction.
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "why", "a@b.c")
    events["manual1"]["source"] = "ical"

    with pytest.raises(ValueError, match="Not correctable"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


# ── approve_correction — recurring series ────────────────────────────


def test_approving_a_series_correction_merges_fields_directly_onto_the_record(
        table, recurring):
    correction_id = db.create_correction(
        "recurring_series", "weekly-thing", {"location": "New Place"},
        "venue changed", "a@b.c")

    db.approve_correction(correction_id, "admin@example.com")

    assert recurring["weekly-thing"]["location"] == "New Place"
    assert db.get_correction(correction_id)["status"] == "approved"
    # No overlay/_rev bookkeeping introduced — this is a direct field merge,
    # not routed through the EVENT#-style overlay system.
    assert "overrides" not in recurring["weekly-thing"]
    assert "_rev" not in recurring["weekly-thing"]


def test_approving_against_a_deleted_recurring_series_raises_and_stays_pending(
        table, recurring):
    correction_id = db.create_correction(
        "recurring_series", "weekly-thing", {"location": "New Place"}, "why", "a@b.c")
    del recurring["weekly-thing"]

    with pytest.raises(ValueError, match="no longer exists"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


# ── approve_correction — recurring instance ──────────────────────────


def test_approving_an_instance_correction_writes_an_override_row_scoped_to_that_date(
        table, recurring):
    correction_id = db.create_correction(
        "recurring_instance", "weekly-thing", {"description": "moved this week"},
        "why", "reporter@example.com", target_date="2026-06-08")

    db.approve_correction(correction_id, "admin@example.com")

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")
    assert override["description"] == "moved this week"
    assert override["_edited_by"] == "admin@example.com"
    assert "Public correction from reporter@example.com" in override["_comment"]
    # The series definition itself is untouched by an instance-level approval.
    assert "description" not in recurring["weekly-thing"]


def test_a_second_instance_correction_for_the_same_date_merges_onto_the_first(
        table, recurring):
    first = db.create_correction(
        "recurring_instance", "weekly-thing", {"description": "first fix"},
        "why", "a@b.c", target_date="2026-06-08")
    db.approve_correction(first, "admin@example.com")

    second = db.create_correction(
        "recurring_instance", "weekly-thing", {"location": "New Place"},
        "why", "a@b.c", target_date="2026-06-08")
    db.approve_correction(second, "admin@example.com")

    override = db.get_recurring_instance_override("weekly-thing", "2026-06-08")
    assert override["description"] == "first fix"
    assert override["location"] == "New Place"


def test_approving_an_instance_correction_against_a_deleted_series_raises(
        table, recurring):
    correction_id = db.create_correction(
        "recurring_instance", "weekly-thing", {"description": "x"}, "why", "a@b.c",
        target_date="2026-06-08")
    del recurring["weekly-thing"]

    with pytest.raises(ValueError, match="no longer exists"):
        db.approve_correction(correction_id, "admin@example.com")

    assert db.get_correction(correction_id)["status"] == "pending"


# ── reject_correction ─────────────────────────────────────────────────


def test_rejecting_marks_status_with_no_overlay_write(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "why", "a@b.c")

    db.reject_correction(correction_id, "admin@example.com", "not a real issue")

    correction = db.get_correction(correction_id)
    assert correction["status"] == "rejected"
    assert correction["rejection_reason"] == "not a real issue"
    assert overlay_of(events, "manual1") == {}


def test_rejecting_a_recurring_series_correction_leaves_the_record_untouched(
        table, recurring):
    correction_id = db.create_correction(
        "recurring_series", "weekly-thing", {"location": "New Place"}, "why", "a@b.c")

    db.reject_correction(correction_id, "admin@example.com")

    assert recurring["weekly-thing"]["location"] == "Old Place"


def test_rejecting_an_unknown_correction_raises(table):
    with pytest.raises(ValueError, match="No such correction"):
        db.reject_correction("ghost123", "admin@example.com")


def test_rejecting_an_already_resolved_correction_raises(table, events):
    correction_id = db.create_correction(
        "event", "manual1", {"time": "20:00"}, "why", "a@b.c")
    db.reject_correction(correction_id, "admin@example.com")

    with pytest.raises(ValueError, match="already rejected"):
        db.reject_correction(correction_id, "admin@example.com")


# ── The DRAFT#/CORRECTION# GSI collision, both directions ────────────
# Found live: a correction created during testing showed up in the DRAFT#
# queue as an "Untitled" row with no details, and rejecting it 404'd ("Draft
# not found") because get_draft looks it up at PK=DRAFT#{id}, which doesn't
# exist for a correction stored at PK=CORRECTION#{id}.


def test_get_drafts_by_status_does_not_pick_up_a_comingled_correction(table, events):
    db.create_correction("event", "manual1", {"time": "20:00"}, "wrong time", "a@b.c")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending", "title": "Real Draft",
    })

    pending = db.get_drafts_by_status("pending")

    assert [d["id"] for d in pending] == ["abc12345"]


def test_get_drafts_by_status_does_not_pick_up_a_recurring_correction(table, recurring):
    # The collision guard must hold regardless of which target_type the
    # co-mingled correction carries.
    db.create_correction(
        "recurring_series", "weekly-thing", {"location": "x"}, "why", "a@b.c")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending", "title": "Real Draft",
    })

    pending = db.get_drafts_by_status("pending")

    assert [d["id"] for d in pending] == ["abc12345"]


def test_get_drafts_by_submitter_does_not_pick_up_a_comingled_correction(table, events):
    db.create_correction("event", "manual1", {"time": "20:00"}, "wrong time", "a@b.c")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI3PK": "USER#a@b.c", "GSI3SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending", "title": "Real Draft",
    })

    mine = db.get_drafts_by_submitter("a@b.c")

    assert [d["id"] for d in mine] == ["abc12345"]


def test_get_corrections_by_status_still_excludes_drafts_with_recurring_targets_present(
        table, events, recurring):
    # Regression guard: adding target_type/target_id must not have broken the
    # existing PK-prefix filter, which is unrelated to those new attributes.
    db.create_correction(
        "recurring_instance", "weekly-thing", {"location": "x"}, "why", "a@b.c",
        target_date="2026-06-08")
    table.put_item(Item={
        "PK": "DRAFT#abc12345", "SK": "META",
        "GSI1PK": "STATUS#pending", "GSI1SK": "2026-01-01T00:00:00Z",
        "draft_type": "event", "status": "pending",
    })

    pending = db.get_corrections_by_status("pending")

    assert [c["id"] for c in pending] == \
        [c["id"] for c in pending if c["target_type"] == "recurring_instance"]
    assert len(pending) == 1
