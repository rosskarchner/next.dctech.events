"""Tests for group skip_phrases filtering in process_events."""
from datetime import date
import pytest

from calgen.pipeline import process_events


TODAY = date(2026, 7, 19)

_BASE_EVENT = {
    'date': '2026-07-25',
    'time': '18:00',
    'url': 'https://example.com/event',
}


def _make_group(skip_phrases=None, **kwargs):
    group = {'id': 'testgroup', 'name': 'Test Group', 'active': True}
    if skip_phrases is not None:
        group['skip_phrases'] = skip_phrases
    group.update(kwargs)
    return group


def _run(group, events):
    ical_events = {'testgroup': events}
    return process_events([group], {}, [], ical_events, [], today=TODAY)


def test_no_skip_phrases_includes_all_events():
    group = _make_group()
    events = [dict(_BASE_EVENT, title='Regular Meetup')]
    result = _run(group, events)
    assert len(result) == 1


def test_skip_phrase_in_title_excludes_event():
    group = _make_group(skip_phrases=['cancelled'])
    events = [dict(_BASE_EVENT, title='Meetup CANCELLED')]
    result = _run(group, events)
    assert result == []


def test_skip_phrase_not_in_title_includes_event():
    group = _make_group(skip_phrases=['cancelled'])
    events = [dict(_BASE_EVENT, title='Regular Meetup')]
    result = _run(group, events)
    assert len(result) == 1


def test_skip_phrase_case_insensitive():
    group = _make_group(skip_phrases=['Cancelled'])
    events = [dict(_BASE_EVENT, title='meetup cancelled')]
    result = _run(group, events)
    assert result == []


def test_skip_phrase_partial_match_in_title():
    group = _make_group(skip_phrases=['social'])
    events = [
        dict(_BASE_EVENT, title='Social Networking Night', url='https://example.com/1'),
        dict(_BASE_EVENT, title='Tech Talk', url='https://example.com/2'),
    ]
    result = _run(group, events)
    assert len(result) == 1
    assert result[0]['title'] == 'Tech Talk'


def test_skip_phrase_in_keywords_excludes_event():
    group = _make_group(skip_phrases=['webinar'])
    events = [dict(_BASE_EVENT, title='Monthly Meetup', keywords=['webinar', 'online'])]
    result = _run(group, events)
    assert result == []


def test_skip_phrase_not_in_keywords_includes_event():
    group = _make_group(skip_phrases=['webinar'])
    events = [dict(_BASE_EVENT, title='Monthly Meetup', keywords=['in-person'])]
    result = _run(group, events)
    assert len(result) == 1


def test_multiple_skip_phrases_any_match_excludes():
    group = _make_group(skip_phrases=['cancelled', 'postponed'])
    events = [
        dict(_BASE_EVENT, title='Event Postponed', url='https://example.com/1'),
        dict(_BASE_EVENT, title='Normal Event', url='https://example.com/2'),
    ]
    result = _run(group, events)
    assert len(result) == 1
    assert result[0]['title'] == 'Normal Event'


def test_empty_skip_phrases_includes_all():
    group = _make_group(skip_phrases=[])
    events = [dict(_BASE_EVENT, title='Any Event')]
    result = _run(group, events)
    assert len(result) == 1


def test_skip_phrases_none_includes_all():
    group = _make_group(skip_phrases=None)
    events = [dict(_BASE_EVENT, title='Any Event')]
    result = _run(group, events)
    assert len(result) == 1


def test_skip_phrases_only_applies_to_owning_group():
    group1 = {'id': 'group1', 'name': 'Group 1', 'active': True, 'skip_phrases': ['cancelled']}
    group2 = {'id': 'group2', 'name': 'Group 2', 'active': True}
    ical_events = {
        'group1': [dict(_BASE_EVENT, title='Meetup Cancelled')],
        'group2': [dict(_BASE_EVENT, title='Meetup Cancelled', url='https://example.com/g2')],
    }
    result = process_events([group1, group2], {}, [], ical_events, [], today=TODAY)
    # group1 event skipped, group2 event kept
    assert len(result) == 1
    assert result[0]['group'] == 'Group 2'
