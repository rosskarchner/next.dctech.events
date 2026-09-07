"""Events stored with time '00:00' must render as "All Day", not "12:00 am".

db.py's put_event/update_event fall back to the literal string '00:00' when
an event has no time (so GSI1SK/GSI5SK sort keys always have a TIME#
component) — see cdk_next/lambda_src/api/db.py. Every display path that
parses event['time'] as a clock time has to recognize that sentinel as
"no time", or a genuinely all-day event (e.g. "Billington CyberSecurity
Summit (17th Annual)") shows up looking like it starts at midnight.
"""
from calgen.app import prepare_events_by_day
from calgen.event_utils import has_specific_time
from calgen.routes.events import _format_event_time


def _event(**kw):
    base = {'date': '2026-09-10', 'time': '00:00', 'title': 'All-Day Event',
            'guid': 'guid1234deadbeef'}
    base.update(kw)
    return base


class TestHasSpecificTime:
    def test_rejects_the_all_day_sentinel(self):
        assert has_specific_time('00:00') is False

    def test_rejects_empty_and_missing(self):
        assert has_specific_time('') is False
        assert has_specific_time(None) is False

    def test_accepts_a_real_time(self):
        assert has_specific_time('18:00') is True


class TestPrepareEventsByDay:
    def test_00_00_groups_under_all_day_not_midnight(self):
        days = prepare_events_by_day([_event()])
        [day] = days
        [slot] = day['time_slots']
        assert slot['time'] == 'All Day'
        assert slot['events'][0]['formatted_time'] == 'All Day'


class TestFormatEventTime:
    def test_00_00_formats_as_blank_not_midnight(self):
        assert _format_event_time(_event()) == ''
