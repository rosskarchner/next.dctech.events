"""Meetup emits a schema.org Event subtype, not plain "Event".

fetch_json_ld_data checked `item.get('@type') == 'Event'`, so a workshop that
Meetup categorized (and rendered) as `ScreeningEvent` never matched — the loop
skipped straight past `eventAttendanceMode` and the event kept its default
`is_virtual: False`, even though the JSON-LD said OnlineEventAttendanceMode
("The Art of the Chart", nova-scribes — next_dctech_events online-flag miss).
"""
import json

import pytest

from calgen.calendars import fetch_json_ld_data


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def _ld_page(ld_object):
    script = json.dumps(ld_object)
    return f'<html><head><script type="application/ld+json">{script}</script></head></html>'


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr('calgen.calendars.JSON_LD_CACHE_DIR', str(tmp_path))


def _mock_get(monkeypatch, html):
    monkeypatch.setattr(
        'calgen.calendars.safe_get',
        lambda *a, **k: _FakeResponse(html),
    )


def test_screening_event_online_is_detected(monkeypatch):
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': 'ScreeningEvent',
        'name': 'The Art of the Chart',
        'eventAttendanceMode': 'https://schema.org/OnlineEventAttendanceMode',
        'location': {'@type': 'VirtualLocation', 'url': 'https://www.meetup.com/x/'},
    }))
    result = fetch_json_ld_data('https://www.meetup.com/nova-scribes/events/316008929/')
    assert result['is_virtual'] is True
    assert result['title'] == 'The Art of the Chart'


@pytest.mark.parametrize('event_type', [
    'Event', 'BusinessEvent', 'EducationEvent', 'SocialEvent', 'LiteraryEvent',
])
def test_known_event_subtypes_are_recognized(monkeypatch, event_type):
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': event_type,
        'name': 'Some Talk',
        'eventAttendanceMode': 'https://schema.org/OnlineEventAttendanceMode',
    }))
    result = fetch_json_ld_data('https://www.meetup.com/some-group/events/1/')
    assert result['is_virtual'] is True


def test_a_physical_screening_event_is_not_forced_virtual(monkeypatch):
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': 'ScreeningEvent',
        'name': 'In-Person Workshop',
        'eventAttendanceMode': 'https://schema.org/OfflineEventAttendanceMode',
        'location': {
            '@type': 'Place',
            'name': 'Venue',
            'address': {'streetAddress': '1 Main St', 'addressLocality': 'DC', 'addressRegion': 'DC'},
        },
    }))
    result = fetch_json_ld_data('https://www.meetup.com/some-group/events/2/')
    assert result['is_virtual'] is False
    assert result['location'] == 'Venue, 1 Main St, DC, DC'


def test_unrelated_ld_types_are_still_ignored(monkeypatch):
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': 'Organization',
        'name': 'Meetup',
    }))
    result = fetch_json_ld_data('https://www.meetup.com/some-group/events/3/')
    assert result == {'title': None, 'is_virtual': False, 'location': None, 'cancelled': False}


@pytest.mark.parametrize('url', [
    'https://luma.com/1nqnrqug',
    'https://lu.ma/1nqnrqug',
])
def test_luma_online_event_is_detected(monkeypatch, url):
    # Luma's iCal LOCATION field is always the event's own luma.com/lu.ma URL
    # (never real venue text), so this JSON-LD fetch is the only way to catch
    # a Luma virtual event — see fetch_json_ld_data's docstring.
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': 'Event',
        'name': 'DC DAO Annual Meeting 2026',
        'eventAttendanceMode': 'https://schema.org/OnlineEventAttendanceMode',
        'location': {'@type': 'VirtualLocation', 'name': 'Online Event', 'url': url},
    }))
    result = fetch_json_ld_data(url)
    assert result['is_virtual'] is True
    assert result['title'] == 'DC DAO Annual Meeting 2026'


def test_luma_physical_event_is_not_forced_virtual(monkeypatch):
    _mock_get(monkeypatch, _ld_page({
        '@context': 'https://schema.org',
        '@type': 'Event',
        'name': 'Groundwork',
        'eventAttendanceMode': 'https://schema.org/OfflineEventAttendanceMode',
        'location': {
            '@type': 'Place',
            'name': 'National Union Building',
            'address': {'addressLocality': 'Washington', 'addressRegion': 'DC'},
        },
    }))
    result = fetch_json_ld_data('https://luma.com/groundwork_dc')
    assert result['is_virtual'] is False
    assert result['location'] == 'National Union Building, Washington, DC'


def test_non_meetup_non_luma_urls_still_skip_the_fetch(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('should not fetch for an unrelated domain')
    monkeypatch.setattr('calgen.calendars.safe_get', _boom)
    result = fetch_json_ld_data('https://www.eventbrite.com/e/some-event')
    assert result == {'title': None, 'is_virtual': False, 'location': None}
