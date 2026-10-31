"""The homepage/newsletter "next N days" window.

A multi-day event whose start date falls inside the window but whose end
date runs past it used to keep appearing on every day of its span, even
days a genuine same-day event would already have been filtered off of (the
TIX ON POSH bug: a Meetup listing with a bogus 10-day end time bled across
the whole homepage window while one-off events starting near the edge were
correctly excluded). filter_events_to_upcoming_days decides what gets in;
prepare_events_by_day's window_end clips how far a multi-day event's
expansion is allowed to run once it's in.
"""
from datetime import date, timedelta

from calgen.app import filter_events_to_upcoming_days, prepare_events_by_day

TODAY = date.today()
WINDOW_END = TODAY + timedelta(days=21)


def _event(**kw):
    base = {'date': TODAY.isoformat(), 'time': '18:00', 'title': 'Event',
            'guid': 'guid1234deadbeef'}
    base.update(kw)
    return base


class TestFilterEventsToUpcomingDays:
    def test_keeps_an_event_starting_inside_the_window(self):
        e = _event(date=(TODAY + timedelta(days=20)).isoformat())
        assert filter_events_to_upcoming_days([e], WINDOW_END) == [e]

    def test_drops_an_event_starting_after_the_window(self):
        e = _event(date=(TODAY + timedelta(days=25)).isoformat())
        assert filter_events_to_upcoming_days([e], WINDOW_END) == []

    def test_drops_an_event_that_already_happened(self):
        e = _event(date=(TODAY - timedelta(days=1)).isoformat())
        assert filter_events_to_upcoming_days([e], WINDOW_END) == []

    def test_a_multiday_event_running_past_the_window_is_kept(self):
        # prepare_events_by_day is what keeps its expansion from running
        # past window_end.
        e = _event(end_date=(TODAY + timedelta(days=40)).isoformat())
        assert filter_events_to_upcoming_days([e], WINDOW_END) == [e]


class TestPrepareEventsByDayWindowClip:
    def test_a_multiday_event_is_clipped_to_window_end(self):
        e = _event(end_date=(TODAY + timedelta(days=40)).isoformat())
        days = prepare_events_by_day([e], window_end=WINDOW_END)
        assert days[-1]['date'] == WINDOW_END.isoformat()
        assert all(d['date'] <= WINDOW_END.isoformat() for d in days)

    def test_without_window_end_a_multiday_event_still_expands_in_full(self):
        """The week/month pages pass no window_end and must keep seeing every day."""
        end = TODAY + timedelta(days=40)
        e = _event(end_date=end.isoformat())
        days = prepare_events_by_day([e])
        assert days[-1]['date'] == end.isoformat()

    def test_a_single_day_event_is_unaffected_by_window_end(self):
        e = _event()
        days = prepare_events_by_day([e], window_end=WINDOW_END)
        assert [d['date'] for d in days] == [TODAY.isoformat()]


# ── Events already under way (next_dctech_events-ax3: BSidesNoVA, a
#    two-day conference, vanished from every page on its second day) ──

import json  # noqa: E402

import pytest  # noqa: E402

from calgen.routes.common import event_last_day, get_events, local_today  # noqa: E402

LOCAL_TODAY = local_today()
YESTERDAY = (LOCAL_TODAY - timedelta(days=1)).isoformat()
LOCAL_WINDOW_END = LOCAL_TODAY + timedelta(days=21)


class TestEventLastDay:
    def test_is_end_date_when_present(self):
        assert event_last_day({'date': '2026-10-30', 'end_date': '2026-10-31'}) == '2026-10-31'

    def test_falls_back_to_date(self):
        assert event_last_day({'date': '2026-10-30'}) == '2026-10-30'

    def test_ignores_an_end_date_before_the_start(self):
        assert event_last_day({'date': '2026-10-30', 'end_date': '2026-10-01'}) == '2026-10-30'

    def test_ignores_a_malformed_end_date(self):
        assert event_last_day({'date': '2026-10-30', 'end_date': 'soon'}) == '2026-10-30'


class TestOngoingEvents:
    def test_filter_keeps_an_event_that_started_yesterday_and_ends_today(self):
        e = _event(date=YESTERDAY, end_date=LOCAL_TODAY.isoformat())
        assert filter_events_to_upcoming_days([e], LOCAL_WINDOW_END) == [e]

    def test_filter_drops_a_multiday_event_that_ended_yesterday(self):
        e = _event(date=(LOCAL_TODAY - timedelta(days=3)).isoformat(), end_date=YESTERDAY)
        assert filter_events_to_upcoming_days([e], LOCAL_WINDOW_END) == []

    def test_window_start_clips_past_days_and_labels_today_continuing(self):
        e = _event(date=YESTERDAY, end_date=LOCAL_TODAY.isoformat(), title='BSides')
        days = prepare_events_by_day([e], window_end=LOCAL_WINDOW_END,
                                     window_start=LOCAL_TODAY)
        assert [d['date'] for d in days] == [LOCAL_TODAY.isoformat()]
        shown = days[0]['time_slots'][0]['events'][0]
        assert shown['display_title'] == 'BSides (continuing)'

    def test_without_window_start_every_day_is_shown(self):
        """Week and month pages keep the whole span, past days included."""
        e = _event(date=YESTERDAY, end_date=LOCAL_TODAY.isoformat(), title='BSides')
        days = prepare_events_by_day([e])
        assert [d['date'] for d in days] == [YESTERDAY, LOCAL_TODAY.isoformat()]
        assert days[0]['time_slots'][0]['events'][0]['display_title'] == 'BSides'

    def test_window_start_leaves_an_event_starting_later_alone(self):
        start = LOCAL_TODAY + timedelta(days=2)
        e = _event(date=start.isoformat(), end_date=(start + timedelta(days=1)).isoformat())
        days = prepare_events_by_day([e], window_start=LOCAL_TODAY)
        assert days[0]['date'] == start.isoformat()


@pytest.fixture
def site_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('calgen.routes.common.get_approved_groups', lambda: [])

    def write(events):
        (tmp_path / '_data').mkdir(exist_ok=True)
        (tmp_path / '_data' / 'all_events.json').write_text(json.dumps(events))
    return write


def test_get_events_keeps_an_event_on_its_last_day(site_events):
    ongoing = _event(guid='ongoing', date=YESTERDAY, end_date=LOCAL_TODAY.isoformat())
    over = _event(guid='over', date=YESTERDAY)
    site_events([ongoing, over])
    assert [e['guid'] for e in get_events()] == ['ongoing']
