"""Tests for the per-submission admin notification.

Run: python -m pytest test_notify.py
"""
import os
import sys
import types

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("SUBMIT_KEY_ID", "test-key")

from routes import submit  # noqa: E402


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


@pytest.fixture
def ses(monkeypatch):
    fake = FakeSes()
    fake_boto3 = types.SimpleNamespace(client=lambda name: fake)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return fake


DRAFT = {
    "title": "Python DC Monthly",
    "date": "2026-09-15",
    "time": "18:30",
    "location": "Washington, DC",
    "url": "https://example.com/e",
}


def test_notification_is_sent_to_the_admin(ses):
    submit._notify_admin("abc123", "event", DRAFT, "someone@example.com", False)

    assert len(ses.sent) == 1
    call = ses.sent[0]
    assert call["Destination"]["ToAddresses"] == [submit.ADMIN_EMAIL]
    assert "Python DC Monthly" in call["Content"]["Simple"]["Subject"]["Data"]


def test_reply_goes_to_the_submitter(ses):
    # Replying to the notification should reach the person who submitted,
    # not the site's own outbound address.
    submit._notify_admin("abc123", "event", DRAFT, "someone@example.com", False)
    assert ses.sent[0]["ReplyToAddresses"] == ["someone@example.com"]


def test_reply_falls_back_when_submitter_unknown(ses):
    submit._notify_admin("abc123", "event", DRAFT, "", False)
    assert ses.sent[0]["ReplyToAddresses"] == [submit.REPLY_TO_EMAIL]


def test_body_carries_the_details_worth_scanning(ses):
    submit._notify_admin("abc123", "event", DRAFT, "someone@example.com", False)
    text = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

    for expected in ("Python DC Monthly", "2026-09-15 18:30",
                     "Washington, DC", "https://example.com/e",
                     "someone@example.com", "abc123"):
        assert expected in text


def test_pending_submission_says_waiting_for_review(ses):
    submit._notify_admin("abc123", "event", DRAFT, "a@example.com", False)
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "Waiting for review" in body


def test_auto_published_submission_is_flagged_differently(ses):
    # These never hit the queue, so the mail is the only signal they happened.
    submit._notify_admin("abc123", "event", DRAFT, "a@example.com", True)
    call = ses.sent[0]
    assert "auto-published" in call["Content"]["Simple"]["Subject"]["Data"]
    assert "trusted submitter" in call["Content"]["Simple"]["Body"]["Text"]["Data"]


def test_group_submission_is_labelled_as_a_group(ses):
    submit._notify_admin("g1", "group", {"name": "Some Group"}, "a@x.com", False)
    assert "group" in ses.sent[0]["Content"]["Simple"]["Subject"]["Data"].lower()


def test_missing_fields_do_not_break_the_mail(ses):
    submit._notify_admin("abc123", "event", {}, "a@example.com", False)
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "(untitled)" in body


def test_long_titles_do_not_produce_an_oversized_subject(ses):
    submit._notify_admin("abc", "event", {"title": "x" * 500}, "a@x.com", False)
    assert len(ses.sent[0]["Content"]["Simple"]["Subject"]["Data"]) <= 200


def test_ses_failure_never_propagates(monkeypatch):
    class Boom:
        def send_email(self, **kwargs):
            raise RuntimeError("SES is down")
    monkeypatch.setitem(sys.modules, "boto3",
                        types.SimpleNamespace(client=lambda name: Boom()))

    # A notification problem must not cost the user their submission.
    submit._notify_admin("abc123", "event", DRAFT, "a@example.com", False)
