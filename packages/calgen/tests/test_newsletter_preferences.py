"""Per-subscriber category/region filtering for the newsletter, and the
newsletter's unconditional exclusion of virtual events.

_filter_events is what the sender's per-preference-group rendering (outside
this package) relies on to actually narrow content; the two routes'
?categories=&regions= handling is what render.py calls into. The live
public /newsletter.html page must be byte-for-byte unaffected by
category/region query params when none are passed — that's the load-bearing
backward-compat guarantee for that part. Virtual events, though, are always
dropped regardless of params or preferences — email is a poor fit for
"join from anywhere" listings, so this isn't a preference axis at all.

Run: python -m pytest test_newsletter_preferences.py
"""
from datetime import date as _date, timedelta
from pathlib import Path

import pytest

from calgen.app import create_app
from calgen.routes.newsletter import _filter_events, PREFERENCES_LINK_PLACEHOLDER

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

# Well inside _newsletter_context's UPCOMING_WINDOW_DAYS=21 window, so the
# route-level tests below aren't filtered out by that window before
# category/region filtering ever runs.
_SOON = (_date.today() + timedelta(days=3)).isoformat()


def _event(guid, categories=None, region=None, virtual=False, date=_SOON):
    return {
        'guid': guid, 'title': f'Event {guid}', 'date': date, 'time': '18:00',
        'categories': categories or [], 'region': region,
        # is_virtual_event() checks this field, not a plain boolean.
        'location_type': 'virtual' if virtual else 'in-person',
    }


# ── _filter_events (category/region only — virtual exclusion happens a
#    layer up, in _newsletter_context, before events ever reach this) ──

def test_no_filters_returns_every_event_unchanged():
    events = [_event('a'), _event('b')]
    assert _filter_events(events, None, None) == events
    assert _filter_events(events, [], []) == events


def test_category_filter_keeps_only_matching_events():
    ai = _event('ai-event', categories=['ai'])
    cloud = _event('cloud-event', categories=['cloud'])
    assert _filter_events([ai, cloud], ['ai'], None) == [ai]


def test_an_event_with_any_matching_category_passes():
    both = _event('both', categories=['ai', 'cloud'])
    assert _filter_events([both], ['cloud'], None) == [both]


def test_region_filter_keeps_only_matching_events():
    dc = _event('dc-event', region='dc')
    va = _event('va-event', region='va')
    assert _filter_events([dc, va], None, ['dc']) == [dc]


def test_an_event_with_no_region_is_excluded_by_a_region_filter():
    no_region = _event('mystery', region=None)
    assert _filter_events([no_region], None, ['dc']) == []


def test_both_filters_apply_together():
    matches = _event('match', categories=['ai'], region='dc')
    wrong_category = _event('wrong-cat', categories=['cloud'], region='dc')
    wrong_region = _event('wrong-region', categories=['ai'], region='va')
    result = _filter_events([matches, wrong_category, wrong_region], ['ai'], ['dc'])
    assert result == [matches]


# ── Routes ───────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    events = [
        _event('ai-dc', categories=['ai'], region='dc'),
        _event('cloud-va', categories=['cloud'], region='va'),
        _event('remote-ai', categories=['ai'], region=None, virtual=True),
    ]
    # newsletter.py does `from calgen.routes.common import (..., get_events,
    # ...)`, so it holds its own reference — the module actually calling it
    # is where a monkeypatch has to land.
    monkeypatch.setattr('calgen.routes.newsletter.get_events', lambda: events)
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def test_no_query_params_still_drops_virtual_events(client):
    # Not "unfiltered" in the virtual sense — that exclusion is
    # unconditional, not a category/region preference — but categories and
    # regions themselves are otherwise untouched with no params.
    resp = client.get('/newsletter.html')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Event ai-dc' in html
    assert 'Event cloud-va' in html
    assert 'Event remote-ai' not in html


def test_categories_query_param_filters_the_html(client):
    resp = client.get('/newsletter.html?categories=cloud')
    html = resp.get_data(as_text=True)
    assert 'Event cloud-va' in html
    assert 'Event ai-dc' not in html
    assert 'Event remote-ai' not in html


def test_regions_query_param_filters_the_html_virtual_stays_excluded(client):
    resp = client.get('/newsletter.html?regions=dc')
    html = resp.get_data(as_text=True)
    assert 'Event ai-dc' in html
    assert 'Event remote-ai' not in html
    assert 'Event cloud-va' not in html


def test_newsletter_text_route_respects_the_same_params(client):
    resp = client.get('/newsletter.txt?categories=ai')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'Event ai-dc' in text
    assert 'Event remote-ai' not in text
    assert 'Event cloud-va' not in text


def test_the_preferences_link_placeholder_appears_literally(client):
    html = client.get('/newsletter.html').get_data(as_text=True)
    assert PREFERENCES_LINK_PLACEHOLDER in html
    text = client.get('/newsletter.txt').get_data(as_text=True)
    assert PREFERENCES_LINK_PLACEHOLDER in text


def test_multiple_comma_separated_slugs_are_all_honored(client):
    resp = client.get('/newsletter.html?categories=ai,cloud')
    html = resp.get_data(as_text=True)
    assert 'Event ai-dc' in html
    assert 'Event cloud-va' in html


def test_a_purely_virtual_event_set_leaves_the_newsletter_empty(monkeypatch):
    monkeypatch.setattr(
        'calgen.routes.newsletter.get_events',
        lambda: [_event('only-remote', virtual=True)])
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    html = app.test_client().get('/newsletter.html').get_data(as_text=True)
    assert 'Event only-remote' not in html
