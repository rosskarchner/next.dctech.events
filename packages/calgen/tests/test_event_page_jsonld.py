"""event_page.html's JSON-LD block gained two new pieces this session:

- image: previously absent entirely, forfeiting Google's Event rich-result
  image eligibility. Originally fell back to the site-wide og:image since
  events had no per-event image; next_dctech_events-409 later gave every
  event a real generated social card (see og_image.py), and because this
  field already read from self.og_image() rather than hardcoding the
  fallback path, it picked up the per-event image for free — no template
  change needed here, just this test's expectation updating to match.
- isAccessibleForFree: previously hardcoded true regardless of event.cost.
  event.cost is free text ("$10", "Suggested $5 donation", ...), not a
  structured amount, so there's nothing reliable to parse a real price out
  of — the fix is to only claim free when the field is empty/plainly free,
  and omit the claim otherwise rather than assert something false.

These tests render the real event_page.html end to end (through Flask, so
`self.og_image()` / base.html inheritance and Jinja's `request` global all
resolve exactly as they do at build time) and parse the emitted script as
JSON, since the template was restructured to a leading-comma style to keep
the object valid across every combination of optional properties.

Run: python -m pytest test_event_page_jsonld.py
"""
import json
import re
from pathlib import Path

import pytest

from calgen.app import create_app

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

BASE_EVENT = {
    'title': 'Intro to Rust',
    'date': '2026-10-05',
    'time': '18:00',
    'url': 'https://meetup.com/dc-rust/events/123',
    'location': '1875 Connecticut Ave NW, Washington, DC',
    'group': 'DC Rust',
    'group_website': 'https://dcrust.org',
    'virtual': False,
    'cost': '',
}


@pytest.fixture
def client(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def _get_jsonld(monkeypatch, client, event_overrides):
    event = {**BASE_EVENT, **event_overrides}
    # Patches where event_page's route handler actually resolves the name
    # from (calgen.routes.events, since the app.py split) — Python looks up
    # a bare name via the *defining* module's globals, not through whatever
    # else re-exports it, so patching calgen.app here would silently no-op.
    monkeypatch.setattr('calgen.routes.events.get_event_by_slug', lambda slug: event)

    resp = client.get('/events/intro-to-rust/')
    assert resp.status_code == 200

    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        resp.get_data(as_text=True), re.S)
    assert match, 'no JSON-LD script tag found on the event page'
    return json.loads(match.group(1))


def test_jsonld_is_valid_with_every_optional_field_present(monkeypatch, client):
    data = _get_jsonld(monkeypatch, client, {})
    assert data['@type'] == 'Event'
    assert data['name'] == 'Intro to Rust'


def test_jsonld_is_valid_with_no_optional_fields_at_all(monkeypatch, client):
    """location/group/url/cost all absent -- the minimal event."""
    data = _get_jsonld(monkeypatch, client, {
        'url': '', 'location': '', 'group': '', 'group_website': '', 'cost': '',
    })
    assert 'location' not in data
    assert 'organizer' not in data
    assert 'offers' not in data
    assert data['isAccessibleForFree'] is True


def test_image_is_the_per_event_social_card(monkeypatch, client):
    data = _get_jsonld(monkeypatch, client, {})
    assert data['image'][0].endswith('/static/og/intro-to-rust.png')


@pytest.mark.parametrize('cost', ['', 'Free', 'FREE', 'free', 'No cost', '$0', 'n/a', 'None'])
def test_empty_or_explicitly_free_cost_asserts_isaccessibleforfree(monkeypatch, client, cost):
    data = _get_jsonld(monkeypatch, client, {'cost': cost})
    assert data['isAccessibleForFree'] is True


@pytest.mark.parametrize('cost', ['$10', '$15-20', 'Suggested $5 donation', '10 dollars'])
def test_a_real_cost_omits_the_free_claim_rather_than_guessing(monkeypatch, client, cost):
    data = _get_jsonld(monkeypatch, client, {'cost': cost})
    assert 'isAccessibleForFree' not in data
    # and it must not invent structured price data it can't actually derive
    assert 'price' not in data
    assert 'priceCurrency' not in data


def test_virtual_event_jsonld_is_still_valid(monkeypatch, client):
    data = _get_jsonld(monkeypatch, client, {
        'virtual': True, 'location': 'Zoom', 'cost': '$10',
    })
    assert data['location']['@type'] == 'VirtualLocation'
    assert 'isAccessibleForFree' not in data
