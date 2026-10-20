"""Tests for group category_rules in process_events."""
from datetime import date

from calgen.event_utils import event_categories
from calgen.pipeline import process_events


TODAY = date(2026, 7, 19)

_BASE_EVENT = {
    'date': '2026-07-25',
    'time': '18:00',
    'url': 'https://example.com/event',
}

GROUP = {'id': 'hac-dc', 'name': 'HacDC', 'active': True,
         'categories': ['hardware'],
         'category_rules': [{'phrase': 'anime night', 'categories': ['social']}]}


def _run(group, events):
    return process_events([group], {}, [], {'hac-dc': events}, [], today=TODAY)


def test_matching_rule_replaces_group_categories():
    result = _run(GROUP, [dict(_BASE_EVENT, title='Anime Night at HacDC')])
    assert result[0]['categories'] == ['social']


def test_unmatched_event_keeps_group_categories():
    result = _run(GROUP, [dict(_BASE_EVENT, title='Soldering 101')])
    assert result[0]['categories'] == ['hardware']


def test_rule_matches_feed_keywords():
    ev = dict(_BASE_EVENT, title='Friday', keywords=['Anime Night'])
    assert event_categories(ev, GROUP) == ['social']


def test_several_matching_rules_union_in_order():
    group = {**GROUP, 'category_rules': [
        {'phrase': 'anime', 'categories': ['social']},
        {'phrase': 'game', 'categories': ['gaming', 'social']},
    ]}
    ev = dict(_BASE_EVENT, title='Anime game jam')
    assert event_categories(ev, group) == ['social', 'gaming']


def test_malformed_rules_are_ignored():
    group = {**GROUP, 'category_rules': [None, {'phrase': ''}, {'categories': ['x']}]}
    assert event_categories(dict(_BASE_EVENT, title='Anime'), group) == ['hardware']
    assert event_categories({'title': 'x'}, {**GROUP, 'category_rules': True}) == ['hardware']
