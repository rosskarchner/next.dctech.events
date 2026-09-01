"""Tests for the correction routes — public submission and admin moderation.

The write rules themselves are tested in test_corrections.py; what is tested
here is the HTTP shape: that a stranger can submit without a Cognito account
(magic link only), that an iCal time correction is refused before it ever
reaches the database, that every admin route checks for the admins group,
and that the router dispatches /approve, /reject and the bare id correctly —
the same trap the drafts routes navigate.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_correction_routes.py
"""
import json
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402
import handler  # noqa: E402
import magic_link  # noqa: E402

ADMIN = {"sub": "u-1", "email": "admin@example.com",
         "cognito:groups": ["admins"]}
PLAIN = {"sub": "u-2", "email": "nobody@example.com", "cognito:groups": []}


def request(path, method="GET", body=None, claims=None, query=None):
    return {
        "path": path,
        "httpMethod": method,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
        "requestContext": {"authorizer": {"claims": dict(claims)}} if claims else {},
        # routes.submit._parse_body (unlike routes.admin._post_payload) only
        # tries JSON when this header says so — the public correction/read
        # routes go through it via _resolve_submitter/_parse_body.
        "headers": {"Content-Type": "application/json"} if body is not None else {},
    }


def call(path, method="GET", **kw):
    response = handler.lambda_handler(request(path, method, **kw), None)
    try:
        payload = json.loads(response["body"]) if response.get("body") else {}
    except (ValueError, TypeError):
        payload = {"error": response.get("body")}
    return response["statusCode"], payload


def magic_link_fields(email="reporter@example.com"):
    """A submission body carrying a magic-link identity. verify_token is
    monkeypatched by the `verified_link` fixture, so the timestamp/signature
    values themselves are never actually checked."""
    return {"mlt_email": email, "mlt_ts": "1700000000", "mlt_sig": "fake"}


class FakeTable:
    """Same shape as test_corrections.py's — CORRECTION# storage only."""

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
def store(monkeypatch):
    events = {
        "ical1": {"guid": "ical1", "title": "iCal Event", "date": "2026-09-10",
                  "time": "18:00", "source": "ical", "group": "Rust DC",
                  "location": "Arlington, VA", "url": "https://example.com/e",
                  "description": "desc"},
        "man1": {"guid": "man1", "title": "Manual Event", "date": "2026-09-11",
                 "time": "12:00", "source": "manual", "group": ""},
    }

    def _update(guid, data, overrides=None, *, expect_overrides_rev=db._UNSET):
        events[guid]["overrides"] = overrides

    monkeypatch.setattr(db, "get_event_from_config",
                        lambda g: dict(events[g]) if g in events else None)
    monkeypatch.setattr(db, "update_event", _update)
    # One shared FakeTable instance per test, not a fresh one per
    # _get_table() call — CORRECTION# writes must persist across calls.
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    return events


@pytest.fixture
def verified_link(monkeypatch):
    """Bypass real KMS verification — these tests cover routing, not crypto
    (that's magic_link.py's own test_magic_link.py)."""
    monkeypatch.setattr(magic_link, "verify_token", lambda *a: (True, None))


# ── Public submission ───────────────────────────────────────────────


def test_a_stranger_can_submit_with_only_a_magic_link(store, verified_link):
    status, payload = call(
        "/api/corrections", "POST",
        body={"guid": "man1", "fields": {"time": "20:00"},
              "reason": "organizer moved it", **magic_link_fields()})
    assert status == 201
    assert "correction_id" in payload


def test_submitting_with_no_identity_at_all_is_refused(store):
    status, _ = call(
        "/api/corrections", "POST",
        body={"guid": "man1", "fields": {"time": "20:00"}, "reason": "why"})
    assert status == 401


def test_a_time_correction_against_an_ical_event_is_refused(store, verified_link):
    status, payload = call(
        "/api/corrections", "POST",
        body={"guid": "ical1", "fields": {"time": "20:00"}, "reason": "why",
              **magic_link_fields()})
    assert status == 422
    assert "Not correctable" in payload["error"]


def test_a_description_correction_against_an_ical_event_is_accepted(store, verified_link):
    status, _ = call(
        "/api/corrections", "POST",
        body={"guid": "ical1", "fields": {"description": "corrected"},
              "reason": "typo", **magic_link_fields()})
    assert status == 201


def test_an_unknown_guid_is_a_404(store, verified_link):
    status, payload = call(
        "/api/corrections", "POST",
        body={"guid": "ghost", "fields": {"description": "x"}, "reason": "why",
              **magic_link_fields()})
    assert status == 404


def test_a_blank_reason_is_refused(store, verified_link):
    status, payload = call(
        "/api/corrections", "POST",
        body={"guid": "man1", "fields": {"time": "20:00"}, "reason": "  ",
              **magic_link_fields()})
    assert status == 400
    assert "reason" in payload["error"].lower() or "wrong" in payload["error"].lower()


def test_empty_fields_are_refused(store, verified_link):
    status, _ = call(
        "/api/corrections", "POST",
        body={"guid": "man1", "fields": {}, "reason": "why", **magic_link_fields()})
    assert status == 400


# ── Public read (for the form) ──────────────────────────────────────


def test_the_public_read_needs_no_auth_at_all(store):
    status, payload = call("/api/public/events/man1")
    assert status == 200
    assert payload["title"] == "Manual Event"
    assert set(payload["correctable_fields"]) == {
        "description", "location", "url", "time", "end_time"}


def test_the_public_read_narrows_correctable_fields_for_ical(store):
    payload = call("/api/public/events/ical1")[1]
    assert set(payload["correctable_fields"]) == {"description", "location", "url"}


def test_the_public_read_omits_admin_only_bookkeeping(store):
    payload = call("/api/public/events/man1")[1]
    assert "overrides" not in payload
    assert "group" not in payload


def test_the_public_read_404s_for_an_unknown_guid(store):
    status, _ = call("/api/public/events/ghost")
    assert status == 404


# ── Admin auth ───────────────────────────────────────────────────────


def test_every_correction_route_requires_admin(store):
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    for path, method, body in [
        ("/api/admin/corrections", "GET", None),
        (f"/api/admin/corrections/{correction_id}", "GET", None),
        (f"/api/admin/corrections/{correction_id}/approve", "POST", None),
        (f"/api/admin/corrections/{correction_id}/reject", "POST", None),
    ]:
        status, _ = call(path, method, body=body, claims=PLAIN)
        assert status == 403, f"{method} {path} did not require admin"


# ── Admin moderation ─────────────────────────────────────────────────


def test_the_pending_queue_lists_a_new_correction(store):
    db.create_correction("man1", {"time": "20:00"}, "why", "a@b.c")
    status, payload = call("/api/admin/corrections", claims=ADMIN,
                           query={"status": "pending"})
    assert status == 200
    assert len(payload["corrections"]) == 1


def test_the_detail_view_enriches_with_the_live_target_event(store):
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    status, payload = call(f"/api/admin/corrections/{correction_id}", claims=ADMIN)
    assert status == 200
    assert payload["correction"]["target_event"]["title"] == "Manual Event"


def test_approving_applies_the_overlay_and_returns_200(store):
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    status, payload = call(
        f"/api/admin/corrections/{correction_id}/approve", "POST", claims=ADMIN)
    assert status == 200
    assert db.get_correction(correction_id)["status"] == "approved"


def test_approving_an_unknown_correction_is_a_404(store):
    status, _ = call("/api/admin/corrections/ghost123/approve", "POST", claims=ADMIN)
    assert status == 404


def test_approving_an_already_approved_correction_is_a_409(store):
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    call(f"/api/admin/corrections/{correction_id}/approve", "POST", claims=ADMIN)
    status, _ = call(
        f"/api/admin/corrections/{correction_id}/approve", "POST", claims=ADMIN)
    assert status == 409


def test_rejecting_with_a_reason(store):
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    status, payload = call(
        f"/api/admin/corrections/{correction_id}/reject", "POST", claims=ADMIN,
        body={"reason": "not a real problem"})
    assert status == 200
    assert db.get_correction(correction_id)["status"] == "rejected"
    assert db.get_correction(correction_id)["rejection_reason"] == \
        "not a real problem"


def test_the_router_does_not_confuse_approve_with_a_correction_id(store):
    # Same trap the drafts routes navigate with /approve and /reject.
    correction_id = db.create_correction(
        "man1", {"time": "20:00"}, "why", "a@b.c")
    status, _ = call(f"/api/admin/corrections/{correction_id}", claims=ADMIN)
    assert status == 200
    status, _ = call(
        f"/api/admin/corrections/{correction_id}/approve", "POST", claims=ADMIN)
    assert status == 200
