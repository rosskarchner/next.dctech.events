"""
Highlight routes: the public quote and checkout, and the /edit admin surface.
The PayPal IPN for highlights arrives on the sponsorships' /api/paypal/ipn
route, which hands anything whose `custom` starts with `highlight|` to
highlights.handle_ipn. The rules live in highlights.py, shared with the MCP
server, so this module only translates HTTP to calls and back.
"""

import highlights as hl
from db import check_and_record_write
from routes.admin import _admin_check
from routes.responses import json as _json
from routes.sponsorships import _notify_url, _payload, _source_ip


def _error(exc, event):
    return _json(exc.status_code, {'error': str(exc)}, event)


# ─── Public ────────────────────────────────────────────────────────

def quote_json(event, jinja_env):
    """GET /api/highlights/quote?guid=."""
    guid = (event.get('queryStringParameters') or {}).get('guid')
    try:
        return _json(200, hl.quote(guid), event)
    except hl.HighlightError as exc:
        return _error(exc, event)


def checkout_json(event, jinja_env):
    """POST /api/highlights — {guid, contact_email, replace_booking_id?}."""
    data = _payload(event)
    ip = _source_ip(event)
    allowed, retry_after = check_and_record_write(ip, 'highlight')
    if not allowed:
        return _json(429, {'error': f'Too many attempts — try again in {retry_after}s'},
                     event)
    allowed, retry_after = check_and_record_write(
        ip, 'highlight_hold', limit=hl.HOLDS_PER_IP_PER_HOUR)
    if not allowed:
        return _json(429, {'error': f'Too many holds — try again in {retry_after}s'},
                     event)
    try:
        item = hl.create_hold(data)
    except hl.HighlightError as exc:
        return _error(exc, event)
    return _json(201, {
        'target_key': item['target_key'],
        'booking_id': item['booking_id'],
        'status': item['status'],
        'hold_expires_at': item['hold_expires_at'],
        'paypal_url': hl.paypal_form_url(),
        'paypal_fields': hl.paypal_fields(item, _notify_url(event)),
    }, event)


# ─── Admin ─────────────────────────────────────────────────────────

def _admin(handler):
    def wrapped(event, jinja_env, *args):
        claims, err = _admin_check(event)
        if err:
            return err
        try:
            return handler(event, claims, *args)
        except hl.HighlightError as exc:
            return _error(exc, event)
    return wrapped


def _reviewer(claims):
    return claims.get('email') or 'admin'


@_admin
def list_json(event, claims):
    """GET /api/admin/highlights[?status=]."""
    status = (event.get('queryStringParameters') or {}).get('status')
    return _json(200, {'highlights': hl.list_highlights(status),
                       'price_usd': str(hl.PRICE_USD)}, event)


@_admin
def create_json(event, claims):
    """POST /api/admin/highlights — {guid, note?}: an editor's pick."""
    data = _payload(event)
    item = hl.add_editor_pick(data.get('guid'), _reviewer(claims),
                              data.get('note') or '')
    return _json(201, {'highlight': item}, event)


@_admin
def revoke_json(event, claims, target_key):
    """POST /api/admin/highlights/{key}/revoke — {reason?}."""
    reason = str(_payload(event).get('reason') or '')
    return _json(200, {'highlight': hl.revoke(target_key, _reviewer(claims),
                                              reason)}, event)
