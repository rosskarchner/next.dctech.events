"""process_events keeps a multi-day event through its last day.

next_dctech_events-ax3: BSidesNoVA (Oct 30-31) was dropped by the pipeline on
Oct 31 because only its start date was compared to today, so it vanished
from every page on its second day.
"""
from datetime import date

from calgen.pipeline import process_events

TODAY = date(2026, 10, 31)


def _single(**kw):
    base = {'guid': 'd0e7fbfd', 'title': 'BSidesNoVA', 'date': '2026-10-30',
            'url': 'https://example.com/bsides', 'location': 'Arlington, VA'}
    base.update(kw)
    return base


def _ical(**kw):
    base = {'title': 'Two-day workshop', 'date': '2026-10-30', 'time': '09:00',
            'url': 'https://example.com/workshop'}
    base.update(kw)
    return base


GROUP = {'id': 'g', 'name': 'Group', 'active': True}


def _run(single=(), ical=()):
    return process_events([GROUP], {}, list(single), {'g': list(ical)}, [],
                          today=TODAY)


def test_a_submitted_event_on_its_last_day_is_kept():
    assert [e['guid'] for e in _run(single=[_single(end_date='2026-10-31')])] == ['d0e7fbfd']


def test_a_submitted_event_that_ended_yesterday_is_dropped():
    assert _run(single=[_single(date='2026-10-29', end_date='2026-10-30')]) == []


def test_a_single_day_event_from_yesterday_is_still_dropped():
    assert _run(single=[_single()]) == []


def test_an_ical_event_under_way_is_kept():
    result = _run(ical=[_ical(end_date='2026-11-01')])
    assert [e['title'] for e in result] == ['Two-day workshop']


def test_an_ical_event_from_yesterday_is_still_dropped():
    assert _run(ical=[_ical()]) == []
