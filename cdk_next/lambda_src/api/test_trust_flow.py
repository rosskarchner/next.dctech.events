"""Tests for the trust decision at approval time and the auto-approve gate.

Run: python -m pytest test_trust_flow.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("SUBMIT_KEY_ID", "test-key")

from routes import admin, submit  # noqa: E402


# ── "Trust this submitter" checkbox parsing ────────────────────────

@pytest.mark.parametrize("value", ["1", "true", "True", "on", "yes", "YES"])
def test_checkbox_values_that_mean_yes(value):
    assert admin._wants_trust({"trust_submitter": value})


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", None])
def test_checkbox_values_that_mean_no(value):
    assert not admin._wants_trust({"trust_submitter": value})


def test_absent_checkbox_means_no():
    # An unchecked HTML checkbox submits nothing at all.
    assert not admin._wants_trust({})


# ── _maybe_trust_submitter ─────────────────────────────────────────

@pytest.fixture
def recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin, "trust_submitter",
        lambda email, trusted_by=None, note=None: calls.append(
            {"email": email, "trusted_by": trusted_by, "note": note}))
    return calls


def test_no_trust_when_box_unchecked(recorder):
    result = admin._maybe_trust_submitter(
        {}, {"submitter_email": "a@example.com"}, {"email": "admin@x.com"})
    assert result is None
    assert recorder == []


def test_trusts_the_draft_submitter_not_the_approver(recorder):
    result = admin._maybe_trust_submitter(
        {"trust_submitter": "true"},
        {"submitter_email": "Submitter@Example.com"},
        {"email": "admin@example.com"})

    assert result == "submitter@example.com"
    assert recorder[0]["email"] == "submitter@example.com"
    assert recorder[0]["trusted_by"] == "admin@example.com"


def test_note_is_recorded_when_given(recorder):
    admin._maybe_trust_submitter(
        {"trust_submitter": "1", "trust_note": "  organizer  "},
        {"submitter_email": "a@example.com"}, {"email": "admin@x.com"})
    assert recorder[0]["note"] == "organizer"


def test_blank_note_is_stored_as_none(recorder):
    admin._maybe_trust_submitter(
        {"trust_submitter": "1", "trust_note": "   "},
        {"submitter_email": "a@example.com"}, {"email": "admin@x.com"})
    assert recorder[0]["note"] is None


def test_draft_without_submitter_email_is_skipped(recorder):
    result = admin._maybe_trust_submitter(
        {"trust_submitter": "1"}, {}, {"email": "admin@x.com"})
    assert result is None
    assert recorder == []


def test_trust_failure_does_not_break_approval(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("dynamo is having a day")
    monkeypatch.setattr(admin, "trust_submitter", boom)

    # The event is already published by this point; a trust failure must not
    # surface as an approval error.
    result = admin._maybe_trust_submitter(
        {"trust_submitter": "1"}, {"submitter_email": "a@example.com"},
        {"email": "admin@x.com"})
    assert result is None


# ── Auto-approval ──────────────────────────────────────────────────

@pytest.fixture
def promoted(monkeypatch):
    calls = {"promote": [], "status": []}
    monkeypatch.setattr(submit, "promote_draft_to_event",
                        lambda merged: calls["promote"].append(merged))
    monkeypatch.setattr(submit, "update_draft_status",
                        lambda did, st, rev=None: calls["status"].append((did, st, rev)))
    return calls


def test_auto_approve_publishes_and_marks_approved(promoted):
    ok = submit._auto_approve_event(
        "abc123", {"title": "T", "date": "2026-09-01"}, "a@example.com")

    assert ok is True
    assert promoted["promote"][0]["id"] == "abc123"
    assert promoted["promote"][0]["submitter_email"] == "a@example.com"

    draft_id, status, reviewer = promoted["status"][0]
    assert (draft_id, status) == ("abc123", "APPROVED")
    # A recognizable automated reviewer keeps the history honest about the
    # fact that no human looked at this one.
    assert reviewer == submit.AUTO_REVIEWER


def test_auto_approve_failure_leaves_the_draft_pending(monkeypatch):
    def boom(merged):
        raise RuntimeError("promotion failed")
    monkeypatch.setattr(submit, "promote_draft_to_event", boom)
    marked = []
    monkeypatch.setattr(submit, "update_draft_status",
                        lambda *a, **kw: marked.append(a))

    ok = submit._auto_approve_event("abc", {"title": "T"}, "a@example.com")

    # Falls back to the ordinary queue rather than losing the submission.
    assert ok is False
    assert marked == []


def test_auto_approve_does_not_mutate_the_caller_s_draft_data(promoted):
    draft_data = {"title": "T", "date": "2026-09-01"}
    submit._auto_approve_event("abc123", draft_data, "a@example.com")
    assert "id" not in draft_data
    assert "submitter_email" not in draft_data
