"""Reading frozen week captures, and merging them with live event data.

The merge is the part worth pinning: for a week or month that is partly past
and partly ahead, both sources are correct about different days, and getting
it wrong shows up as either duplicated listings or vanished ones.
"""
import os

import pytest
import yaml

from calgen.archive import (
    get_archived_event_count, get_archived_events, get_archived_week,
    get_archived_weeks, merge_events,
)


def _write_week(root, week_id, week_start, week_end, events):
    d = root / '_archive'
    d.mkdir(exist_ok=True)
    (d / f'{week_id}.yaml').write_text(yaml.safe_dump({
        'week_id': week_id,
        'week_start': week_start,
        'week_end': week_end,
        'captured_at': f'{week_start}T11:00:00Z',
        'event_count': len(events),
        'events': events,
    }, sort_keys=False))


def _event(title='Meetup', date='2026-07-21', time='18:30', url=None):
    return {'title': title, 'date': date, 'time': time,
            'url': url or f'https://example.org/{title}'.replace(' ', '-')}


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestReading:
    def test_no_archive_dir_is_empty_not_an_error(self, site):
        assert get_archived_weeks() == {}
        assert get_archived_events() == []
        assert get_archived_week('2026-W30') is None

    def test_loads_a_week(self, site):
        _write_week(site, '2026-W30', '2026-07-20', '2026-07-26',
                    [_event('Python Meetup'), _event('Rust Meetup')])
        week = get_archived_week('2026-W30')
        assert week['event_count'] == 2
        assert week['week_start'] == '2026-07-20'
        assert [e['title'] for e in week['events']] == ['Python Meetup', 'Rust Meetup']

    def test_events_are_marked_archived(self, site):
        """prepare_events_by_day keys the permalink decision off this flag.

        Per-event pages are built only for current events, so an archived
        listing that carried a permalink would link to a page that was never
        frozen.
        """
        _write_week(site, '2026-W30', '2026-07-20', '2026-07-26', [_event()])
        assert all(e['archived'] for e in get_archived_week('2026-W30')['events'])

    def test_malformed_yaml_is_skipped_not_fatal(self, site):
        d = site / '_archive'
        d.mkdir()
        (d / 'broken.yaml').write_text('{[not yaml')
        (d / 'no-week-id.yaml').write_text(yaml.safe_dump({'events': [_event()]}))
        _write_week(site, '2026-W31', '2026-07-27', '2026-08-02', [_event()])
        assert list(get_archived_weeks()) == ['2026-W31']

    def test_events_span_all_weeks(self, site):
        _write_week(site, '2026-W30', '2026-07-20', '2026-07-26', [_event('A')])
        _write_week(site, '2026-W31', '2026-07-27', '2026-08-02', [_event('B'), _event('C')])
        assert sorted(e['title'] for e in get_archived_events()) == ['A', 'B', 'C']
        assert get_archived_event_count() == 3


class TestMerge:
    def test_live_only(self):
        live = [_event('A'), _event('B')]
        assert merge_events(live, []) == live

    def test_archive_only(self):
        archived = [_event('A')]
        assert merge_events([], archived) == archived

    def test_same_event_from_both_sides_appears_once(self):
        """The current week overlaps: its past days come from the capture,
        its remaining days from live data, and the capture also still lists
        the days ahead."""
        shared = _event('Shared', date='2026-08-14')
        merged = merge_events([dict(shared)], [dict(shared, archived=True)])
        assert len(merged) == 1

    def test_live_wins_on_conflict(self):
        """A capture can be up to a week stale; live data is current."""
        live = dict(_event('Meetup'), location='New Room 200')
        archived = dict(_event('Meetup'), location='Old Room 100', archived=True)
        merged = merge_events([live], [archived])
        assert len(merged) == 1
        assert merged[0]['location'] == 'New Room 200'
        assert 'archived' not in merged[0]

    def test_events_differing_only_by_time_are_distinct(self):
        a = _event('Standup', time='09:00')
        b = _event('Standup', time='17:00')
        assert len(merge_events([a], [b])) == 2

    def test_same_title_and_date_different_url_are_distinct(self):
        a = _event('Monthly Meetup', url='https://example.org/a')
        b = _event('Monthly Meetup', url='https://example.org/b')
        assert len(merge_events([a], [b])) == 2

    def test_archived_extras_are_appended_not_dropped(self):
        live = [_event('Upcoming', date='2026-08-20')]
        archived = [_event('Past', date='2026-08-02')]
        merged = merge_events(live, archived)
        assert sorted(e['title'] for e in merged) == ['Past', 'Upcoming']
