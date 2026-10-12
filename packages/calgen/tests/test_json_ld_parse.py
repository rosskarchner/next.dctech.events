"""parse_event_json_ld, the parser shared with the MCP server's re-check.

The case that prompted it: the Washington Quantum Computing Meetup event read as
in-person when first fetched, later switched to online, and the aggregator's
JSON-LD cache never looked again (next_dctech_events-ba5). Meetup's page now
carries the shape below.
"""
import json

from calgen.calendars import fetch_json_ld_data
from calgen.json_ld import cache_entry, is_json_ld_host, parse_event_json_ld


def _page(*objects):
    scripts = ''.join(
        f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objects)
    return f'<html><head>{scripts}</head></html>'


MEETUP_ONLINE = {
    '@context': 'https://schema.org',
    '@type': 'Event',
    'name': 'High-Rate and Computationally-Efficient Seedless Extractors',
    'startDate': '2026-10-12T10:00:00-04:00',
    'endDate': '2026-10-12T12:00:00-04:00',
    'eventStatus': 'https://schema.org/EventScheduled',
    'eventAttendanceMode': 'https://schema.org/OnlineEventAttendanceMode',
    'location': {'@type': 'VirtualLocation',
                 'url': 'https://www.meetup.com/washington-quantum-computing-meetup/events/316452554/'},
}


def test_meetup_online_event():
    parsed = parse_event_json_ld(_page({'@type': 'Organization', 'name': 'Meetup'}, MEETUP_ONLINE))
    assert parsed['is_virtual'] is True
    assert parsed['cancelled'] is False
    assert parsed['event_status'] == 'EventScheduled'
    assert parsed['attendance_mode'] == 'OnlineEventAttendanceMode'
    assert parsed['start_date'] == '2026-10-12T10:00:00-04:00'
    assert parsed['location'] is None


def test_in_person_event_location():
    parsed = parse_event_json_ld(_page({
        '@type': 'EducationEvent', 'name': 'Talk',
        'eventAttendanceMode': 'https://schema.org/OfflineEventAttendanceMode',
        'location': {'@type': 'Place', 'name': 'MLK Library',
                     'address': {'streetAddress': '901 G St NW',
                                 'addressLocality': 'Washington', 'addressRegion': 'DC'}},
    }))
    assert parsed['is_virtual'] is False
    assert parsed['location'] == 'MLK Library, 901 G St NW, Washington, DC'


def test_cancelled_and_postponed_status():
    cancelled = parse_event_json_ld(_page({
        **MEETUP_ONLINE, 'eventStatus': 'https://schema.org/EventCancelled'}))
    assert cancelled['cancelled'] is True
    postponed = parse_event_json_ld(_page({
        **MEETUP_ONLINE, 'eventStatus': 'https://schema.org/EventPostponed'}))
    assert postponed['cancelled'] is False
    assert postponed['event_status'] == 'EventPostponed'


def test_no_event_and_bad_json_return_none():
    html = ('<script type="application/ld+json">{not json</script>'
            + _page({'@type': 'BreadcrumbList'}))
    assert parse_event_json_ld(html) is None


def test_cache_entry_shape():
    entry = cache_entry(parse_event_json_ld(_page(MEETUP_ONLINE)))
    assert set(entry) == {'title', 'is_virtual', 'location', 'cancelled', 'fetched_at'}
    assert entry['is_virtual'] is True
    assert cache_entry(None)['is_virtual'] is False


def test_hosts():
    assert is_json_ld_host('https://lu.ma/x')
    assert not is_json_ld_host('https://www.eventbrite.com/e/1')
    assert not is_json_ld_host(None)


def test_fetch_stamps_fetched_at(tmp_path, monkeypatch):
    monkeypatch.setattr('calgen.calendars.JSON_LD_CACHE_DIR', str(tmp_path))

    class _Resp:
        status_code = 200
        text = _page(MEETUP_ONLINE)
    monkeypatch.setattr('calgen.calendars.safe_get', lambda *a, **k: _Resp())
    result = fetch_json_ld_data('https://www.meetup.com/g/events/1/')
    assert result['is_virtual'] is True
    assert result['fetched_at'].endswith('Z')
