import os
from datetime import date

import pytest
import yaml

from calgen import updates


def _write_post(tmp_path, week_id, week_start, events=None, **extra):
    d = tmp_path / updates.UPDATES_DIR
    d.mkdir(exist_ok=True)
    data = {
        'week_id': week_id,
        'week_start': week_start,
        'week_end': week_start,
        'events': events if events is not None else [],
        **extra,
    }
    (d / f'{week_id}.yaml').write_text(yaml.dump(data), encoding='utf-8')


@pytest.fixture
def site_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_updates_dir_returns_empty(site_dir):
    assert updates.get_update_posts() == []


def test_posts_are_newest_first(site_dir):
    _write_post(site_dir, '2026-W30', '2026-07-20')
    _write_post(site_dir, '2026-W32', '2026-08-03')
    _write_post(site_dir, '2026-W31', '2026-07-27')

    got = [p['week_id'] for p in updates.get_update_posts()]
    assert got == ['2026-W32', '2026-W31', '2026-W30']


def test_derived_urls_are_unpadded_to_match_int_converter(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03')
    post = updates.get_update_posts()[0]

    # Flask's <int:> converter renders 8, not 08 — a padded literal would not
    # match the path Frozen-Flask writes.
    assert post['url'] == '/updates/2026/8/3/'
    assert post['week_url'] == '/week/2026-W32/'


def test_default_title_when_absent(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03')
    post = updates.get_update_posts()[0]
    assert post['title'] == 'DC Tech Events for the week of August 3, 2026'


def test_explicit_title_wins(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03', title='Custom headline')
    assert updates.get_update_posts()[0]['title'] == 'Custom headline'


def test_week_start_parsed_from_string_and_date(site_dir):
    # PyYAML turns a bare YYYY-MM-DD into a date; a quoted one stays a string.
    _write_post(site_dir, '2026-W32', '2026-08-03')
    _write_post(site_dir, '2026-W31', date(2026, 7, 27))

    for post in updates.get_update_posts():
        assert isinstance(post['week_start'], date)


def test_malformed_and_incomplete_posts_are_skipped(site_dir):
    d = site_dir / updates.UPDATES_DIR
    d.mkdir()
    (d / 'broken.yaml').write_text('{[not valid yaml', encoding='utf-8')
    (d / 'no-week-id.yaml').write_text(
        yaml.dump({'week_start': '2026-08-03'}), encoding='utf-8')
    (d / 'no-start.yaml').write_text(
        yaml.dump({'week_id': '2026-W32'}), encoding='utf-8')
    _write_post(site_dir, '2026-W30', '2026-07-20')

    posts = updates.get_update_posts()
    assert [p['week_id'] for p in posts] == ['2026-W30']


def test_non_yaml_files_ignored(site_dir):
    d = site_dir / updates.UPDATES_DIR
    d.mkdir()
    (d / 'README.md').write_text('not a post', encoding='utf-8')
    assert updates.get_update_posts() == []


def test_get_update_post_lookup(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03')
    assert updates.get_update_post(2026, 8, 3)['week_id'] == '2026-W32'
    assert updates.get_update_post(2026, 8, 4) is None


def test_event_count_defaults_to_len_events(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03',
                events=[{'title': 'A'}, {'title': 'B'}])
    assert updates.get_update_posts()[0]['event_count'] == 2


def test_summarize_lists_titles_and_remainder(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03', events=[
        {'title': 'A'}, {'title': 'B'}, {'title': 'C'}, {'title': 'D'},
    ])
    post = updates.get_update_posts()[0]
    summary = updates.summarize(post)
    assert '4 events this week' in summary
    assert 'A, B, C, and 1 more' in summary


def test_summarize_singular_and_empty(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03', events=[{'title': 'Solo'}])
    _write_post(site_dir, '2026-W31', '2026-07-27', events=[])

    by_id = {p['week_id']: p for p in updates.get_update_posts()}
    assert '1 event this week' in updates.summarize(by_id['2026-W32'])
    assert updates.summarize(by_id['2026-W31']) == \
        'No events were listed for this week.'


# ── Free-form posts ────────────────────────────────────────────────

def _write_free(tmp_path, slug, published_on, status='published', **extra):
    d = tmp_path / updates.POSTS_DIR
    d.mkdir(exist_ok=True)
    data = {
        'slug': slug,
        'published_on': published_on,
        'status': status,
        'title': extra.pop('title', f'Post {slug}'),
        'body': extra.pop('body', 'Hello world.'),
        **extra,
    }
    (d / f'{slug}.yaml').write_text(yaml.dump(data), encoding='utf-8')


def test_no_posts_dir_returns_empty(site_dir):
    assert updates.get_free_posts() == []


def test_free_post_renders_markdown_body(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05',
                body='## Heading\n\nSome **bold** text.')
    post = updates.get_free_posts()[0]
    assert '<h2>Heading</h2>' in post['body_html']
    assert '<strong>bold</strong>' in post['body_html']


def test_free_post_url_uses_slug(site_dir):
    _write_free(site_dir, 'recurring-events', '2026-08-05')
    assert updates.get_free_posts()[0]['url'] == '/updates/recurring-events/'


def test_drafts_are_never_rendered(site_dir):
    _write_free(site_dir, 'live', '2026-08-05', status='published')
    _write_free(site_dir, 'wip', '2026-08-06', status='draft')

    slugs = [p['slug'] for p in updates.get_free_posts()]
    assert slugs == ['live']
    assert updates.get_free_post('wip') is None


def test_status_defaults_to_draft_when_absent(site_dir):
    d = site_dir / updates.POSTS_DIR
    d.mkdir()
    (d / 'x.yaml').write_text(
        yaml.dump({'slug': 'x', 'published_on': '2026-08-05', 'body': 'hi'}),
        encoding='utf-8')
    assert updates.get_free_posts() == []


def test_free_post_missing_slug_or_date_skipped(site_dir):
    d = site_dir / updates.POSTS_DIR
    d.mkdir()
    (d / 'a.yaml').write_text(
        yaml.dump({'published_on': '2026-08-05', 'status': 'published'}),
        encoding='utf-8')
    (d / 'b.yaml').write_text(
        yaml.dump({'slug': 'b', 'status': 'published'}), encoding='utf-8')
    assert updates.get_free_posts() == []


def test_get_free_post_lookup(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05')
    assert updates.get_free_post('hello')['title'] == 'Post hello'
    assert updates.get_free_post('nope') is None


def test_all_posts_interleaves_both_kinds_by_date(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03')
    _write_post(site_dir, '2026-W31', '2026-07-27')
    _write_free(site_dir, 'midweek', '2026-07-30')
    _write_free(site_dir, 'later', '2026-08-06')

    got = [(p['kind'], p.get('slug') or p.get('week_id'))
           for p in updates.get_all_posts()]
    assert got == [
        ('post', 'later'),          # Aug 6
        ('weekly', '2026-W32'),     # Aug 3
        ('post', 'midweek'),        # Jul 30
        ('weekly', '2026-W31'),     # Jul 27
    ]


def test_all_posts_share_a_common_display_shape(site_dir):
    _write_post(site_dir, '2026-W32', '2026-08-03')
    _write_free(site_dir, 'hello', '2026-08-05')

    for post in updates.get_all_posts():
        assert isinstance(post['published_on'], date)
        assert post['url'].startswith('/updates/')
        assert post['title']
        assert post['date_formatted']
        assert post['kind'] in ('weekly', 'post')


def test_summarize_free_post_prefers_explicit_summary(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05',
                summary='The short version.', body='## H\n\nLong prose here.')
    assert updates.summarize(updates.get_free_posts()[0]) == 'The short version.'


def test_summarize_free_post_skips_headings(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05',
                body='## A heading\n\nThe first real line.\n\nMore.')
    assert updates.summarize(updates.get_free_posts()[0]) == 'The first real line.'


def test_summarize_free_post_strips_markdown_syntax(site_dir):
    # The blurb comes from rendered HTML, so no **bold** or [link](url) leaks.
    _write_free(site_dir, 'hello', '2026-08-05',
                body='I added **recurring events** — see [the groups](/groups/).')
    summary = updates.summarize(updates.get_free_posts()[0])
    assert summary == 'I added recurring events — see the groups.'


def test_summarize_free_post_joins_wrapped_source_lines(site_dir):
    # A paragraph wrapped across source lines is one sentence, not a fragment.
    _write_free(site_dir, 'hello', '2026-08-05',
                body='The first half of a sentence\nand the second half.')
    summary = updates.summarize(updates.get_free_posts()[0])
    assert summary == 'The first half of a sentence and the second half.'


def test_summarize_free_post_truncates_long_prose_on_word_boundary(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05', body='word ' * 100)
    summary = updates.summarize(updates.get_free_posts()[0])
    assert len(summary) <= 201
    assert summary.endswith('…')
    assert 'wor…' not in summary  # never clipped mid-word


def test_summarize_free_post_with_empty_body(site_dir):
    _write_free(site_dir, 'hello', '2026-08-05', body='')
    assert updates.summarize(updates.get_free_posts()[0]) == ''
