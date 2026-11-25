"""IndexNow: tell Bing (and the engines sharing its index) what changed.

Search engines otherwise find a new event page whenever they next crawl the
sitemap, which for a listing that is only useful for a few weeks can be too
late. IndexNow lets the site say "these URLs changed" right after a build.

Ownership is proven by a key file at the site root, `/<key>.txt` containing
the key. The key is not a secret: anyone can read it, and all it lets someone
do is ask Bing to recrawl our own pages. It lives in config.yaml as
`indexnow_key`, and calgen serves the file from a root-level route (a key
under /static/ would only vouch for URLs under /static/). Leave the key out to
switch the whole thing off.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from calgen.lastmod import get_changed_slugs
from calgen.site_config import get_config

ENDPOINT = 'https://api.indexnow.org/indexnow'
# The protocol's per-request ceiling.
MAX_URLS = 10_000


def changed_urls(base_url, slugs):
    if not slugs:
        return []
    urls = [f"{base_url}/"] + [f"{base_url}/events/{slug}/" for slug in slugs]
    return urls[:MAX_URLS]


def build_payload(base_url, key, urls):
    return {
        'host': urlparse(base_url).netloc,
        'key': key,
        'keyLocation': f"{base_url}/{key}.txt",
        'urlList': urls,
    }


def submit_changed(dry_run=False, opener=urllib.request.urlopen):
    """Submit this build's changed pages. Returns a one-line summary."""
    cfg = get_config()
    key = str(cfg.get('indexnow_key') or '').strip()
    base_url = str(cfg.get('base_url') or '').rstrip('/')
    if not key or not base_url:
        return 'indexnow: no indexnow_key/base_url configured; skipped'
    urls = changed_urls(base_url, get_changed_slugs())
    if not urls:
        return 'indexnow: nothing changed; skipped'
    payload = build_payload(base_url, key, urls)
    if dry_run:
        return f'indexnow (dry run): would submit {len(urls)} URLs\n' + json.dumps(payload, indent=2)
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with opener(request, timeout=20) as response:
            return f'indexnow: submitted {len(urls)} URLs (HTTP {response.status})'
    except urllib.error.HTTPError as e:
        return f'indexnow: submission refused, HTTP {e.code}; continuing'
    except (urllib.error.URLError, OSError) as e:
        return f'indexnow: submission failed ({e}); continuing'


def register_routes(app):
    """Serve /<key>.txt when a key is configured. Registered at app creation
    because the URL itself is the key."""
    key = str(get_config().get('indexnow_key') or '').strip()
    if not key:
        return
    from flask import Response

    def indexnow_key_file():
        return Response(key, mimetype='text/plain')

    app.add_url_rule(f'/{key}.txt', 'indexnow_key_file', indexnow_key_file)
