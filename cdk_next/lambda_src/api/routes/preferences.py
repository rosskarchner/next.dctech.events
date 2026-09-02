"""Subscriber category/region newsletter preferences.

Reached via a unique, emailed magic link (see routes/submit.py's
request_link_json, purpose='prefs') rather than a Cognito account — the
same account-free pattern as event submission/correction, since a
newsletter subscriber never has one. Both routes are public at the API
Gateway level; the Lambda does its own token verification.
"""
import json

import magic_link
from db import get_all_categories, get_subscriber_preferences, put_subscriber_preferences
from routes.responses import html as _html, json as _json_response

# Static and effectively permanent — see site/regions.py, the source of
# truth this list is duplicated from. The API Lambda's build has no copy of
# that file (it's calgen/site-only), and regions have been a deliberately
# static, git-committed list since this DynamoDB-backed stack replaced the
# old one (lambda_src/mcp/server.py explicitly dropped a write_regions
# tool as "not applicable" for exactly this reason). Update both places if
# a region is ever added.
REGIONS = {
    'dc': {'slug': 'dc', 'name': 'Washington DC'},
    'md': {'slug': 'md', 'name': 'Maryland'},
    'va': {'slug': 'va', 'name': 'Virginia'},
}


def _json(status_code, body, event=None):
    return _json_response(status_code, body, event)


def _error_payload(message):
    return {'error': message}


def _parse_body(event):
    body = event.get('body', '')
    if not body:
        return {}
    content_type = (event.get('headers', {}).get('Content-Type')
                     or event.get('headers', {}).get('content-type') or '')
    if 'application/json' in content_type:
        return json.loads(body)
    from urllib.parse import parse_qs
    parsed = parse_qs(body)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def _token_from_query(event):
    """GET requests carry the token as e/t/s query params (matching
    build_link's own param names), not the mlt_* body fields
    token_from_request expects — those are for POST bodies."""
    params = event.get('queryStringParameters') or {}
    email = magic_link.decode_email_param(params.get('e', ''))
    return email, params.get('t', ''), params.get('s', '')


def _clean_slugs(values, valid_slugs):
    """Normalize a categories/regions selection to a deduped list of only
    real, currently-existing slugs — a stale or hand-crafted slug is
    silently dropped rather than stored (it would just never match any
    event, so keeping it around is pure debris)."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    seen = []
    for v in values:
        v = str(v or '').strip()
        if v and v in valid_slugs and v not in seen:
            seen.append(v)
    return seen


def get_preferences_json(event, jinja_env):
    """GET /api/preferences?e=&t=&s= — current selections plus the full
    category/region catalog, so the preferences page needs no second
    unauthenticated call to populate its checkboxes."""
    email, timestamp, signature = _token_from_query(event)
    ok, reason = magic_link.verify_token(email, timestamp, signature, purpose='prefs')
    if not ok:
        return _json(401, _error_payload(reason), event)

    prefs = get_subscriber_preferences(email) or {'categories': [], 'regions': []}
    return _json(200, {
        'email': email,
        'categories': prefs['categories'],
        'regions': prefs['regions'],
        'all_categories': get_all_categories(),
        'all_regions': REGIONS,
    }, event)


def update_preferences_json(event, jinja_env):
    """POST /api/preferences — body carries the same e/t/s token fields
    (magic_link.token_from_request's mlt_e/mlt_t/mlt_s or mlt_email/mlt_ts/
    mlt_sig) plus categories/regions to save. Scoped to preferences only —
    does not touch SES subscription/opt-in status."""
    data = _parse_body(event)
    email, timestamp, signature = magic_link.token_from_request(data)
    ok, reason = magic_link.verify_token(email, timestamp, signature, purpose='prefs')
    if not ok:
        return _json(401, _error_payload(reason), event)

    all_categories = get_all_categories()
    categories = _clean_slugs(data.get('categories'), set(all_categories.keys()))
    regions = _clean_slugs(data.get('regions'), set(REGIONS.keys()))

    put_subscriber_preferences(email, categories, regions)
    return _json(200, {'categories': categories, 'regions': regions}, event)
