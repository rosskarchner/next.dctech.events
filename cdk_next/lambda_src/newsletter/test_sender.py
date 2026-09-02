"""Tests for the weekly sender's per-subscriber personalization.

No prior test suite existed for this Lambda — these focus on the new,
genuinely risky logic: subscribers with different (or no) preferences are
grouped and rendered correctly, only one render happens per distinct group
(not per subscriber), and each subscriber's own preferences link — not a
shared one — lands in the content actually sent to them.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_sender.py
"""
import os
import sys

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("CONTACT_LIST_NAME", "test-list")
os.environ.setdefault("TOPIC_NAME", "dctech")

# db.py/magic_link.py/constants.py aren't physically here in lambda_src/
# — build_lambdas.sh copies them in from lambda_src/api/ at build time.
# Point at the real source so tests see exactly what the deployed Lambda
# does, without duplicating the files into this directory too.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))

import db  # noqa: E402
import sender  # noqa: E402


@pytest.fixture
def no_real_aws(monkeypatch):
    """Every test here mocks db/ses/render/magic_link directly — nothing
    should ever reach a real boto3 client."""
    monkeypatch.setattr(sender, "build_site", lambda: None)
    monkeypatch.setattr(
        sender.magic_link, "generate_token",
        lambda email, purpose='submit': (1700000000, f"sig-for-{email}"))
    monkeypatch.setattr(
        sender.magic_link, "build_link",
        lambda email, ts, sig, path='': f"https://dctech.events{path}?e={email}&s={sig}")


def _contact(email, opted_in=True):
    status = 'OPT_IN' if opted_in else 'OPT_OUT'
    return {'EmailAddress': email,
            'TopicPreferences': [{'TopicName': 'dctech', 'SubscriptionStatus': status}]}


# ── _group_subscribers ──────────────────────────────────────────────

def test_subscribers_with_no_preferences_share_the_unfiltered_group(monkeypatch):
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: None)
    groups = sender._group_subscribers(["a@example.com", "b@example.com"])
    assert groups == {(tuple(), tuple()): ["a@example.com", "b@example.com"]}


def test_subscribers_with_different_preferences_land_in_different_groups(monkeypatch):
    prefs = {
        "a@example.com": {"categories": ["ai"], "regions": []},
        "b@example.com": {"categories": ["cloud"], "regions": []},
    }
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: prefs.get(email))
    groups = sender._group_subscribers(["a@example.com", "b@example.com"])
    assert set(groups.keys()) == {(("ai",), tuple()), (("cloud",), tuple())}
    assert groups[(("ai",), tuple())] == ["a@example.com"]


def test_subscribers_with_identical_preferences_share_one_group(monkeypatch):
    prefs = {"categories": ["ai"], "regions": ["dc"]}
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: dict(prefs))
    groups = sender._group_subscribers(["a@example.com", "b@example.com"])
    assert groups == {(("ai",), ("dc",)): ["a@example.com", "b@example.com"]}


# ── send_newsletter_to_subscribers ──────────────────────────────────

class FakeSes:
    def __init__(self, contacts):
        self._contacts = contacts
        self.sent = []

    def list_contacts(self, **kwargs):
        return {'Contacts': self._contacts}

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


def test_only_one_render_call_per_distinct_preference_group(monkeypatch, no_real_aws):
    contacts = [_contact("a@example.com"), _contact("b@example.com"), _contact("c@example.com")]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)

    prefs = {"a@example.com": {"categories": ["ai"], "regions": []}}  # b, c: unfiltered
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: prefs.get(email))

    render_calls = []

    def fake_render(categories, regions):
        render_calls.append((tuple(categories), tuple(regions)))
        return f"<html>content for {categories}</html>", "text"

    monkeypatch.setattr(sender, "render_for_filters", fake_render)

    result = sender.send_newsletter_to_subscribers()
    assert result['status'] == 'completed'
    assert result['successful_sends'] == 3
    # Two distinct groups (a alone; b+c together) → exactly two renders,
    # not three.
    assert len(render_calls) == 2
    assert len(fake_ses.sent) == 3


def test_each_subscriber_gets_their_own_preferences_link(monkeypatch, no_real_aws):
    contacts = [_contact("a@example.com"), _contact("b@example.com")]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: None)
    monkeypatch.setattr(
        sender, "render_for_filters",
        lambda categories, regions: (
            "<html>Manage: __PREFERENCES_LINK__</html>", "text"))

    sender.send_newsletter_to_subscribers()

    sent_by_email = {s['Destination']['ToAddresses'][0]: s for s in fake_ses.sent}
    a_html = sent_by_email['a@example.com']['Content']['Template']['TemplateData']
    b_html = sent_by_email['b@example.com']['Content']['Template']['TemplateData']
    assert 'a@example.com' in a_html
    assert 'b@example.com' in b_html
    assert a_html != b_html
    assert '__PREFERENCES_LINK__' not in a_html


def test_unsubscribed_contacts_are_skipped(monkeypatch, no_real_aws):
    contacts = [_contact("subscribed@example.com", opted_in=True),
                _contact("optedout@example.com", opted_in=False)]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: None)
    monkeypatch.setattr(sender, "render_for_filters", lambda c, r: ("<html></html>", "text"))

    sender.send_newsletter_to_subscribers()
    assert len(fake_ses.sent) == 1
    assert fake_ses.sent[0]['Destination']['ToAddresses'] == ['subscribed@example.com']


def test_only_addresses_narrows_the_send(monkeypatch, no_real_aws):
    contacts = [_contact("a@example.com"), _contact("b@example.com")]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: None)
    monkeypatch.setattr(sender, "render_for_filters", lambda c, r: ("<html></html>", "text"))

    sender.send_newsletter_to_subscribers(only_addresses=["a@example.com"])
    assert len(fake_ses.sent) == 1
    assert fake_ses.sent[0]['Destination']['ToAddresses'] == ['a@example.com']


def test_dry_run_sends_nothing(monkeypatch, no_real_aws):
    contacts = [_contact("a@example.com")]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: None)
    monkeypatch.setattr(sender, "render_for_filters", lambda c, r: ("<html></html>", "text"))

    result = sender.send_newsletter_to_subscribers(dry_run=True)
    assert result['successful_sends'] == 1
    assert fake_ses.sent == []


def test_a_render_failure_for_one_group_does_not_block_other_groups(monkeypatch, no_real_aws):
    contacts = [_contact("broken@example.com"), _contact("fine@example.com")]
    fake_ses = FakeSes(contacts)
    monkeypatch.setattr(sender, "ses", fake_ses)
    prefs = {"broken@example.com": {"categories": ["ghost-category"], "regions": []}}
    monkeypatch.setattr(db, "get_subscriber_preferences", lambda email: prefs.get(email))

    def fake_render(categories, regions):
        # sender.py calls render_for_filters with plain lists (converted
        # from the tuple group key), not the tuples themselves.
        if categories == ["ghost-category"]:
            raise RuntimeError("boom")
        return "<html></html>", "text"

    monkeypatch.setattr(sender, "render_for_filters", fake_render)

    result = sender.send_newsletter_to_subscribers()
    assert result['successful_sends'] == 1
    assert result['failed_sends'] == 1
    assert fake_ses.sent[0]['Destination']['ToAddresses'] == ['fine@example.com']


def test_build_site_failure_aborts_before_sending_anything(monkeypatch):
    def fake_build_site():
        raise RuntimeError("export failed")
    monkeypatch.setattr(sender, "build_site", fake_build_site)

    result = sender.send_newsletter_to_subscribers()
    assert result['status'] == 'error'
    assert 'export failed' in result['reason']
