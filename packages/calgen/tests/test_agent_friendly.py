"""Search- and agent-facing output (next_dctech_events-4rr).

- JSON-LD times carry the site's UTC offset, and the venue is split from the
  street address (calgen.structured).
- /…/events.json slices and /calendar.json share one public event shape.
- /llms.txt maps the site for language-model agents.
- Event pages get an honest sitemap <lastmod> carried across builds
  (calgen.lastmod), and IndexNow is told about exactly those changes.

Run: python -m pytest test_agent_friendly.py
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from calgen import indexnow, lastmod
from calgen.app import create_app
from calgen.event_utils import event_slug
from calgen.routes.common import get_week_identifier
from calgen.structured import iso_local, public_event, split_place

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

SOON = date.today() + timedelta(days=3)


# ── iso_local ─────────────────────────────────────────────────────────────────

def test_winter_time_gets_eastern_standard_offset():
    assert iso_local('2026-12-01', '17:30') == '2026-12-01T17:30:00-05:00'


def test_summer_time_gets_daylight_offset():
    assert iso_local('2026-07-01', '09:00') == '2026-07-01T09:00:00-04:00'


@pytest.mark.parametrize('time', ['', None, 'TBD', '25:99'])
def test_untimed_or_unparseable_stays_a_bare_date(time):
    assert iso_local('2026-12-01', time) == '2026-12-01'


def test_recurring_per_date_time_dict():
    assert iso_local('2026-12-01', {'2026-12-01': '18:00'}) == '2026-12-01T18:00:00-05:00'
    assert iso_local('2026-12-02', {'2026-12-01': '18:00'}) == '2026-12-02'


# ── split_place ───────────────────────────────────────────────────────────────

def test_venue_street_and_zip_are_separated():
    place = split_place('Sudhouse DC, 1340 U St NW, Washington, DC 20009, USA, Washington, DC')
    assert place == {'name': 'Sudhouse DC', 'street': '1340 U St NW', 'postal_code': '20009'}


def test_suite_stays_with_the_street_line():
    place = split_place('HacDC, 3333 14th St NW, Suite M110, Washington, DC')
    assert place['name'] == 'HacDC'
    assert place['street'] == '3333 14th St NW, Suite M110'


def test_repeated_meetup_address_is_collapsed():
    place = split_place('1501 Langston Blvd., Arlington, VA 22209, USA, '
                        '1501 Langston Blvd., Arlington, VA 22209, USA')
    assert place['name'] is None
    assert place['street'] == '1501 Langston Blvd.'
    assert place['postal_code'] == '22209'


def test_no_street_falls_back_to_the_whole_string():
    place = split_place('Hyatt Regency Tysons Corner Center, Tysons Corner, VA')
    assert place['street'] is None
    assert place['name'] == 'Hyatt Regency Tysons Corner Center, Tysons Corner, VA'


# ── Event page JSON-LD ────────────────────────────────────────────────────────

EVENT = {
    'guid': 'abcdef1234567890', 'title': 'December GeoDC', 'date': '2026-12-02',
    'time': '18:00', 'end_date': '2026-12-02', 'end_time': '20:00',
    'url': 'https://www.meetup.com/geo-dc/events/1/',
    'location': 'Sudhouse DC, 1340 U St NW, Washington, DC 20009, USA',
    'group': 'Geo DC', 'categories': ['gis'], 'location_type': 'physical',
    'region': 'dc', 'cost': '',
}


@pytest.fixture
def client():
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def test_event_page_jsonld_has_offsets_and_split_address(monkeypatch, client):
    monkeypatch.setattr('calgen.routes.events.get_event_by_slug', lambda slug: dict(EVENT))
    html = client.get('/events/x/').get_data(as_text=True)
    data = json.loads(re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    assert data['startDate'] == '2026-12-02T18:00:00-05:00'
    assert data['endDate'] == '2026-12-02T20:00:00-05:00'
    assert data['location']['name'] == 'Sudhouse DC'
    assert data['location']['address']['streetAddress'] == '1340 U St NW'
    assert data['location']['address']['postalCode'] == '20009'


# ── JSON slices ───────────────────────────────────────────────────────────────

def _soon(**overrides):
    return {**EVENT, 'date': SOON.isoformat(), 'end_date': SOON.isoformat(), **overrides}


def test_public_event_shape():
    record = public_event(_soon(), 'https://dctech.events')
    assert record['start'].startswith(f'{SOON.isoformat()}T18:00:00')
    assert record['url'] == EVENT['url']
    assert record['page'] == f"https://dctech.events/events/{event_slug(_soon())}/"
    assert record['all_day'] is False
    assert record['cost'] is None
    assert record['categories'] == ['gis']


def test_category_slice(monkeypatch, client):
    monkeypatch.setattr('calgen.routes.categories.get_categories',
                        lambda: {'gis': {'name': 'GIS'}})
    monkeypatch.setattr('calgen.routes.categories.get_events',
                        lambda *a, **k: [_soon(), _soon(guid='other', title='Not GIS', categories=['ai'])])
    resp = client.get('/categories/gis/events.json')
    assert resp.mimetype == 'application/json'
    doc = resp.get_json()
    assert doc['count'] == 1 and doc['events'][0]['title'] == 'December GeoDC'
    assert doc['page'].endswith('/categories/gis/')
    assert doc['timezone'] == 'America/New_York'
    assert client.get('/categories/nope/events.json').status_code == 404


def test_week_slice_matches_week_page_rules(monkeypatch, client):
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda *a, **k: [
        _soon(), _soon(guid='zoom', title='Online', location_type='virtual')])
    monkeypatch.setattr('calgen.routes.listings.get_archived_week', lambda *a, **k: None)
    doc = client.get(f'/week/{get_week_identifier(SOON)}/events.json').get_json()
    assert [e['title'] for e in doc['events']] == ['December GeoDC']
    assert client.get('/week/garbage/events.json').status_code == 404


def test_calendar_json_is_the_public_shape(monkeypatch, client):
    monkeypatch.setattr('calgen.routes.feeds.get_events', lambda *a, **k: [_soon()])
    doc = client.get('/calendar.json').get_json()
    assert doc['schema_version'] == 1
    assert set(doc['events'][0]) >= {'title', 'start', 'url', 'page', 'location_type'}


def test_llms_txt(monkeypatch, client):
    monkeypatch.setattr('calgen.routes.feeds.get_events', lambda *a, **k: [_soon()])
    monkeypatch.setattr('calgen.routes.feeds.get_categories',
                        lambda: {'gis': {'name': 'GIS', 'description': 'Maps'}})
    resp = client.get('/llms.txt')
    assert resp.mimetype == 'text/plain'
    body = resp.get_data(as_text=True)
    assert body.startswith('# ')
    assert '/calendar.json' in body
    assert '/categories/gis/events.json' in body
    assert 'America/New_York' in body


# ── lastmod ───────────────────────────────────────────────────────────────────

TODAY = '2026-11-25'


def test_without_previous_state_only_createdat_dates_a_page():
    known = _soon(guid='known')
    unknown = _soon(guid='unknown', title='Other')
    index = ({'known': '2026-11-01T12:00:00Z'}, {})
    state, changed = lastmod.compute_state([known, unknown], None, TODAY, index)
    assert state[event_slug(known)]['lastmod'] == '2026-11-01'
    assert state[event_slug(unknown)]['lastmod'] is None
    assert changed == []


def test_unchanged_keeps_its_date_and_changed_gets_today():
    event = _soon()
    first, _ = lastmod.compute_state([event], {}, '2026-11-20')
    slug = event_slug(event)
    assert first[slug]['lastmod'] == '2026-11-20'

    same, changed = lastmod.compute_state([event], first, TODAY)
    assert same[slug]['lastmod'] == '2026-11-20' and changed == []

    # Same guid (so same slug), new location: a real change.
    moved, changed = lastmod.compute_state([{**event, 'location': 'Elsewhere'}], first, TODAY)
    assert moved[slug]['lastmod'] == TODAY and changed == [slug]


def test_description_changes_do_not_count():
    event = _soon(description='old')
    first, _ = lastmod.compute_state([event], {}, '2026-11-20')
    _, changed = lastmod.compute_state([{**event, 'description': 'new'}], first, TODAY)
    assert changed == []


def test_sitemap_emits_event_lastmod(monkeypatch, client):
    event = _soon()
    monkeypatch.setattr('calgen.routes.feeds.get_events_by_slug', lambda: {event_slug(event): event})
    monkeypatch.setattr('calgen.routes.feeds.get_event_lastmods',
                        lambda: {event_slug(event): '2026-11-20'})
    xml = client.get('/sitemap.xml').get_data(as_text=True)
    assert (f'<loc>/events/{event_slug(event)}/</loc>\n    <lastmod>2026-11-20</lastmod>' in xml)


def test_state_round_trips_through_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    event = _soon()
    state, _ = lastmod.compute_state([event], {}, TODAY)
    lastmod.write_state(state, [event_slug(event)], TODAY)
    # The published copy is what the next build fetches back as "previous".
    published = tmp_path / 'static' / 'lastmod.json'
    assert lastmod.load_previous(str(published)) == state
    assert lastmod.get_event_lastmods() == {event_slug(event): TODAY}
    assert lastmod.get_changed_slugs() == [event_slug(event)]


# ── IndexNow ──────────────────────────────────────────────────────────────────

def test_indexnow_payload_and_key_route(monkeypatch):
    cfg = {'indexnow_key': 'k' * 32, 'base_url': 'https://dctech.events'}
    monkeypatch.setattr('calgen.indexnow.get_config', lambda: cfg)
    monkeypatch.setattr('calgen.indexnow.get_changed_slugs', lambda: ['2026-12-02-geodc-abcdef12'])

    sent = {}

    class _Resp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout):
        sent['body'] = json.loads(request.data)
        return _Resp()

    assert 'submitted 2 URLs' in indexnow.submit_changed(opener=opener)
    assert sent['body']['host'] == 'dctech.events'
    assert sent['body']['keyLocation'] == f"https://dctech.events/{'k' * 32}.txt"
    assert sent['body']['urlList'] == [
        'https://dctech.events/', 'https://dctech.events/events/2026-12-02-geodc-abcdef12/']

    app = create_app(site_dir=str(SITE_DIR))
    assert app.test_client().get(f"/{'k' * 32}.txt").get_data(as_text=True) == 'k' * 32


def test_indexnow_is_off_without_a_key(monkeypatch):
    monkeypatch.setattr('calgen.indexnow.get_config', lambda: {})
    assert 'skipped' in indexnow.submit_changed()


def test_indexnow_skips_when_nothing_changed(monkeypatch):
    monkeypatch.setattr('calgen.indexnow.get_config',
                        lambda: {'indexnow_key': 'k', 'base_url': 'https://x.test'})
    monkeypatch.setattr('calgen.indexnow.get_changed_slugs', lambda: [])
    assert 'nothing changed' in indexnow.submit_changed()
