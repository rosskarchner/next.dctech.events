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

    def test_a_multiday_event_passes_on_its_start_date_alone(self):
        # end_date is irrelevant to this filter by design — prepare_events_by_day
        # is what keeps its expansion from running past window_end.
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
