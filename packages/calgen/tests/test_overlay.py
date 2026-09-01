"""Tests for applying an overlay at render time.

An overlay is what a moderator writes in /edit and what the weekly QC agent
writes on its Monday pass. Two things here had no coverage at all before: that
the merge happens for iCal events, and that it happens for the other sources —
it did not, so an overlay on a submitted event was exported and then silently
ignored.
"""
from datetime import date

import pytest

from calgen.overlay import OVERLAY_PROTECTED_FIELDS, apply_overlay
from calgen.regions import EventRejected
from calgen.pipeline import process_events

TODAY = date(2026, 7, 19)

_BASE = {
    'date': '2026-07-25',
    'time': '18:00',
    'url': 'https://example.com/event',
    'title': 'Feed Title',
}

GROUP = {'id': 'testgroup', 'name': 'Test Group', 'active': True,
         'website': 'https://example.com', 'categories': ['ai']}


def _run(ical=None, single=None, recurring=None, overrides=None,
        recurring_overrides=None):
    return process_events(
        [GROUP], {}, list(single or []), {'testgroup': list(ical or [])},
        list(recurring or []), today=TODAY, event_overrides=overrides or {},
        recurring_overrides=recurring_overrides or {},
    )


def _guid_of(event):
    return event['guid']


def _only(result):
    assert len(result) == 1, [e.get('title') for e in result]
    return result[0]


# ── apply_overlay in isolation ─────────────────────────────────────


@pytest.mark.parametrize('field', sorted(OVERLAY_PROTECTED_FIELDS))
def test_a_protected_field_is_never_merged(field):
    event = {field: 'original', 'title': 'x'}
    apply_overlay(event, {field: 'hijacked'})
    assert event[field] == 'original'


@pytest.mark.parametrize('key', ['_comment', '_qa_run', '_rev', '_edited_by',
                                 '_edited_at', '_field_edits'])
def test_private_bookkeeping_is_never_merged(key):
    # The exporter strips these, but it and this module deploy independently,
    # so a stale exporter must not be able to paint _qa_run onto an event.
    event = {'title': 'x'}
    apply_overlay(event, {key: 'junk'})
    assert key not in event


def test_an_editable_field_is_merged():
    event = {'title': 'Feed Title'}
    assert apply_overlay(event, {'title': 'Corrected'})['title'] == 'Corrected'


def test_no_overlay_is_a_no_op():
    assert apply_overlay({'title': 'x'}, None) == {'title': 'x'}
    assert apply_overlay({'title': 'x'}, {}) == {'title': 'x'}


# ── iCal events — the path that existed and was untested ───────────


def test_an_overlay_corrects_an_ical_event():
    guid = _guid_of(_only(_run(ical=[dict(_BASE)])))
    result = _only(_run(ical=[dict(_BASE)],
                        overrides={guid: {'title': 'Corrected Title'}}))
    assert result['title'] == 'Corrected Title'


def test_an_overlay_replaces_ical_categories():
    guid = _guid_of(_only(_run(ical=[dict(_BASE)])))
    result = _only(_run(ical=[dict(_BASE)],
                        overrides={guid: {'categories': ['cloud']}}))
    assert result['categories'] == ['cloud']


def test_an_overlay_cannot_reassign_an_ical_events_group():
    guid = _guid_of(_only(_run(ical=[dict(_BASE)])))
    result = _only(_run(ical=[dict(_BASE)],
                        overrides={guid: {'group': 'Somebody Else'}}))
    assert result['group'] == 'Test Group'


# ── Submitted and manual events — this used to do nothing ──────────


def test_an_overlay_corrects_a_single_event():
    single = [dict(_BASE, guid='abc12345', title='Submitted Title')]
    result = _only(_run(single=single,
                        overrides={'abc12345': {'title': 'Corrected'}}))
    assert result['title'] == 'Corrected'


def test_an_overlay_hides_a_single_event():
    single = [dict(_BASE, guid='abc12345')]
    result = _only(_run(single=single,
                        overrides={'abc12345': {'hidden': True}}))
    # Present in the output — get_events(include_hidden=True) is a real
    # caller — but flagged for the filter that app.get_events applies.
    assert result['hidden'] is True


def test_an_overlay_on_a_single_event_can_set_a_location():
    single = [dict(_BASE, guid='abc12345')]
    result = _only(_run(single=single,
                        overrides={'abc12345': {'location': 'Arlington, VA'}}))
    assert result['location'] == 'Arlington, VA'


# ── Recurring occurrences ──────────────────────────────────────────


def _recurring():
    return [{
        'id': 'weekly-thing', 'title': 'Weekly Thing',
        'rrule': 'FREQ=WEEKLY;BYDAY=MO', 'time': '18:00',
        'url': 'https://example.com/weekly', 'date': '2026-07-20',
        'group': 'Test Group', 'categories': [],
    }]


def test_a_recurring_instance_override_applies_to_one_occurrence_only():
    # Occurrence identity for an override is (series id, date) — never the
    # occurrence's guid, which is recomputed fresh every build and so is not
    # a safe key (see db.create_correction's docstring for why).
    occurrences = _run(recurring=_recurring())
    assert len(occurrences) > 1
    target = occurrences[0]
    result = _run(recurring=_recurring(), recurring_overrides={
        'weekly-thing': {target['date']: {'title': 'Just This One'}}})
    titles = {e['date']: e['title'] for e in result}
    assert titles[target['date']] == 'Just This One'
    for occ_date, title in titles.items():
        if occ_date != target['date']:
            assert title == 'Weekly Thing'


def test_a_recurring_series_level_edit_applies_to_every_occurrence():
    # Series-level corrections need no override plumbing at all: they're
    # merged directly onto the recurring definition before process_events
    # ever runs (db.merge_recurring_event_fields), so every occurrence
    # picks the new value up via its own dict(event) copy.
    edited = [dict(_recurring()[0], title='Corrected For Everyone')]
    result = _run(recurring=edited)
    assert len(result) > 1
    assert all(e['title'] == 'Corrected For Everyone' for e in result)


def test_an_instance_override_wins_over_the_series_value_for_its_date():
    occurrences = _run(recurring=_recurring())
    target = occurrences[0]
    edited = [dict(_recurring()[0], title='Series Says This')]
    result = _run(recurring=edited, recurring_overrides={
        'weekly-thing': {target['date']: {'title': 'This Date Says Otherwise'}}})
    titles = {e['date']: e['title'] for e in result}
    assert titles[target['date']] == 'This Date Says Otherwise'
    for occ_date, title in titles.items():
        if occ_date != target['date']:
            assert title == 'Series Says This'


def test_an_instance_override_for_a_date_outside_the_window_is_harmless():
    # A correction can be approved against a date that's aged out of the
    # expansion window; it just has nothing to apply to until (if ever) that
    # date is back in window on a future build.
    result = _run(recurring=_recurring(), recurring_overrides={
        'weekly-thing': {'2020-01-01': {'title': 'Long Gone'}}})
    assert all(e['title'] != 'Long Gone' for e in result)


@pytest.mark.parametrize('field', sorted(OVERLAY_PROTECTED_FIELDS))
def test_a_protected_field_is_never_merged_into_a_recurring_instance(field):
    occurrences = _run(recurring=_recurring())
    target = occurrences[0]
    original = target.get(field)
    result = _run(recurring=_recurring(), recurring_overrides={
        'weekly-thing': {target['date']: {field: 'hijacked'}}})
    matched = next(e for e in result if e['date'] == target['date'])
    assert matched.get(field) == original


# ── Identity is source-derived, presentation is overlay-derived ────


def test_overriding_the_title_does_not_change_the_guid():
    guid = _guid_of(_only(_run(ical=[dict(_BASE)])))
    result = _only(_run(ical=[dict(_BASE)],
                        overrides={guid: {'title': 'Something Else'}}))
    assert result['guid'] == guid


def test_overriding_the_time_does_not_change_the_guid():
    guid = _guid_of(_only(_run(ical=[dict(_BASE)])))
    result = _only(_run(ical=[dict(_BASE)],
                        overrides={guid: {'time': '20:00'}}))
    assert result['guid'] == guid
    assert result['time'] == '20:00'


def test_an_overlay_cannot_move_an_event_into_the_future():
    # The date is how the pipeline decides an event is still upcoming; letting
    # an overlay change it would resurrect past events.
    past = dict(_BASE, date='2026-01-01')
    assert _run(ical=[past]) == []
    # Even with an overlay claiming a future date, it stays dropped.
    assert _run(ical=[past], overrides={'anything': {'date': '2026-12-01'}}) == []


def test_a_supplied_guid_survives_loading_and_processing():
    # next_dctech_events-p8o: DynamoDB owns identity. An 8-hex key must not be
    # replaced by a 32-char content hash, or its overlay can never match.
    result = _only(_run(single=[dict(_BASE, guid='abc12345')]))
    assert result['guid'] == 'abc12345'


def test_a_single_event_with_no_guid_still_gets_one():
    result = _only(_run(single=[dict(_BASE)]))
    assert len(result['guid']) == 32


# ── A hidden event must not swallow its visible twin ───────────────
# remove_duplicates keeps whichever of a pair it reaches first; app.get_events
# filters `hidden` afterwards. So a hidden event reached first would absorb the
# visible one and both listings would vanish.


def _twins():
    """Two events calgen's heuristic treats as the same real-world event."""
    return [
        dict(_BASE, title='Shared Talk', guid='aaaa1111'),
        dict(_BASE, title='Shared Talk', guid='bbbb2222'),
    ]


def test_a_hidden_event_does_not_absorb_its_visible_duplicate():
    result = _run(single=_twins(), overrides={'aaaa1111': {'hidden': True}})
    visible = [e for e in result if not e.get('hidden')]
    assert len(visible) == 1
    assert visible[0]['guid'] == 'bbbb2222'


def test_the_same_holds_with_the_ordering_reversed():
    result = _run(single=_twins(), overrides={'bbbb2222': {'hidden': True}})
    visible = [e for e in result if not e.get('hidden')]
    assert len(visible) == 1
    assert visible[0]['guid'] == 'aaaa1111'


def test_two_visible_twins_still_dedup_to_one():
    # The fix must not disable deduplication for ordinary duplicates.
    visible = [e for e in _run(single=_twins()) if not e.get('hidden')]
    assert len(visible) == 1


def test_hidden_events_stay_in_the_output_for_include_hidden_callers():
    result = _run(single=[dict(_BASE, guid='aaaa1111')],
                  overrides={'aaaa1111': {'hidden': True}})
    assert [e['guid'] for e in result] == ['aaaa1111']


# ── Overlay before region ──────────────────────────────────────────


class _RegionPlugin:
    """Rejects anything whose location does not mention Virginia."""

    def location_to_region(self, location):
        if 'VA' not in str(location or ''):
            raise EventRejected('not in region')
        return 'northern-virginia'

    def list_regions(self):
        return [{'slug': 'northern-virginia', 'name': 'Northern Virginia'}]


def _run_with_region(ical, overrides):
    return process_events(
        [GROUP], {}, [], {'testgroup': ical}, [], today=TODAY,
        event_overrides=overrides, region_plugin=_RegionPlugin(),
    )


def test_the_overlays_location_is_what_the_region_is_derived_from():
    # If the region were assigned before the merge, a corrected location could
    # not rescue an event the feed had placed nowhere.
    event = dict(_BASE, location='Somewhere Unknown')
    guid = _guid_of(_only(_run(ical=[dict(event)])))

    assert _run_with_region([dict(event)], {}) == []
    rescued = _only(_run_with_region(
        [dict(event)], {guid: {'location': 'Arlington, VA'}}))
    assert rescued['region'] == 'northern-virginia'


# ── load_single_events, from disk ──────────────────────────────────
# The guid fix lives in this loader, so it is worth exercising through a real
# file rather than only through process_events' in-memory path.


def _write_single(tmp_path, name, data):
    import yaml
    directory = tmp_path / '_single_events'
    directory.mkdir(exist_ok=True)
    (directory / f'{name}.yaml').write_text(yaml.dump(data), encoding='utf-8')


def test_the_loader_honours_a_guid_the_exporter_wrote(tmp_path, monkeypatch):
    from calgen.pipeline import load_single_events
    monkeypatch.chdir(tmp_path)
    _write_single(tmp_path, 'abc12345', dict(_BASE, guid='abc12345'))

    loaded = load_single_events()

    assert [e['guid'] for e in loaded] == ['abc12345']


def test_the_loader_still_computes_a_guid_when_none_was_written(tmp_path,
                                                               monkeypatch):
    from calgen.pipeline import load_single_events
    monkeypatch.chdir(tmp_path)
    _write_single(tmp_path, 'legacy', dict(_BASE))

    loaded = load_single_events()

    assert len(loaded[0]['guid']) == 32


def test_an_overlay_reaches_a_submitted_event_end_to_end(tmp_path, monkeypatch):
    """The bug next_dctech_events-p8o described, from both ends.

    The exporter writes _single_events/{slug}.yaml carrying the table's guid
    and _overlay/{guid}.yaml beside it. Before the fix the loader recomputed a
    32-char hash over an 8-hex key, so the two could never meet.
    """
    from calgen.pipeline import load_overlays, load_single_events
    import yaml

    monkeypatch.chdir(tmp_path)
    _write_single(tmp_path, 'my-event-slug', dict(_BASE, guid='abc12345'))
    overlay_dir = tmp_path / '_overlay'
    overlay_dir.mkdir()
    (overlay_dir / 'abc12345.yaml').write_text(
        yaml.dump({'title': 'Corrected By A Human'}), encoding='utf-8')

    result = _only(process_events(
        [GROUP], {}, load_single_events(), {}, [], today=TODAY,
        event_overrides=load_overlays(),
    ))

    assert result['title'] == 'Corrected By A Human'
