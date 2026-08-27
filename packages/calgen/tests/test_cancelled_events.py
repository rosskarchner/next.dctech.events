"""Detecting a cancelled event before it reaches the calendar.

Until now the only signal was schema.org `eventStatus: EventCancelled`, scraped
from the event page — which needs the event to have a URL, the page to be
reachable, and JSON-LD to be on it. Three ways to miss a cancellation, and the
result is a listing that sends someone to a room nobody is in
(next_dctech_events-kqt).
"""
import icalendar
import pytest

from calgen.calendars import ical_says_cancelled, title_says_cancelled


def _vevent(**props):
    event = icalendar.Event()
    for key, value in props.items():
        event.add(key, value)
    return event


# ── The iCal standard's own answer ─────────────────────────────────


def test_status_cancelled_is_detected():
    assert ical_says_cancelled(_vevent(summary='Talk', status='CANCELLED'))


def test_status_is_case_insensitive():
    assert ical_says_cancelled(_vevent(summary='Talk', status='cancelled'))


@pytest.mark.parametrize('status', ['CONFIRMED', 'TENTATIVE'])
def test_the_other_standard_statuses_are_not_cancellations(status):
    assert not ical_says_cancelled(_vevent(summary='Talk', status=status))


def test_no_status_property_at_all():
    # Most feeds never set it; absence must not read as cancelled.
    assert not ical_says_cancelled(_vevent(summary='Talk'))


# ── Decorated markers in the title ─────────────────────────────────
# Only decorated forms. A bare substring match would drop a talk *about*
# cancellation, which is a worse error than missing a marker.


@pytest.mark.parametrize('title', [
    'AFCEA Belvoir August 2026 Luncheon *** Cancelled ***',
    'Monthly Meetup ***CANCELLED***',
    'Monthly Meetup [CANCELLED]',
    'Monthly Meetup (canceled)',
    'CANCELLED: Monthly Meetup',
    'Cancelled - Monthly Meetup',
    'Monthly Meetup - CANCELLED',
    'Monthly Meetup — Cancelled',
])
def test_a_decorated_marker_is_detected(title):
    assert title_says_cancelled(title), title


@pytest.mark.parametrize('title', [
    'How to Cancel Your Cloud Subscription',
    'Cancellation Policy Workshop',
    'Building Noise-Cancelling Headphones',
    'A Talk About Cancelled Projects',
    'Cancel Culture and Tech',
    'Monthly Meetup',
    '',
])
def test_an_undecorated_mention_is_left_alone(title):
    assert not title_says_cancelled(title), title


def test_both_american_and_british_spellings():
    assert title_says_cancelled('Meetup [CANCELED]')
    assert title_says_cancelled('Meetup [CANCELLED]')


def test_none_and_non_strings_do_not_raise():
    assert not title_says_cancelled(None)
    assert not title_says_cancelled(12345)
