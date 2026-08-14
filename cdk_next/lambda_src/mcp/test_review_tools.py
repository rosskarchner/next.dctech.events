"""Tests for the MCP submission-review tools.

Run: python -m pytest test_review_tools.py
"""
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import db  # noqa: E402
import server  # noqa: E402

# This SDK's @mcp.tool() registers the function and returns it unchanged, so
# the module attributes are directly callable.
approve = server.approve_submission
reject = server.reject_submission
get_sub = server.get_submission
list_pending = server.list_pending_submissions
submit = server.submit_event
trust = server.trust_submitter
untrust = server.untrust_submitter


PENDING = {"id": "d1", "draft_type": "event", "status": "pending",
           "title": "A Talk", "submitter_email": "Sub@Example.com",
           "submitter_id": "magiclink:sub@example.com"}


@pytest.fixture
def fake(monkeypatch):
    state = {"status": [], "trusted": set(), "promoted": [], "untrusted": [],
             "created": []}

    monkeypatch.setattr(server.db, "get_draft",
                        lambda did: dict(PENDING) if did == "d1" else None)
    monkeypatch.setattr(server.db, "promote_draft",
                        lambda did, dtype, merged: state["promoted"].append(
                            (did, dtype, merged)) or "published-id")
    monkeypatch.setattr(server.db, "update_draft_status",
                        lambda did, st, rev=None: state["status"].append((did, st, rev)))
    monkeypatch.setattr(server.db, "trust_submitter",
                        lambda email, trusted_by=None, note=None: state["trusted"].add(email))
    monkeypatch.setattr(server.db, "untrust_submitter",
                        lambda email: state["untrusted"].append(email))
    monkeypatch.setattr(server.db, "is_trusted_submitter",
                        lambda email: str(email).lower() in state["trusted"])
    monkeypatch.setattr(server.db, "get_drafts_by_submitter", lambda uid: [])
    monkeypatch.setattr(server.db, "get_all_categories",
                        lambda: {"ai": {}, "data": {}})
    monkeypatch.setattr(server.db, "create_draft",
                        lambda dtype, data, email, sub_id=None: state["created"].append(
                            (dtype, data, email, sub_id)) or "new-draft")
    return state


# ── Submit ─────────────────────────────────────────────────────────

def test_submit_creates_a_pending_event_draft(fake):
    result = submit(title="A Talk", date="2026-08-20",
                    url="https://luma.com/x", submitter_email="sub@example.com",
                    time="18:00", location="DC", categories=["ai"])

    assert result == {"draft_id": "new-draft", "status": "pending",
                      "title": "A Talk", "date": "2026-08-20",
                      "submitter_email": "sub@example.com"}
    dtype, data, email, _ = fake["created"][0]
    assert dtype == "event"
    assert data["time"] == "18:00"
    assert data["all_day"] is False
    assert data["categories"] == ["ai"]


def test_submit_never_publishes_even_for_a_trusted_submitter(fake):
    # The address is who the event is credited to, not consent to list it,
    # so trust must not short-circuit review the way /api/submissions does.
    fake["trusted"].add("sub@example.com")
    assert submit(title="A Talk", date="2026-08-20", url="https://luma.com/x",
                  submitter_email="sub@example.com")["status"] == "pending"
    assert fake["promoted"] == []


def test_submit_marks_all_day_events(fake):
    submit(title="A Conference", date="2026-08-20", url="https://x.test",
           submitter_email="sub@example.com", all_day=True)
    data = fake["created"][0][1]
    assert data["all_day"] is True
    assert data["time"] is None


def test_submit_rejects_unknown_category_slugs(fake):
    with pytest.raises(ValueError, match="Unknown category"):
        submit(title="A Talk", date="2026-08-20", url="https://x.test",
               submitter_email="sub@example.com", categories=["not-a-category"])
    assert fake["created"] == []


def test_submit_requires_a_title_and_date(fake):
    with pytest.raises(ValueError, match="title and date are required"):
        submit(title="   ", date="2026-08-20", url="https://x.test",
               submitter_email="sub@example.com")
    assert fake["created"] == []


def test_submit_rejects_a_non_address(fake):
    with pytest.raises(ValueError, match="not a valid email"):
        submit(title="A Talk", date="2026-08-20", url="https://x.test",
               submitter_email="not-an-email")
    assert fake["created"] == []


def test_submit_normalizes_the_submitter_address(fake):
    result = submit(title="A Talk", date="2026-08-20", url="https://x.test",
                    submitter_email="  Sub@Example.COM ")
    assert result["submitter_email"] == "sub@example.com"
    assert fake["created"][0][2] == "sub@example.com"


# ── Approve ────────────────────────────────────────────────────────

def test_approve_publishes_and_marks_approved(fake):
    result = approve("d1")

    assert result["published_id"] == "published-id"
    assert fake["status"] == [("d1", "APPROVED", server.MCP_REVIEWER)]
    assert result["trusted"] is None


def test_approve_records_an_identifiable_reviewer(fake):
    # History should show an agent approved this, not a person.
    approve("d1")
    assert fake["status"][0][2] == "mcp:agent"


def test_approve_can_also_trust_the_submitter(fake):
    result = approve("d1", trust_submitter=True)
    assert result["trusted"] == "sub@example.com"
    assert "sub@example.com" in fake["trusted"]


def test_approve_without_the_flag_trusts_nobody(fake):
    approve("d1")
    assert fake["trusted"] == set()


def test_approve_overrides_categories_when_given(fake):
    approve("d1", categories=["ai"])
    assert fake["promoted"][0][2]["categories"] == ["ai"]


def test_approve_rejects_unknown_category_slugs(fake):
    with pytest.raises(ValueError, match="Unknown category"):
        approve("d1", categories=["not-a-real-category"])
    # Nothing should have been published on a validation failure.
    assert fake["promoted"] == []


def test_approve_unknown_draft_raises(fake):
    with pytest.raises(ValueError, match="No submission"):
        approve("nope")


def test_approve_is_not_repeatable(monkeypatch, fake):
    monkeypatch.setattr(server.db, "get_draft",
                        lambda did: {**PENDING, "status": "approved"})
    # Re-approving would publish a duplicate and overwrite the reviewer.
    with pytest.raises(ValueError, match="already"):
        approve("d1")
    assert fake["promoted"] == []


# ── Reject ─────────────────────────────────────────────────────────

def test_reject_marks_rejected_without_publishing(fake):
    result = reject("d1", reason="off topic")

    assert result["status"] == "rejected"
    assert result["reason"] == "off topic"
    assert fake["status"] == [("d1", "REJECTED", server.MCP_REVIEWER)]
    assert fake["promoted"] == []


def test_reject_unknown_draft_raises(fake):
    with pytest.raises(ValueError, match="No submission"):
        reject("nope")


def test_already_approved_submission_cannot_be_rejected(monkeypatch, fake):
    monkeypatch.setattr(server.db, "get_draft",
                        lambda did: {**PENDING, "status": "approved"})
    with pytest.raises(ValueError, match="already"):
        reject("d1")


# ── Read tools ─────────────────────────────────────────────────────

def test_get_submission_reports_trust_status(fake):
    fake["trusted"].add("sub@example.com")
    assert get_sub("d1")["submitter_trusted"] is True


def test_get_submission_excludes_the_draft_from_its_own_history(monkeypatch, fake):
    monkeypatch.setattr(server.db, "get_drafts_by_submitter", lambda uid: [
        {"id": "d1", "title": "A Talk", "status": "pending"},
        {"id": "d0", "title": "Older", "status": "approved"},
    ])
    history = get_sub("d1")["submitter_history"]
    assert [h["id"] for h in history] == ["d0"]


def test_list_pending_annotates_trust(monkeypatch, fake):
    monkeypatch.setattr(server.db, "get_drafts_by_status",
                        lambda status: [dict(PENDING)])
    fake["trusted"].add("sub@example.com")
    assert list_pending()[0]["submitter_trusted"] is True


# ── Trust tools ────────────────────────────────────────────────────

def test_trust_normalizes_the_address(fake):
    assert trust("  Person@Example.COM ")["trusted"] == "person@example.com"


def test_trust_rejects_a_non_address(fake):
    with pytest.raises(ValueError, match="not a valid email"):
        trust("not-an-email")


def test_untrust_requires_an_existing_entry(fake):
    with pytest.raises(ValueError, match="not currently trusted"):
        untrust("stranger@example.com")


def test_untrust_revokes(fake):
    fake["trusted"].add("sub@example.com")
    assert untrust("Sub@Example.com")["untrusted"] == "sub@example.com"
    assert fake["untrusted"] == ["sub@example.com"]
