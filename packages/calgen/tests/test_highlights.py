"""Highlighted events in calgen: matching a highlight to this build's event,
the 21-day window, which pages get a box (and which highlights each shows),
the inline label, the newsletter, the /highlights/ page — and that there is
no box at all when nothing matches.

Run: python -m pytest tests/test_highlights.py
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from calgen import highlights as hl
from calgen.app import create_app
from calgen.routes.common import get_week_identifier, local_tz, select_highlights

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'
TODAY = datetime.now(local_tz).date()


def _day(n):
    return (TODAY + timedelta(days=n)).isoformat()


def _event(guid, n, title=None, **extra):
    return {'guid': guid, 'title': title or f'Event {guid}', 'date': _day(n),
            'time': '18:00', 'url': f'https://{guid}.example/e',
            'location': 'Washington, DC', 'location_type': 'physical',
            'group': f'Group {guid}', 'categories': ['python'], 'source': 'ical',
            **extra}


def _mark(event, kind='editor'):
    event['highlight'] = {'kind': kind, 'key': f"g-{event['guid']}",
                          'label': hl.LABELS[kind]}
    return event


# ─── Matching ───────────────────────────────────────────────────────

def _h(**kw):
    base = {'target_key': 'g-orig', 'kind': 'paid', 'guid': 'orig',
            'title': 'BSides NOVA', 'date': _day(10), 'time': '08:00',
            'url': 'https://bsides.example/nova', 'group': 'BSides',
            'recurring_slug': '', 'resolved_guid': ''}
    return {**base, **kw}


def test_match_by_guid():
    events = [_event('orig', 10), _event('other', 10)]
    assert hl.find_match(_h(), events, TODAY.isoformat())['guid'] == 'orig'


def test_match_by_resolved_guid():
    events = [_event('moved', 12)]
    found = hl.find_match(_h(resolved_guid='moved'), events, TODAY.isoformat())
    assert found['guid'] == 'moved'


def test_reschedule_follows_the_url_within_the_group():
    # The iCal guid hashes the date, so a reschedule is a new guid.
    events = [_event('new', 14, group='BSides', url='https://bsides.example/nova/'),
              _event('x', 14, group='Other', url='https://bsides.example/nova')]
    found = hl.find_match(_h(), events, TODAY.isoformat())
    assert found['guid'] == 'new'


def test_reschedule_by_title_when_url_changed():
    events = [_event('new', 14, title='BSides  NOVA!', group='BSides',
                     url='https://elsewhere.example')]
    assert hl.find_match(_h(), events, TODAY.isoformat())['guid'] == 'new'


def test_ambiguous_fallback_does_not_guess():
    events = [_event('a', 14, title='BSides NOVA', group='BSides', url='https://a.example'),
              _event('b', 21, title='BSides NOVA', group='BSides', url='https://b.example')]
    assert hl.find_match(_h(), events, TODAY.isoformat()) is None


def test_recurring_occurrence_matches_series_and_date():
    occ = _event('fresh-guid-each-build', 3, recurring_slug='tuesdays',
                 is_recurring=True)
    other = _event('next-week', 10, recurring_slug='tuesdays', is_recurring=True)
    h = _h(target_key=f'r-tuesdays-{_day(3)}', guid='old-guid',
           recurring_slug='tuesdays', date=_day(3))
    assert hl.find_match(h, [other, occ], TODAY.isoformat()) is occ


def test_recurring_occurrence_never_falls_back_to_another_date():
    h = _h(recurring_slug='tuesdays', date=_day(3))
    assert hl.find_match(h, [_event('n', 10, recurring_slug='tuesdays')],
                         TODAY.isoformat()) is None


def test_duplicate_merged_into_parent_is_followed():
    # remove_duplicates dropped our guid and credited our group on the parent.
    parent = _event('parent', 10, group='Meetup Org', url='https://parent.example',
                    also_published_by=[{'group': 'BSides'}])
    assert hl.find_match(_h(), [parent], TODAY.isoformat()) is parent


def test_duplicate_with_same_url_and_date_is_followed():
    parent = _event('parent', 10, group='Other', url='https://bsides.example/nova')
    assert hl.find_match(_h(), [parent], TODAY.isoformat()) is parent


def test_hidden_events_never_match():
    assert hl.find_match(_h(), [_event('orig', 10, hidden=True)],
                         TODAY.isoformat()) is None


def test_apply_marks_events_and_reports_matches(tmp_path):
    (tmp_path / 'highlights.json').write_text(json.dumps([
        _h(), _h(target_key='g-gone', guid='gone', url='https://gone.example',
                 group='Gone', title='Gone')]))
    events = [_event('orig', 10)]
    matches = hl.run(events, TODAY.isoformat(), str(tmp_path))
    assert matches == {'g-orig': 'orig', 'g-gone': None}
    assert events[0]['highlight'] == {'kind': 'paid', 'key': 'g-orig', 'label': 'Paid'}
    assert json.loads((tmp_path / 'highlight_matches.json').read_text()) == matches


def test_missing_highlights_file_is_fine(tmp_path):
    assert hl.run([_event('a', 1)], TODAY.isoformat(), str(tmp_path)) == {}


# ─── The window ─────────────────────────────────────────────────────

def test_window_keeps_upcoming_in_person_within_21_days():
    events = [_mark(_event('soon', 2)), _mark(_event('edge', 21)),
              _mark(_event('far', 22)), _mark(_event('past', -1)),
              _mark(_event('virtual', 3, location_type='virtual')),
              _event('plain', 4)]
    picked = select_highlights(events)
    assert [p['title'] for p in picked] == ['Event soon', 'Event edge']
    assert picked[0]['short_time'] == '6pm'
    assert picked[0]['label'] == "Editor's pick"


def test_window_sorts_by_date_then_time():
    a = _mark(_event('a', 5, time='19:30'))
    b = _mark(_event('b', 5, time='08:00'))
    c = _mark(_event('c', 1))
    assert [p['title'] for p in select_highlights([a, b, c])] == \
        ['Event c', 'Event b', 'Event a']
    assert select_highlights([a])[0]['short_time'] == '7:30pm'


# ─── Pages ──────────────────────────────────────────────────────────

PY = _mark(_event('py', 3, title='PyData DC', categories=['python']), 'paid')
SEC = _mark(_event('sec', 4, title='BSides NOVA', categories=['cybersecurity']))
LATER = _mark(_event('later', 40, title='Far Conf', categories=['python']))
PLAIN = _event('plain', 2, title='Plain Meetup', categories=['python', 'cybersecurity'])


def _client(monkeypatch, tmp_path, events):
    (tmp_path / '_data').mkdir()
    (tmp_path / '_data' / 'sponsors.json').write_text('[]')
    monkeypatch.chdir(tmp_path)
    getter = lambda *a, **k: [dict(e) for e in events]  # noqa: E731
    for mod in ('common', 'listings', 'categories', 'newsletter', 'highlights'):
        monkeypatch.setattr(f'calgen.routes.{mod}.get_events', getter)
    monkeypatch.setattr('calgen.routes.listings.get_archived_week', lambda *a, **k: None)
    monkeypatch.setattr('calgen.routes.listings.get_archived_events', lambda *a, **k: [])
    cats = lambda *a, **k: {'python': {'name': 'Python'},  # noqa: E731
                             'cybersecurity': {'name': 'Cybersecurity'}}
    monkeypatch.setattr('calgen.routes.categories.get_categories', cats)
    monkeypatch.setattr('calgen.app.get_categories', cats)
    monkeypatch.setattr('calgen.routes.highlights.get_config',
                        lambda: {'highlight_price_usd': '10'})
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


def _box(html):
    if 'class="pick-box"' not in html:
        return None
    return html.split('class="pick-box"', 1)[1].split('</aside>', 1)[0]


@pytest.fixture
def client(monkeypatch, tmp_path):
    return _client(monkeypatch, tmp_path, [PLAIN, PY, SEC, LATER])


def test_homepage_box_lists_window_highlights_linking_out(client):
    box = _box(_get(client, '/'))
    assert 'PyData DC' in box and 'BSides NOVA' in box
    assert 'Far Conf' not in box
    assert 'href="https://py.example/e"' in box
    assert '/events/' not in box
    assert 'href="/highlights/"' in box
    assert box.index('PyData DC') < box.index('BSides NOVA')


def test_category_box_shows_only_that_category(client):
    box = _box(_get(client, '/categories/cybersecurity/'))
    assert 'BSides NOVA' in box and 'PyData DC' not in box


def test_month_box_shows_only_that_month(client):
    soon, later = TODAY + timedelta(days=3), TODAY + timedelta(days=40)
    box = _box(_get(client, f'/{soon.year}/{soon.month}/'))
    assert 'PyData DC' in box and 'Far Conf' not in box
    # Far Conf's own month: it is outside the 21-day window, and nothing
    # else highlighted falls in that month, so no box at all.
    assert _box(_get(client, f'/{later.year}/{later.month}/')) is None


def test_week_box_shows_only_that_week(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, [PY])
    week = get_week_identifier(TODAY + timedelta(days=3))
    other = get_week_identifier(TODAY + timedelta(days=14))
    assert 'PyData DC' in _box(_get(client, f'/week/{week}/'))
    assert _box(_get(client, f'/week/{other}/')) is None


def test_inline_label_and_accent_in_day_list(client):
    html = _get(client, '/categories/python/')
    day_list = html.split('class="events-by-day"', 1)[1]
    assert 'has-pick' in day_list
    assert 'class="pick-label pick-label-buyer" href="/highlights/">Paid</a>' in day_list
    # The plain meetup is still listed, without a label.
    plain = day_list.split('Plain Meetup', 1)[1].split('</li>', 1)[0]
    assert 'pick-label' not in plain


@pytest.mark.parametrize('path', [
    '/', '/categories/python/', f'/{TODAY.year}/{TODAY.month}/',
    f'/week/{get_week_identifier(TODAY)}/',
])
def test_no_box_at_all_without_highlights(monkeypatch, tmp_path, path):
    client = _client(monkeypatch, tmp_path, [PLAIN])
    html = _get(client, path)
    assert 'pick-box' not in html
    assert 'Highlighted' not in html


def test_newsletter_has_box_before_the_day_list(client):
    html = _get(client, '/newsletter.html')
    box = _newsletter_box(html)
    assert 'PyData DC' in box and 'BSides NOVA' in box and 'Far Conf' not in box
    assert 'href="https://py.example/e"' in box
    assert html.index('<strong>Highlighted</strong>') < html.index('Plain Meetup')


def _newsletter_box(html):
    return html.split('<strong>Highlighted</strong>', 1)[1].split('<h2', 1)[0]


def test_newsletter_respects_preferences(client):
    box = _newsletter_box(_get(client, '/newsletter.html?categories=cybersecurity'))
    assert 'BSides NOVA' in box and 'PyData DC' not in box


def test_newsletter_text(client):
    text = _get(client, '/newsletter.txt')
    assert "Highlighted (what's this?" in text
    assert '[Paid]' in text and "[Editor's pick]" in text
    assert '  https://py.example/e' in text
    assert 'About highlights: ' in text and text.count('/highlights/') >= 2


@pytest.mark.parametrize('path', ['/newsletter.html', '/newsletter.txt'])
def test_newsletter_without_highlights_has_no_box(monkeypatch, tmp_path, path):
    client = _client(monkeypatch, tmp_path, [PLAIN])
    body = _get(client, path)
    assert 'Highlighted' not in body
    assert 'About highlights' not in body


def test_highlights_page_explains_and_lists(client):
    html = _get(client, '/highlights/')
    main = html.split('</header>', 1)[1]
    assert "Editor's pick" in main and 'Paid' in main
    assert '$10' in main
    assert 'In-person events only' in main
    assert 'PyData DC' in main and 'Far Conf' not in main


def test_class_names_avoid_easylist():
    # EasyList hides .header-sponsor (and similar) outright, which blanked the
    # sponsor slot in Firefox once. Everything here is pick-*.
    for name in ('partials/highlight_box.html', 'partials/events_by_day.html',
                 'highlights.html', 'event_page.html'):
        text = (SITE_DIR / 'templates' / name).read_text()
        classes = ' '.join(part.split('"', 1)[0]
                           for part in text.split('class="')[1:])
        for word in ('sponsor', 'promoted', 'paid', 'advert'):
            assert word not in classes.lower(), (name, word)
        assert ' ad ' not in f' {classes} ' and 'ad-' not in classes
