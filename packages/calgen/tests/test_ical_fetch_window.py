"""Which feed events the iCal fetch keeps.

The window used to open at "now", so an event dropped out of the cache at
the first aggregator run after it *started*, and the aggregator then deleted
it as if cancelled. It now opens at local midnight and judges each event by
when it ends: everything happening today stays all day, and a multi-day
event under way is kept. All-day DTEND is exclusive (RFC 5545) and is
stored as the real last day.
"""
from datetime import datetime, timedelta

import pytest

import calgen.calendars as cal

TODAY = datetime.now(cal.local_tz).date()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


def _dt(day, hh, mm=0):
    return f"{day.strftime('%Y%m%d')}T{hh:02d}{mm:02d}00"


def _vevent(uid, summary, start, end=None, all_day=False):
    p = ';VALUE=DATE' if all_day else ';TZID=America/New_York'
    lines = ['BEGIN:VEVENT', f'UID:{uid}', f'SUMMARY:{summary}', f'DTSTART{p}:{start}']
    if end:
        lines.append(f'DTEND{p}:{end}')
    return lines + ['END:VEVENT']


class _Resp:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.content = body.encode()


@pytest.fixture
def fetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cal, 'fetch_json_ld_data', lambda url: {})

    def run(*vevents):
        body = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//t//t//EN',
                            *[l for v in vevents for l in v], 'END:VCALENDAR'])
        monkeypatch.setattr(cal, 'safe_get', lambda *a, **k: _Resp(body))
        return {e['title']: e for e in cal.fetch_ical_and_extract_events(
            'https://example.com/feed.ics', 'grp', {'id': 'grp', 'name': 'G'})}
    return run


def test_an_event_from_earlier_today_is_kept_even_after_it_ended(fetch):
    # 00:01-00:02 today has ended by the time any real run happens.
    got = fetch(_vevent('a', 'Early', _dt(TODAY, 0, 1), _dt(TODAY, 0, 2)))
    assert 'Early' in got


def test_an_event_from_yesterday_is_dropped(fetch):
    got = fetch(_vevent('b', 'Yesterday', _dt(YESTERDAY, 10), _dt(YESTERDAY, 11)))
    assert got == {}


def test_a_multiday_event_under_way_is_kept(fetch):
    got = fetch(_vevent('c', 'Conference', _dt(YESTERDAY, 9), _dt(TOMORROW, 17)))
    assert got['Conference']['date'] == YESTERDAY.isoformat()
    assert got['Conference']['end_date'] == TOMORROW.isoformat()


def test_an_upcoming_event_is_kept(fetch):
    assert 'Later' in fetch(_vevent('d', 'Later', _dt(TOMORROW, 18), _dt(TOMORROW, 20)))


def test_a_one_day_all_day_event_gets_no_spurious_end_date(fetch):
    got = fetch(_vevent('e', 'All Day', TODAY.strftime('%Y%m%d'),
                        TOMORROW.strftime('%Y%m%d'), all_day=True))
    assert got['All Day']['date'] == TODAY.isoformat()
    assert got['All Day']['end_date'] == ''


def test_yesterdays_all_day_event_is_dropped_despite_its_exclusive_end(fetch):
    got = fetch(_vevent('f', 'Was Yesterday', YESTERDAY.strftime('%Y%m%d'),
                        TODAY.strftime('%Y%m%d'), all_day=True))
    assert got == {}


def test_a_two_day_all_day_event_ends_on_its_real_last_day(fetch):
    day_after = TOMORROW + timedelta(days=1)
    got = fetch(_vevent('g', 'Two Days', TODAY.strftime('%Y%m%d'),
                        day_after.strftime('%Y%m%d'), all_day=True))
    assert got['Two Days']['end_date'] == TOMORROW.isoformat()
