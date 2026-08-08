"""Tests for trusted submitters (auto-approval).

Run: python -m pytest test_trusted.py
"""
import os

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import db  # noqa: E402


class FakeTable:
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

    def query(self, **kwargs):
        # Only GSI1 / TRUSTED is queried by the code under test.
        rows = [i for i in self.items.values() if i.get("GSI1PK") == "TRUSTED"]
        return {"Items": rows}


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(db, "_get_table", lambda: fake)
    return fake


# ── Basic trust lifecycle ──────────────────────────────────────────

def test_unknown_submitter_is_not_trusted(table):
    assert not db.is_trusted_submitter("stranger@example.com")


def test_trust_then_check(table):
    db.trust_submitter("a@example.com", trusted_by="admin@example.com")
    assert db.is_trusted_submitter("a@example.com")


def test_untrust_revokes(table):
    db.trust_submitter("a@example.com")
    db.untrust_submitter("a@example.com")
    assert not db.is_trusted_submitter("a@example.com")


def test_untrust_unknown_address_is_harmless(table):
    assert db.untrust_submitter("nobody@example.com") is True
    assert not db.is_trusted_submitter("nobody@example.com")


# ── Identity normalization ─────────────────────────────────────────
# Trust is keyed by email across two auth paths, so casing and stray
# whitespace must not create a second, separate identity.

@pytest.mark.parametrize("stored,looked_up", [
    ("A@Example.COM", "a@example.com"),
    ("a@example.com", "  A@EXAMPLE.com  "),
    ("  Mixed@Case.org ", "mixed@case.org"),
])
def test_trust_is_case_and_whitespace_insensitive(table, stored, looked_up):
    db.trust_submitter(stored)
    assert db.is_trusted_submitter(looked_up)


def test_trusting_twice_does_not_duplicate(table):
    db.trust_submitter("a@example.com", trusted_by="one@example.com")
    db.trust_submitter("A@EXAMPLE.COM", trusted_by="two@example.com")
    assert len(db.list_trusted_submitters()) == 1


def test_retrust_preserves_original_trusted_at(table):
    db.trust_submitter("a@example.com", trusted_by="one@example.com")
    first = db.list_trusted_submitters()[0]["trusted_at"]
    db.trust_submitter("a@example.com", trusted_by="two@example.com")
    assert db.list_trusted_submitters()[0]["trusted_at"] == first


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_email_is_never_trusted(table, blank):
    assert db.trust_submitter(blank) is None
    assert not db.is_trusted_submitter(blank)
    assert db.list_trusted_submitters() == []


# ── Listing ────────────────────────────────────────────────────────

def test_list_reports_who_and_when(table):
    db.trust_submitter("a@example.com", trusted_by="admin@example.com",
                       note="runs the Python meetup")
    entry = db.list_trusted_submitters()[0]
    assert entry["email"] == "a@example.com"
    assert entry["trusted_by"] == "admin@example.com"
    assert entry["note"] == "runs the Python meetup"
    assert entry["trusted_at"]


def test_list_is_empty_when_nobody_is_trusted(table):
    assert db.list_trusted_submitters() == []


def test_list_only_returns_trust_records(table):
    db.trust_submitter("a@example.com")
    # An unrelated row must not leak into the trusted list.
    table.put_item({"PK": "DRAFT#x", "SK": "META", "GSI1PK": "STATUS#pending"})
    assert [t["email"] for t in db.list_trusted_submitters()] == ["a@example.com"]
