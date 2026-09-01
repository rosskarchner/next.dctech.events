"""get_recently_added_count() — the homepage's one-line freshness signal
("N new events added this week"), replacing the old boxed just-added
preview. Counts events added within a rolling trailing 7-day window from
today (confirmed with the user as "this week", not a Monday-reset calendar
week), reusing group_by_added_date's own newest-first day buckets.
"""
import json

import pytest

from calgen.app import get_recently_added_count, local_tz
from datetime import datetime, timedelta


def _write_index(root, by_guid=None):
    d = root / '_data'
    d.mkdir(exist_ok=True)
    (d / 'added_at.json').write_text(json.dumps({'by_guid': by_guid or {}, 'by_title': {}}))


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _today():
    return datetime.now(local_tz).date()


def _days_ago(n):
    return (_today() - timedelta(days=n)).isoformat()


def _event(guid, date='2026-09-01'):
    return {'title': f'Event {guid}', 'date': date, 'time': '18:00', 'guid': guid}


def test_no_index_counts_zero(site, monkeypatch):
    # get_recently_added_count is imported from calgen.app (a re-export —
    # see that module's own docstring) but actually defined in and resolves
    # get_events from calgen.routes.listings, so that's what needs patching:
    # Python looks up a bare name via the function's *own* module globals.
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a')])
    assert get_recently_added_count() == 0


def test_counts_events_added_today(site, monkeypatch):
    _write_index(site, by_guid={'a': f'{_days_ago(0)}T10:00:00'})
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a')])
    assert get_recently_added_count() == 1


def test_counts_events_added_within_the_last_7_days_inclusive(site, monkeypatch):
    _write_index(site, by_guid={
        'a': f'{_days_ago(0)}T10:00:00',
        'b': f'{_days_ago(6)}T10:00:00',
    })
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a'), _event('b')])
    assert get_recently_added_count() == 2


def test_excludes_events_added_more_than_7_days_ago(site, monkeypatch):
    _write_index(site, by_guid={
        'a': f'{_days_ago(0)}T10:00:00',
        'old': f'{_days_ago(7)}T10:00:00',
    })
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a'), _event('old')])
    assert get_recently_added_count() == 1


def test_is_a_rolling_window_not_a_calendar_week(site, monkeypatch):
    """Confirmed with the user: "this week" means the trailing 7 days from
    today, not a Monday-reset calendar week — so a count taken on a Tuesday
    still reaches back across the previous week's Wednesday-through-Sunday.
    """
    _write_index(site, by_guid={'a': f'{_days_ago(5)}T10:00:00'})
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a')])
    assert get_recently_added_count() == 1


def test_multiple_events_added_the_same_day_all_count(site, monkeypatch):
    _write_index(site, by_guid={
        'a': f'{_days_ago(1)}T09:00:00',
        'b': f'{_days_ago(1)}T15:00:00',
        'c': f'{_days_ago(1)}T21:00:00',
    })
    monkeypatch.setattr(
        'calgen.routes.listings.get_events',
        lambda: [_event('a'), _event('b'), _event('c')])
    assert get_recently_added_count() == 3


def test_custom_window_days(site, monkeypatch):
    _write_index(site, by_guid={'a': f'{_days_ago(10)}T10:00:00'})
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda: [_event('a')])
    assert get_recently_added_count(window_days=7) == 0
    assert get_recently_added_count(window_days=14) == 1
