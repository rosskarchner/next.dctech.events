"""Tests for the MCP correction-review tools.

The business logic (field allowlisting, re-validation on approval, GSI
collision with drafts) is covered in api/test_corrections.py — these tools
are thin wrappers, so what's tested here is only the wiring: that each tool
calls the right db function with the right arguments, and that MCP_REVIEWER
(not some other identity) is recorded as the approver/rejecter.

Run: python -m pytest test_correction_tools.py
"""
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import server  # noqa: E402

list_pending = server.list_pending_corrections
get_correction = server.get_correction
approve = server.approve_correction
reject = server.reject_correction

PENDING = {"id": "c1", "status": "pending", "target_guid": "g1",
          "target_source": "manual", "fields": {"time": "20:00"},
          "reason": "moved", "submitter_email": "reporter@example.com"}


@pytest.fixture
def fake(monkeypatch):
    calls = {}

    monkeypatch.setattr(server.db, "get_corrections_by_status",
                        lambda status='pending': [dict(PENDING)] if status == 'pending' else [])
    monkeypatch.setattr(server.db, "get_correction",
                        lambda cid: dict(PENDING) if cid == "c1" else None)

    def _approve(cid, reviewer):
        calls["approve"] = (cid, reviewer)
        return {"correction_id": cid, "guid": "g1", "overlay": {"time": "20:00"}}

    def _reject(cid, reviewer, reason=None):
        calls["reject"] = (cid, reviewer, reason)
        return {"correction_id": cid, "status": "rejected"}

    monkeypatch.setattr(server.db, "approve_correction", _approve)
    monkeypatch.setattr(server.db, "reject_correction", _reject)
    return calls


def test_list_pending_corrections_returns_the_queue(fake):
    assert list_pending() == [PENDING]


def test_get_correction_returns_the_record(fake):
    assert get_correction("c1") == PENDING


def test_get_correction_raises_for_an_unknown_id(fake):
    with pytest.raises(ValueError, match="No correction"):
        get_correction("ghost")


def test_approve_correction_uses_the_mcp_reviewer_identity(fake):
    result = approve("c1")
    assert fake["approve"] == ("c1", server.MCP_REVIEWER)
    assert result["guid"] == "g1"


def test_reject_correction_passes_through_the_reason(fake):
    reject("c1", reason="not a real issue")
    assert fake["reject"] == ("c1", server.MCP_REVIEWER, "not a real issue")


def test_reject_correction_reason_defaults_to_none(fake):
    reject("c1")
    assert fake["reject"] == ("c1", server.MCP_REVIEWER, None)
