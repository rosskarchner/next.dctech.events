"""Tests for the admin event routes — the HTTP half of the human QA surface.

The write rules themselves are tested in test_overlay.py; what is tested here
is the HTTP shape around them: that a moderator's identity reaches the overlay,
that a stale form gets a 409 instead of clobbering, that bad values come back as
422 rather than 500, and that the router does not mistake /bulk for a guid.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_event_routes.py
"""
import json
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402
import handler  # noqa: E402
from routes import events as events_routes  # noqa: E402

ADMIN = {"sub": "u-1", "email": "ross@example.com",
         "cognito:groups": ["admins"]}
CATEGORIES = {"ai": {"name": "AI"}, "cloud": {"name": "Cloud"}}


def request(path, method="GET", body=None, claims=ADMIN, query=None):
    return {
        "path": path,
        "httpMethod": method,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
        "requestContext": {"authorizer": {"claims": dict(claims)}} if claims else {},
        "headers": {},
    }


def call(path, method="GET", **kw):
    """Through the real router, so dispatch is covered too.

    Auth failures come back as plain text ('Unauthorized', 'Admin access
    required'), not JSON, so the body is decoded defensively.
    """
    response = handler.lambda_handler(request(path, method, **kw), None)
    try:
        payload = json.loads(response["body"]) if response.get("body") else {}
    except (ValueError, TypeError):
        payload = {"error": response.get("body")}
    return response["statusCode"], payload


@pytest.fixture
def store(monkeypatch):
    events = {
        "ical1": {"guid": "ical1", "title": "Feed Title", "date": "2026-09-10",
                  "time": "18:00", "source": "ical", "group": "Rust DC",
                  "group_id": "rust-dc", "categories": [],
                  "location": "Arlington, VA, Arlington, VA",
                  "review_status": "pending_qa",
                  "description": "A long description."},
        "ical2": {"guid": "ical2", "title": "Feed Title Repost",
                  "date": "2026-09-10", "time": "18:00", "source": "ical",
                  "group": "Aggregator", "group_id": "agg", "categories": [],
                  "review_status": "pending_qa"},
        "man1": {"guid": "man1", "title": "Manual Event", "date": "2026-09-11",
                 "time": "12:00", "source": "manual", "group": "",
                 "categories": ["ai"], "review_status": "approved"},
    }

    def _update(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        events[guid]["overrides"] = overrides

    def _set_status(guid, status):
        if guid not in events:
            return None
        events[guid]["review_status"] = status
        return guid

    monkeypatch.setattr(db, "get_event_from_config",
                        lambda g: dict(events[g]) if g in events else None)
    monkeypatch.setattr(db, "get_all_events",
                        lambda date_prefix=None, filter_type=None, include_past=False:
                        [dict(e) for e in events.values()])
    monkeypatch.setattr(db, "get_events_by_review_status",
                        lambda status, limit=None: [dict(e) for e in events.values()
                                                    if e.get("review_status") == status])
    monkeypatch.setattr(db, "update_event", _update)
    monkeypatch.setattr(db, "set_event_review_status", _set_status)
    monkeypatch.setattr(db, "get_all_categories", lambda: dict(CATEGORIES))
    return events


# ── Auth ───────────────────────────────────────────────────────────


def test_a_non_admin_is_refused(store):
    status, _ = call("/api/admin/events",
                     claims={"sub": "u-2", "email": "nobody@example.com",
                             "cognito:groups": []})
    assert status == 403


def test_every_event_route_checks_admin(store):
    """A route that forgets _admin_check is the whole bug class here."""
    plain = {"sub": "u-2", "email": "nobody@example.com",
             "cognito:groups": []}
    for path, method, body in [
        ("/api/admin/events", "GET", None),
        ("/api/admin/events/ical1", "GET", None),
        ("/api/admin/events/ical1/overlay", "PUT", {"fields": {"title": "x"},
                                                    "comment": "c"}),
        ("/api/admin/events/ical1/overlay", "DELETE", None),
        ("/api/admin/events/ical1/review-status", "PUT",
         {"review_status": "approved"}),
        ("/api/admin/events/bulk", "POST", {"action": "hide",
                                            "guids": ["ical1"]}),
        ("/api/admin/qa-runs/r1", "GET", None),
        ("/api/admin/qa-runs/r1/revert", "POST", None),
    ]:
        status, _ = call(path, method, body=body, claims=plain)
        assert status == 403, f"{method} {path} did not require admin"


# ── Listing ────────────────────────────────────────────────────────


def test_the_list_omits_descriptions(store):
    # Kilobytes per event, hundreds of events, never edited in this workflow.
    status, payload = call("/api/admin/events")
    assert status == 200
    row = payload["events"][0]
    assert "description" not in row
    assert row["has_description"] in (True, False)


def test_the_list_carries_everything_the_badges_need(store):
    db.set_event_overlay("ical1", {"hidden": True}, "out of area", run_id="r1")
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "ical1")
    assert row["overlay"] == {"hidden": True}
    assert row["overlay_meta"]["run_id"] == "r1"
    assert row["effective"]["hidden"] is True
    assert row["review_status"] == "pending_qa"


def test_bookkeeping_is_split_out_not_passed_through_raw(store):
    # So the browser cannot round-trip a forged _qa_run into a write.
    db.set_event_overlay("ical1", {"title": "x"}, "why", run_id="r1")
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "ical1")
    assert row["overlay"] == {"title": "x"}
    assert "_qa_run" not in row["overlay"]
    assert "overrides" not in row


def test_the_queue_filter_uses_the_review_status_index(store):
    status, payload = call("/api/admin/events",
                           query={"review_status": "pending_qa"})
    assert payload["mode"] == "review_status"
    assert {r["guid"] for r in payload["events"]} == {"ical1", "ical2"}


def test_an_unknown_review_status_is_a_400(store):
    status, payload = call("/api/admin/events",
                           query={"review_status": "nonsense"})
    assert status == 400
    assert "valid" in payload


def test_filtering_by_source(store):
    payload = call("/api/admin/events", query={"source": "manual"})[1]
    assert [r["guid"] for r in payload["events"]] == ["man1"]


def test_searching_matches_the_effective_title(store):
    # Not the record's: a corrected title is what a moderator sees and types.
    db.set_event_overlay("ical1", {"title": "Mid-month Rustful"}, "corrected")
    payload = call("/api/admin/events", query={"q": "rustful"})[1]
    assert [r["guid"] for r in payload["events"]] == ["ical1"]


def test_the_uncategorized_filter(store):
    payload = call("/api/admin/events", query={"state": "uncategorized"})[1]
    assert {r["guid"] for r in payload["events"]} == {"ical1", "ical2"}


def test_the_overlaid_filter_finds_hand_edits(store):
    db.set_event_overlay("man1", {"title": "x"}, "why", actor="a@b.c")
    payload = call("/api/admin/events", query={"state": "overlaid"})[1]
    assert [r["guid"] for r in payload["events"]] == ["man1"]


def test_the_agent_filter_finds_agent_edits(store):
    db.set_event_overlay("man1", {"title": "x"}, "why", actor="a@b.c")
    db.set_event_overlay("ical1", {"hidden": True}, "qc", run_id="r1")
    payload = call("/api/admin/events", query={"state": "agent"})[1]
    assert [r["guid"] for r in payload["events"]] == ["ical1"]


def test_the_agent_filter_does_not_catch_a_hand_edit(store):
    # A hand edit writes a _comment too, so the filter cannot key off that.
    db.set_event_overlay("man1", {"title": "x"}, "my reason", actor="a@b.c")
    payload = call("/api/admin/events", query={"state": "agent"})[1]
    assert payload["events"] == []


def test_a_legacy_agent_overlay_with_no_recorded_author_still_matches(store):
    # Written before authorship was recorded; the run stamp is all there is.
    store["ical1"]["overrides"] = {"hidden": True, "_rev": 1,
                                   "_qa_run": {"run_id": "old", "prior": {},
                                               "added": ["hidden"]}}
    payload = call("/api/admin/events", query={"state": "agent"})[1]
    assert [r["guid"] for r in payload["events"]] == ["ical1"]


def test_the_editable_field_list_travels_with_the_response(store):
    # The form is generated from it, so the UI cannot offer a field the write
    # path rejects.
    payload = call("/api/admin/events")[1]
    assert payload["editable_fields"] == list(db.OVERLAY_EDITABLE_FIELDS)


# ── Duplicate states — the three ways a merge silently fails ───────


def test_a_healthy_merge_reads_as_ok(store):
    db.set_event_overlay("ical2", {"duplicate_of": "ical1"}, "repost")
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "ical2")
    assert row["duplicate_state"] == "ok"


def test_a_dangling_merge_is_surfaced(store):
    # calgen ignores it, so the duplicate is still on the calendar.
    store["ical2"]["overrides"] = {"duplicate_of": "ghost", "_rev": 1}
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "ical2")
    assert row["duplicate_state"] == "dangling"


def test_merging_into_a_hidden_event_is_surfaced(store):
    # Child dropped as a duplicate, parent dropped as hidden: both listings go.
    db.set_event_overlay("ical1", {"hidden": True}, "out of area")
    store["ical2"]["overrides"] = {"duplicate_of": "ical1", "_rev": 1}
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "ical2")
    assert row["duplicate_state"] == "into-hidden"


def test_a_chained_merge_is_surfaced(store):
    db.set_event_overlay("ical2", {"duplicate_of": "ical1"}, "repost")
    store["man1"]["overrides"] = {"duplicate_of": "ical2", "_rev": 1}
    row = next(r for r in call("/api/admin/events")[1]["events"]
               if r["guid"] == "man1")
    assert row["duplicate_state"] == "chain"


def test_the_problems_filter_collects_all_three(store):
    store["ical2"]["overrides"] = {"duplicate_of": "ghost", "_rev": 1}
    payload = call("/api/admin/events", query={"state": "problems"})[1]
    assert [r["guid"] for r in payload["events"]] == ["ical2"]


def test_duplicate_state_looks_at_the_whole_corpus_not_the_filtered_rows(store):
    # The canonical event may be filtered out of the response; that must not
    # make a healthy merge look dangling.
    db.set_event_overlay("ical2", {"duplicate_of": "man1"}, "repost")
    payload = call("/api/admin/events", query={"source": "ical"})[1]
    row = next(r for r in payload["events"] if r["guid"] == "ical2")
    assert row["duplicate_state"] == "ok"


# ── Detail ─────────────────────────────────────────────────────────


def test_the_detail_route_includes_the_description(store):
    status, payload = call("/api/admin/events/ical1")
    assert status == 200
    assert payload["event"]["description"] == "A long description."


def test_the_detail_route_resolves_the_canonical_event(store):
    db.set_event_overlay("ical2", {"duplicate_of": "ical1"}, "repost")
    payload = call("/api/admin/events/ical2")[1]
    assert payload["event"]["duplicate_of_event"]["title"] == "Feed Title"


def test_an_unknown_guid_is_a_404(store):
    status, _ = call("/api/admin/events/ghost")
    assert status == 404


# ── Writing an overlay ─────────────────────────────────────────────


def test_an_edit_is_attributed_to_the_signed_in_moderator(store):
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"title": "Corrected"},
                                 "comment": "kept from feed; trimmed filler"})
    assert status == 200
    assert payload["edited_by"] == "ross@example.com"
    assert store["ical1"]["overrides"]["title"] == "Corrected"


def test_a_comment_is_required(store):
    # An unexplained overlay is the thing a reviewer cannot act on.
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"title": "x"}})
    assert status == 400
    assert "comment" in payload["error"]


def test_an_empty_request_is_refused(store):
    status, _ = call("/api/admin/events/ical1/overlay", "PUT",
                     body={"fields": {}, "comment": "c"})
    assert status == 400


def test_a_protected_field_is_a_400(store):
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"group": "Someone Else"},
                                 "comment": "c"})
    assert status == 400
    assert "protected" in payload["error"]


def test_an_invented_category_is_a_422_not_a_500(store):
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"categories": ["nonsense"]},
                                 "comment": "c"})
    assert status == 422
    assert "Unknown category slug" in payload["error"]


def test_a_dangling_merge_target_is_a_422(store):
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"duplicate_of": "ghost"},
                                 "comment": "c"})
    assert status == 422


def test_writing_to_an_unknown_event_is_a_404(store):
    status, _ = call("/api/admin/events/ghost/overlay", "PUT",
                     body={"fields": {"hidden": True}, "comment": "c"})
    assert status == 404


def test_clearing_a_field_falls_back_to_the_source(store):
    db.set_event_overlay("ical1", {"title": "Overridden"}, "why")
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"clear": ["title"], "comment": "reverted"})
    assert status == 200
    assert payload["effective"]["title"] == "Feed Title"


# ── Optimistic concurrency over HTTP ───────────────────────────────


def test_a_write_with_no_expected_rev_merges(store):
    # Bulk and agent paths: a field-level merge is the intended outcome.
    db.set_event_overlay("ical1", {"title": "agent"}, "qc", run_id="r1")
    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"location": "Arlington, VA"},
                                 "comment": "read from the page"})
    assert status == 200
    assert payload["overlay"] == {"title": "agent",
                                  "location": "Arlington, VA"}


def test_a_stale_form_gets_a_409_with_the_current_overlay(store):
    db.set_event_overlay("ical1", {"title": "theirs"}, "first")   # rev 1
    db.set_event_overlay("ical1", {"location": "x"}, "second")    # rev 2

    status, payload = call("/api/admin/events/ical1/overlay", "PUT",
                           body={"fields": {"title": "mine"},
                                 "comment": "c", "expected_rev": 1})

    assert status == 409
    assert payload["current"]["rev"] == 2
    assert payload["current"]["overlay"]["title"] == "theirs"
    assert store["ical1"]["overrides"]["title"] == "theirs"


def test_an_explicit_null_expected_rev_means_expect_no_overlay(store):
    status, _ = call("/api/admin/events/ical1/overlay", "PUT",
                     body={"fields": {"title": "x"}, "comment": "c",
                           "expected_rev": None})
    assert status == 200
    # Now there is one, so the same request must conflict.
    status, _ = call("/api/admin/events/ical1/overlay", "PUT",
                     body={"fields": {"title": "y"}, "comment": "c",
                           "expected_rev": None})
    assert status == 409


# ── Clearing an overlay ────────────────────────────────────────────


def test_delete_clears_every_rendered_field(store):
    db.set_event_overlay("ical1", {"title": "x", "hidden": True}, "why")
    status, payload = call("/api/admin/events/ical1/overlay", "DELETE")
    assert status == 200
    assert payload["overlay"] == {}


def test_delete_keeps_who_cleared_it(store):
    db.set_event_overlay("ical1", {"title": "x"}, "why")
    call("/api/admin/events/ical1/overlay", "DELETE")
    book = db.overlay_bookkeeping(store["ical1"]["overrides"])
    assert book["edited_by"] == "ross@example.com"


def test_delete_honours_expected_rev_from_the_query_string(store):
    db.set_event_overlay("ical1", {"title": "x"}, "why")   # rev 1
    status, _ = call("/api/admin/events/ical1/overlay", "DELETE",
                     query={"expected_rev": "99"})
    assert status == 409


# ── Review status ──────────────────────────────────────────────────


def test_approving_clears_the_event_from_the_queue(store):
    status, _ = call("/api/admin/events/ical1/review-status", "PUT",
                     body={"review_status": "approved"})
    assert status == 200
    assert store["ical1"]["review_status"] == "approved"


def test_an_arbitrary_review_status_is_refused(store):
    status, _ = call("/api/admin/events/ical1/review-status", "PUT",
                     body={"review_status": "whatever"})
    assert status == 400


def test_review_status_on_an_unknown_event_is_a_404(store):
    status, _ = call("/api/admin/events/ghost/review-status", "PUT",
                     body={"review_status": "approved"})
    assert status == 404


# ── Bulk ───────────────────────────────────────────────────────────


def test_bulk_is_not_parsed_as_a_guid(store):
    """The router hazard: /bulk must match before the {guid} block."""
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "hide", "guids": ["ical1"]})
    assert status == 200
    assert payload["action"] == "hide"


def test_bulk_hide_writes_overlays(store):
    call("/api/admin/events/bulk", "POST",
         body={"action": "hide", "guids": ["ical1", "ical2"],
               "comment": "out of area"})
    assert store["ical1"]["overrides"]["hidden"] is True
    assert store["ical2"]["overrides"]["hidden"] is True


def test_bulk_reports_partial_success_rather_than_failing(store):
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "hide",
                                 "guids": ["ical1", "ghost"]})
    assert status == 200
    assert payload["updated"] == 1
    assert payload["failed"][0]["guid"] == "ghost"


def test_bulk_combine_requires_an_explicit_canonical(store):
    # Never inferred from selection order: which listing readers keep is the
    # entire decision.
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "combine",
                                 "guids": ["ical1", "ical2"]})
    assert status == 400
    assert "canonical_guid" in payload["error"]


def test_bulk_combine_merges_into_the_named_event(store):
    status, _ = call("/api/admin/events/bulk", "POST",
                     body={"action": "combine", "guids": ["ical2"],
                           "canonical_guid": "ical1"})
    assert status == 200
    assert store["ical2"]["overrides"]["duplicate_of"] == "ical1"


def test_bulk_combine_refuses_a_hidden_canonical(store):
    # Both listings would disappear.
    db.set_event_overlay("ical1", {"hidden": True}, "out of area")
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "combine", "guids": ["ical2"],
                                 "canonical_guid": "ical1"})
    assert status == 422
    assert "hidden" in payload["error"]


def test_bulk_combine_refuses_a_canonical_that_is_itself_merged(store):
    db.set_event_overlay("ical1", {"duplicate_of": "man1"}, "repost")
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "combine", "guids": ["ical2"],
                                 "canonical_guid": "ical1"})
    assert status == 422


def test_bulk_rejects_an_unknown_action(store):
    status, _ = call("/api/admin/events/bulk", "POST",
                     body={"action": "obliterate", "guids": ["ical1"]})
    assert status == 400


def test_bulk_rejects_an_empty_selection(store):
    status, _ = call("/api/admin/events/bulk", "POST",
                     body={"action": "hide", "guids": []})
    assert status == 400


def test_bulk_is_capped(store):
    status, payload = call("/api/admin/events/bulk", "POST",
                           body={"action": "hide",
                                 "guids": [f"g{n}" for n in range(500)]})
    assert status == 400
    assert "at a time" in payload["error"]


def test_bulk_set_review_status(store):
    call("/api/admin/events/bulk", "POST",
         body={"action": "set_review_status", "guids": ["ical1", "ical2"],
               "review_status": "approved"})
    assert store["ical1"]["review_status"] == "approved"
    assert store["ical2"]["review_status"] == "approved"


def test_bulk_unhide_is_available_as_an_undo(store):
    call("/api/admin/events/bulk", "POST",
         body={"action": "hide", "guids": ["ical1"]})
    call("/api/admin/events/bulk", "POST",
         body={"action": "unhide", "guids": ["ical1"]})
    assert db.public_overlay(store["ical1"]["overrides"]) == {}


# ── QA runs ────────────────────────────────────────────────────────


def test_a_run_can_be_reviewed_over_http(store):
    db.set_event_overlay("ical1", {"hidden": True}, "out of area", run_id="r1")
    status, payload = call("/api/admin/qa-runs/r1")
    assert status == 200
    assert payload["count"] == 1
    assert payload["changes"][0]["applied"] == {"hidden": True}


def test_a_run_can_be_reverted_over_http(store):
    # What makes the digest's revert_qa_run("qc-…") clickable.
    db.set_event_overlay("ical1", {"hidden": True}, "out of area", run_id="r1")
    status, payload = call("/api/admin/qa-runs/r1/revert", "POST")
    assert status == 200
    assert payload["reverted"] == 1
    assert db.public_overlay(store["ical1"]["overrides"]) == {}


def test_reverting_records_who_did_it(store):
    db.set_event_overlay("ical1", {"hidden": True}, "qc", run_id="r1")
    call("/api/admin/qa-runs/r1/revert", "POST")
    book = db.overlay_bookkeeping(store["ical1"]["overrides"])
    assert book["edited_by"] == "ross@example.com"


# ── Dead routes are gone ───────────────────────────────────────────


def test_the_broken_admin_dashboard_route_is_gone(store):
    # It rendered admin/dashboard.html, which does not exist in this fork.
    assert not hasattr(events_routes, "dashboard")
    response = handler.lambda_handler(request("/admin", "GET"), None)
    assert response["statusCode"] == 404
