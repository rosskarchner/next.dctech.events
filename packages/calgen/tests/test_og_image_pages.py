"""og:image / twitter:image / JSON-LD image wiring for the page kinds
og-images (calgen og-images) generates cards for beyond individual events:
category pages, week pages, and both /updates/ post kinds (free-form and
weekly roundup). Each just needs to resolve to the right static/og/ path —
the cards themselves are covered by test_og_image.py, and the CLI's
generation of these files by test_og_images_cli.py.

Run: python -m pytest test_og_image_pages.py
"""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from calgen.app import create_app

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'


@pytest.fixture
def client(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    return app.test_client()


def _og_image(html):
    match = re.search(r'<meta property="og:image" content="([^"]*)">', html)
    assert match, 'no og:image meta tag found'
    return match.group(1)


def _jsonld_image(html):
    scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    for raw in scripts:
        data = json.loads(raw)
        if data.get('@type') == 'BlogPosting':
            return data.get('image')
    return None


def test_category_page_og_image(client, monkeypatch):
    # category_page lives in calgen.routes.categories (moved out of app.py),
    # so that's where its calls to these names actually resolve from.
    monkeypatch.setattr('calgen.routes.categories.get_categories', lambda: {
        'ai': {'slug': 'ai', 'name': 'AI & Machine Learning', 'description': ''},
    })
    monkeypatch.setattr('calgen.routes.categories.get_events', lambda *a, **k: [])
    resp = client.get('/categories/ai/')
    assert resp.status_code == 200
    assert _og_image(resp.get_data(as_text=True)).endswith('/static/og/category-ai.png')


def test_week_page_og_image(client, monkeypatch):
    monkeypatch.setattr('calgen.routes.listings.get_events', lambda *a, **k: [])
    monkeypatch.setattr('calgen.routes.listings.get_archived_week', lambda week_id: None)
    resp = client.get('/week/2026-W38/')
    assert resp.status_code == 200
    assert _og_image(resp.get_data(as_text=True)).endswith('/static/og/week-2026-W38.png')


def test_free_post_og_image(client, monkeypatch):
    post = {
        'slug': 'welcome', 'title': 'Welcome to DC Tech Events',
        'published_on': date(2026, 8, 1), 'date_formatted': 'August 1, 2026',
        'body_html': '<p>Hi</p>', 'url': '/updates/welcome/',
    }
    monkeypatch.setattr('calgen.routes.posts.get_free_post', lambda slug: post)
    resp = client.get('/updates/welcome/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _og_image(html).endswith('/static/og/post-welcome.png')
    assert _jsonld_image(html)[0].endswith('/static/og/post-welcome.png')


def test_update_post_og_image(client, monkeypatch):
    post = {
        'title': 'DC Tech Events for the week of September 15, 2026',
        'week_start': date(2026, 9, 15), 'week_start_formatted': 'September 15, 2026',
        'year': 2026, 'month': 9, 'day': 15, 'events': [], 'added_since': None,
        'week_url': '/week/2026-W38/', 'url': '/updates/2026/9/15/',
    }
    monkeypatch.setattr('calgen.routes.posts.get_update_post', lambda year, month, day: post)
    resp = client.get('/updates/2026/9/15/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _og_image(html).endswith('/static/og/post-2026-09-15.png')
    assert _jsonld_image(html)[0].endswith('/static/og/post-2026-09-15.png')


def test_update_post_og_image_key_is_zero_padded_for_single_digit_dates(client, monkeypatch):
    """The route computes og_image_key itself (the post dict has no slug to
    key off of) — confirm single-digit month/day are still zero-padded, so
    the URL matches what `calgen og-images` actually names the file."""
    post = {
        'title': 'DC Tech Events for the week of January 5, 2026',
        'week_start': date(2026, 1, 5), 'week_start_formatted': 'January 5, 2026',
        'year': 2026, 'month': 1, 'day': 5, 'events': [], 'added_since': None,
        'week_url': '/week/2026-W02/', 'url': '/updates/2026/1/5/',
    }
    monkeypatch.setattr('calgen.routes.posts.get_update_post', lambda year, month, day: post)
    resp = client.get('/updates/2026/1/5/')
    assert resp.status_code == 200
    assert _og_image(resp.get_data(as_text=True)).endswith('/static/og/post-2026-01-05.png')
