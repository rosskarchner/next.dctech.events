"""Week pages (/week/<week_id>/) must drop virtual events, same as the
homepage (filter_in_person_events) and the newsletter (is_virtual_event) —
next_dctech_events week-page-includes-virtual report. week_page previously
skipped this filter entirely, so a Zoom-only event showed up on its ISO
week page even though it never appeared on the homepage or in the
newsletter.

Run: python -m pytest test_week_page_excludes_virtual.py
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from calgen.app import create_app
from calgen.routes.common import get_week_identifier

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

_SOON = date.today() + timedelta(days=2)
_WEEK_ID = get_week_identifier(_SOON)


def _event(guid, virtual):
    return {
        'guid': guid, 'title': f'Event {guid}', 'url': 'https://example.com/e',
        'date': _SOON.isoformat(), 'time': '18:00',
        'location': '', 'group': '', 'categories': [],
        'location_type': 'virtual' if virtual else 'physical',
    }


@pytest.fixture
def client(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    monkeypatch.setattr(
        'calgen.routes.listings.get_events',
        lambda *a, **k: [_event('in-person', virtual=False), _event('online', virtual=True)])
    monkeypatch.setattr('calgen.routes.listings.get_archived_week', lambda *a, **k: None)
    return app


def test_week_page_drops_virtual_events(client):
    html = client.test_client().get(f'/week/{_WEEK_ID}/').get_data(as_text=True)
    assert 'Event in-person' in html
    assert 'Event online' not in html
