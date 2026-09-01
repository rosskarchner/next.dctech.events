"""BreadcrumbList JSON-LD on category/location pages -- previously only
Event and WebPage types appeared anywhere on the site, so crawlers got no
structured signal for the Locations -> VA / Categories -> AI hierarchy these
pages actually sit in.

Run: python -m pytest test_breadcrumb_jsonld.py
"""
import json
import re
from pathlib import Path

import pytest

from calgen.app import create_app

SITE_DIR = Path(__file__).resolve().parents[3] / 'site'

_FAKE_EVENT = {
    'title': 'Test Talk', 'date': '2026-10-05', 'time': '18:00',
    'url': 'https://example.com/e', 'categories': ['ai'], 'region': 'va',
}


class _FakeRegionPlugin:
    def list_regions(self):
        return [{'slug': 'va', 'name': 'Virginia'}]


@pytest.fixture
def client(monkeypatch):
    app = create_app(site_dir=str(SITE_DIR))
    app.testing = True
    app.region_plugin = _FakeRegionPlugin()
    # category_page and region_page both moved out of app.py (to
    # calgen.routes.categories / calgen.routes.locations respectively) — each
    # needs patching where it actually resolves the name from. Python looks
    # up a bare name via the *defining* module's globals, not through
    # wherever else re-exports it.
    monkeypatch.setattr('calgen.routes.categories.get_events', lambda *a, **k: [_FAKE_EVENT])
    monkeypatch.setattr('calgen.routes.locations.get_events', lambda *a, **k: [_FAKE_EVENT])
    monkeypatch.setattr('calgen.routes.categories.get_categories', lambda: {
        'ai': {'slug': 'ai', 'name': 'AI', 'description': ''},
    })
    return app.test_client()


def _breadcrumbs(html):
    scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    for raw in scripts:
        data = json.loads(raw)
        if data.get('@type') == 'BreadcrumbList':
            return data
    raise AssertionError('no BreadcrumbList JSON-LD found')


def test_category_page_breadcrumbs(client):
    resp = client.get('/categories/ai/')
    assert resp.status_code == 200
    data = _breadcrumbs(resp.get_data(as_text=True))

    items = data['itemListElement']
    assert [i['position'] for i in items] == [1, 2, 3]
    assert items[0]['name'] == 'Home'
    assert items[1] == {'@type': 'ListItem', 'position': 2, 'name': 'Categories',
                         'item': items[1]['item']}
    assert items[1]['item'].endswith('/categories/')
    assert items[2]['name'] == 'AI'
    assert items[2]['item'].endswith('/categories/ai/')


def test_location_page_breadcrumbs(client):
    resp = client.get('/locations/va/')
    assert resp.status_code == 200
    data = _breadcrumbs(resp.get_data(as_text=True))

    items = data['itemListElement']
    assert [i['position'] for i in items] == [1, 2, 3]
    assert items[1]['item'].endswith('/locations/')
    assert items[2]['name'] == 'Virginia'
    assert items[2]['item'].endswith('/locations/va/')
