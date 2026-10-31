"""Every listed event carries a machine-readable start (next_dctech_events-bm9).

The homepage JSON-LD and the h-event markup on every listing used to emit a
start only when the event had a clock time, so all-day events (most
conferences) had none. schema.org startDate and microformats dt-start both
accept a bare date. A multi-day event's "continuing" day must still point at
its real start, not the day it is listed under.

Run: python -m pytest test_event_start_markup.py
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from calgen.app import create_app

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

D1 = date.today() + timedelta(days=3)
D2 = D1 + timedelta(days=1)

EVENTS = [
    {'guid': 'conf0001', 'title': 'Two-Day Conference', 'url': 'https://example.com/conf',
     'date': D1.isoformat(), 'end_date': D2.isoformat(), 'time': '', 'categories': []},
    {'guid': 'meet0001', 'title': 'Evening Meetup', 'url': 'https://example.com/meet',
     'date': D1.isoformat(), 'time': '18:00', 'categories': []},
]


@pytest.fixture
def html(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda *a, **k: [dict(e) for e in EVENTS])
    monkeypatch.setattr('calgen.routes.listings.filter_in_person_events', lambda events: events)
    return app.test_client().get('/').get_data(as_text=True)


def _jsonld_items(html):
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        data = json.loads(block)
        if data.get('@type') == 'ItemList':
            return [i['item'] for i in data['itemListElement']]
    raise AssertionError('no ItemList JSON-LD on the homepage')


def test_jsonld_gives_every_event_a_start(html):
    items = _jsonld_items(html)
    assert items and all('startDate' in i for i in items)


def test_jsonld_all_day_event_uses_its_real_start_and_end_on_every_day(html):
    conf = [i for i in _jsonld_items(html) if i['name'] == 'Two-Day Conference']
    assert len(conf) == 2  # listed on both days
    for item in conf:
        assert item['startDate'] == D1.isoformat()
        assert item['endDate'] == D2.isoformat()


def test_jsonld_timed_event_keeps_its_time(html):
    [meet] = [i for i in _jsonld_items(html) if i['name'] == 'Evening Meetup']
    assert meet['startDate'] == f'{D1.isoformat()}T18:00'
    assert 'endDate' not in meet


def test_h_event_all_day_start_and_end_are_bare_dates(html):
    assert f'<data class="dt-start" value="{D1.isoformat()}"></data>' in html
    assert f'<data class="dt-end" value="{D2.isoformat()}"></data>' in html
    # The continuing day must not claim to start on D2.
    assert f'class="dt-start" value="{D2.isoformat()}"' not in html


def test_h_event_timed_start_is_unchanged(html):
    assert f'<time class="dt-start" datetime="{D1.isoformat()}T18:00">' in html
