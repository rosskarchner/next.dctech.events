"""
Sponsorship routes: public booking, the PayPal IPN listener, and the /edit
admin surface. The rules themselves live in sponsorships.py, shared with the
MCP server, so this module only translates HTTP to calls and back.
"""

import base64
import json
import traceback

import sponsorships as sp
from db import check_and_record_write
from routes.admin import _admin_check
from routes.responses import json as _json


def _payload(event):
    raw = event.get('body') or ''
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode('utf-8', 'replace')
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        from urllib.parse import parse_qsl
        data = dict(parse_qsl(raw))
    return data if isinstance(data, dict) else {}


def _error(exc, event):
    return _json(exc.status_code, {'error': str(exc)}, event)


def _source_ip(event):
    return ((event.get('requestContext') or {}).get('identity') or {}).get(
        'sourceIp') or 'unknown'


def _notify_url(event):
    """Where PayPal should POST the IPN: this same API, by its own address.

    Read from the request rather than an env var, since the API's URL is not
    known until the stack that creates this function has made the RestApi.
    """
    ctx = event.get('requestContext') or {}
    domain = ctx.get('domainName')
    stage = ctx.get('stage')
    if not domain or not stage:
        return None
    return f'https://{domain}/{stage}/api/paypal/ipn'


# ─── Public ────────────────────────────────────────────────────────

def availability_json(event, jinja_env):
    """GET /api/sponsorships/availability."""
    return _json(200, sp.availability(), event)


def book_json(event, jinja_env):
    """POST /api/sponsorships — hold a week for PayPal, or book it with a coupon."""
    data = _payload(event)

    # Per source IP: a failed coupon guess costs the same as a booking, which
    # makes walking the code space hopeless at 20 an hour.
    allowed, retry_after = check_and_record_write(_source_ip(event), 'sponsorship')
    if not allowed:
        return _json(429, {'error': f'Too many attempts — try again in {retry_after}s'},
                     event)

    try:
        code = str(data.get('coupon_code') or '').strip()
        if code:
            booking = sp.redeem_coupon_booking(data, code)
            return _json(201, {'week_id': booking['week_id'],
                               'status': booking['status']}, event)

        # Holds cost nothing, so they get a far smaller budget than coupon
        # attempts: at 20 an hour, a few IPs could keep every week held.
        allowed, retry_after = check_and_record_write(
            _source_ip(event), 'sponsorship_hold', limit=sp.HOLDS_PER_IP_PER_HOUR)
        if not allowed:
            return _json(429, {'error': f'Too many holds — try again in {retry_after}s'},
                         event)

        booking, _ = sp.create_hold(data)
    except sp.SponsorshipError as exc:
        return _error(exc, event)

    return _json(201, {
        'week_id': booking['week_id'],
        'booking_id': booking['booking_id'],
        'status': booking['status'],
        'hold_expires_at': booking['hold_expires_at'],
        'paypal_url': sp.paypal_form_url(),
        'paypal_fields': sp.paypal_fields(booking, _notify_url(event)),
    }, event)


def ipn(event, jinja_env):
    """POST /api/paypal/ipn — PayPal's server-to-server payment notice.

    Answers 200 regardless of outcome once the message has been handled;
    PayPal retries anything else for days, and none of our rejections (bad
    amount, duplicate, wrong receiver) would come out differently next time.
    A crash returns 500 so PayPal *does* retry it.
    """
    raw = event.get('body') or ''
    if event.get('isBase64Encoded'):
        # latin-1 maps bytes 1:1, so the echo back to PayPal is byte-exact.
        raw = base64.b64decode(raw).decode('latin-1')
    try:
        outcome = sp.handle_ipn(raw)
    except Exception:
        print(f'IPN handling failed: {traceback.format_exc()}')
        return {'statusCode': 500, 'headers': {}, 'body': ''}
    print(f'IPN outcome: {outcome}')
    return {'statusCode': 200, 'headers': {'Content-Type': 'text/plain'},
            'body': ''}


# ─── Admin ─────────────────────────────────────────────────────────

def _admin(handler):
    """Admin check + SponsorshipError → JSON, shared by every admin route."""
    def wrapped(event, jinja_env, *args):
        claims, err = _admin_check(event)
        if err:
            return err
        try:
            return handler(event, claims, *args)
        except sp.SponsorshipError as exc:
            return _error(exc, event)
    return wrapped


def _reviewer(claims):
    return claims.get('email') or 'admin'


@_admin
def list_json(event, claims):
    """GET /api/admin/sponsorships[?status=]."""
    status = (event.get('queryStringParameters') or {}).get('status')
    return _json(200, {'sponsorships': sp.list_sponsorships(status),
                       'price_usd': str(sp.PRICE_USD)}, event)


@_admin
def create_json(event, claims):
    """POST /api/admin/sponsorships — a comp or off-PayPal booking."""
    data = _payload(event)
    booking = sp.manual_booking(str(data.get('week_id') or ''), data,
                                _reviewer(claims),
                                approve=data.get('approve', True) is not False)
    return _json(201, {'sponsorship': booking}, event)


@_admin
def get_json(event, claims, week_id):
    """GET /api/admin/sponsorships/{week}."""
    booking = sp.get_sponsorship(week_id)
    if not booking:
        return _json(404, {'error': f'No booking for {week_id}'}, event)
    return _json(200, {'sponsorship': booking}, event)


@_admin
def update_json(event, claims, week_id):
    """PUT /api/admin/sponsorships/{week} — edit the copy."""
    return _json(200, {'sponsorship': sp.update_copy(week_id, _payload(event))},
                 event)


@_admin
def approve_json(event, claims, week_id):
    """POST /api/admin/sponsorships/{week}/approve."""
    return _json(200, {'sponsorship': sp.approve(week_id, _reviewer(claims))},
                 event)


@_admin
def reject_json(event, claims, week_id):
    """POST /api/admin/sponsorships/{week}/reject."""
    reason = str(_payload(event).get('reason') or '')
    return _json(200, {'sponsorship': sp.reject(week_id, _reviewer(claims),
                                                reason)}, event)


@_admin
def cancel_json(event, claims, week_id):
    """POST /api/admin/sponsorships/{week}/cancel — free it quietly."""
    return _json(200, {'sponsorship': sp.cancel(week_id, _reviewer(claims))},
                 event)


@_admin
def list_coupons_json(event, claims):
    """GET /api/admin/coupons."""
    return _json(200, {'coupons': sp.list_coupons()}, event)


@_admin
def mint_coupons_json(event, claims):
    """POST /api/admin/coupons — {count, note, expires_on}."""
    data = _payload(event)
    minted = sp.mint_coupons(data.get('count') or 1, data.get('note') or '',
                             data.get('expires_on') or None, _reviewer(claims))
    return _json(201, {'coupons': minted}, event)


@_admin
def revoke_coupon_json(event, claims, code):
    """DELETE /api/admin/coupons/{code}."""
    return _json(200, {'coupon': sp.revoke_coupon(code)}, event)
