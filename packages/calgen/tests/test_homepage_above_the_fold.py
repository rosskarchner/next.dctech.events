"""Homepage above-the-fold redesign (next_dctech_events-czy): only one event
was visible without scrolling, worse on narrow screens, because the full-size
h1, a tall #subscribe box, and a boxed "just added" preview all stacked above
any events. This shrinks the h1 to a subheading, collapses "just added" into
one line, and moves #subscribe down so it sits between the first two
day-groups and the rest — not before all events.

Run: python -m pytest test_homepage_above_the_fold.py
"""
from pathlib import Path

import pytest

from calgen.app import create_app

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'


def _day(date, short_date, title):
    return {
        'date': date,
        'short_date': short_date,
        'week_url': None,
        'has_events': True,
        'time_slots': [{
            'events': [{
                'title': title, 'url': 'https://example.com/e',
                'display_title': title, 'formatted_time': '6:00 PM',
                'time': '18:00', 'permalink': None, 'is_recurring': False,
                'location': '', 'group': '', 'also_published_by': [],
                'categories': [],
            }],
        }],
    }


_DAYS = [
    _day('2026-09-01', 'Tue Sep 1', 'Day 1 Event'),
    _day('2026-09-02', 'Wed Sep 2', 'Day 2 Event'),
    _day('2026-09-03', 'Thu Sep 3', 'Day 3 Event'),
]


@pytest.fixture
def client(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    # homepage() lives in calgen.routes.listings (moved out of calgen.app —
    # see that module's own docstring), so that's where its calls to these
    # names actually resolve from: Python looks up a bare name via the
    # function's *own* module globals, not through calgen.app's re-export.
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda *a, **k: [])
    monkeypatch.setattr(
        'calgen.routes.listings.filter_in_person_events', lambda events: events)
    monkeypatch.setattr(
        'calgen.routes.listings.filter_events_to_upcoming_days', lambda events, window_end: events)
    monkeypatch.setattr(
        'calgen.routes.listings.prepare_events_by_day', lambda *a, **k: list(_DAYS))
    return app


def _get(app):
    return app.test_client().get('/')


def test_h1_is_styled_as_a_subheading_not_removed(client):
    """Kept as <h1> for SEO/a11y — only the styling changes."""
    html = _get(client).get_data(as_text=True)
    assert '<h1 class="page-subheading">Upcoming Tech Events' in html


def test_freshness_line_shown_and_links_to_just_added(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 3)
    html = _get(client).get_data(as_text=True)
    assert 'recently-added-line' in html
    assert '3 new events added this week' in html
    assert 'href="/just-added/"' in html


def test_freshness_line_uses_singular_for_one_event(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 1)
    html = _get(client).get_data(as_text=True)
    assert '1 new event added this week' in html
    assert '1 new events' not in html


def test_freshness_line_hidden_when_count_is_zero(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 0)
    html = _get(client).get_data(as_text=True)
    assert 'recently-added-line' not in html


def test_old_boxed_just_added_preview_is_gone(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 3)
    html = _get(client).get_data(as_text=True)
    assert 'just-added-preview' not in html


def test_subscribe_box_sits_after_the_first_two_day_groups(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 0)
    html = _get(client).get_data(as_text=True)
    # The JSON-LD ItemList in <head> deliberately lists every day
    # (unaffected by the {% with days=... %} body slicing — see the design
    # doc), including "Day 3 Event" text, ahead of the body content this
    # test cares about. Restrict to the body so that doesn't confuse the
    # ordering check below.
    body = html[html.index('class="home-container"'):]

    day1 = body.index('Day 1 Event')
    day2 = body.index('Day 2 Event')
    day3 = body.index('Day 3 Event')
    subscribe = body.index('id="subscribe"')

    assert day1 < day2 < subscribe < day3, (
        'expected: day 1, day 2, #subscribe, day 3 (and beyond) — '
        'subscribe must follow the first two day-groups, not precede all events')


def test_with_two_or_fewer_days_the_second_slice_include_is_skipped(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_recently_added_count', lambda: 0)
    monkeypatch.setattr(
        'calgen.routes.listings.prepare_events_by_day', lambda *a, **k: list(_DAYS[:2]))
    html = _get(client).get_data(as_text=True)

    # The partial's own "no events" fallback must not fire for the second,
    # now-empty slice.
    assert 'No events found for this period' not in html
