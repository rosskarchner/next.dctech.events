"""
Weekly sponsorships, Daring Fireball style: one sponsor per ISO week, whose
one-line message runs beside the logo, atop the newsletter, on the week page,
and in a Monday intro / Thursday thank-you post.

Shared by the API Lambda (public booking, PayPal IPN, /edit admin) and the MCP
server, which is why it lives beside db.py and is copied into build/mcp/ the
same way. Everything here talks to the table through db._get_table(), so the
tests' FakeTable covers it.

Items:

  SPONSOR#<iso-week> / META            the booking for that week. Keying by
                                       week makes "one sponsor per week" a
                                       conditional put, not a query.
  SPONSOR#<iso-week> / PAYMENT#<txn>#<status>
                                       one per IPN message: idempotency and
                                       an audit trail. The site export reads
                                       META only, so these are never published.
  COUPON#<CODE> / META                 a single-use free week, minted by an
                                       admin.

Booking lifecycle:

  held ──IPN Completed──> paid ──approve──> approved      (shown on the site)
    │                      └──reject───> rejected         (refund by hand)
    └──IPN Pending──> payment_pending ──IPN Completed──> paid
                             └──IPN Failed/Denied──> cancelled

A week is free again once its booking is rejected, refunded or cancelled, or
while it is only `held` and the hold has lapsed.
"""

import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

import db

LOCAL_TZ = ZoneInfo('America/New_York')

PRICE_USD = Decimal(os.environ.get('SPONSOR_WEEK_PRICE_USD', '0') or '0')
PAYPAL_RECEIVER_EMAIL = os.environ.get('PAYPAL_RECEIVER_EMAIL', 'ross@karchner.com')
PAYPAL_SANDBOX = os.environ.get('PAYPAL_SANDBOX', '') == '1'
BASE_URL = os.environ.get('BASE_URL', 'https://dctech.events')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')

HORIZON_WEEKS = 26
HOLD_SECONDS = 30 * 60
# Per source IP. Enough to retry after cancelling at PayPal a few times; few
# enough that one address can't keep more than a couple of weeks held.
HOLDS_PER_IP_PER_HOUR = 4
NAME_MAX = 60
MESSAGE_MAX = 140

STATUSES = ('held', 'payment_pending', 'paid', 'approved',
            'rejected', 'refunded', 'cancelled')
FREEING_STATUSES = ('rejected', 'refunded', 'cancelled')
# Statuses an admin can still approve from. `held` is included so a comp or
# an off-PayPal payment can be pushed through by hand.
APPROVABLE_STATUSES = ('held', 'payment_pending', 'paid')

# Crockford base32 minus nothing ambiguous: no I, L, O or U.
COUPON_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
COUPON_LENGTH = 10

_WEEK_RE = re.compile(r'^(\d{4})-W(\d{2})$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')

# A week is free to (re)book if nothing is there, its booking was let go, or
# it is only an expired hold. The same condition guards the coupon
# transaction, so both booking paths agree on what "free" means.
_WEEK_FREE_CONDITION = (
    'attribute_not_exists(PK) OR #s IN (:rejected, :refunded, :cancelled) '
    'OR (#s = :held AND hold_expires_at < :now)'
)


class SponsorshipError(ValueError):
    """A caller-facing failure: the message is safe to show the booker."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


# ─── Weeks ─────────────────────────────────────────────────────────

def week_id_for(d):
    iso_year, iso_week, _ = d.isocalendar()
    return f'{iso_year}-W{iso_week:02d}'


def week_start(week_id):
    """The Monday of `week_id` ('2026-W45'), or None if it is malformed."""
    m = _WEEK_RE.match(str(week_id or ''))
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None


def booking_cutoff(week_id):
    """Bookings for a week close at the end of the Thursday before it.

    That leaves Friday to Sunday to review copy before the Monday build, the
    newsletter, and the intro post all pick it up.
    """
    monday = week_start(week_id)
    friday_before = monday - timedelta(days=3)
    return datetime.combine(friday_before, datetime.min.time(), tzinfo=LOCAL_TZ)


def _local_now(now=None):
    if now is None:
        return datetime.now(LOCAL_TZ)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, LOCAL_TZ)
    return now.astimezone(LOCAL_TZ)


def bookable_weeks(now=None):
    """Week ids still open for booking, soonest first."""
    local_now = _local_now(now)
    today = local_now.date()
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(HORIZON_WEEKS + 1):
        wid = week_id_for(this_monday + timedelta(weeks=i))
        if local_now < booking_cutoff(wid):
            weeks.append(wid)
    return weeks


def _is_bookable(week_id, now=None):
    return week_id in bookable_weeks(now)


# ─── Reading ───────────────────────────────────────────────────────

def _key(week_id):
    return {'PK': f'SPONSOR#{week_id}', 'SK': 'META'}


def _epoch(now=None):
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        return int(now.timestamp())
    return int(now)


def _iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _to_dict(item):
    if not item:
        return None
    out = {k: db._to_plain(v) for k, v in item.items()
           if k not in ('PK', 'SK')}
    return out


def get_sponsorship(week_id):
    item = db._get_table().get_item(Key=_key(week_id)).get('Item')
    return _to_dict(item)


def is_week_free(item, now=None):
    if not item:
        return True
    status = item.get('status')
    if status in FREEING_STATUSES:
        return True
    return status == 'held' and int(item.get('hold_expires_at', 0)) < _epoch(now)


def list_sponsorships(status=None):
    from boto3.dynamodb.conditions import Attr
    items = db._scan_all(
        db._get_table(),
        FilterExpression=Attr('PK').begins_with('SPONSOR#') & Attr('SK').eq('META'),
    )
    rows = [_to_dict(i) for i in items]
    if status:
        rows = [r for r in rows if r.get('status') == status]
    rows.sort(key=lambda r: r.get('week_id', ''))
    return rows


def availability(now=None):
    """Bookable weeks with whether each is still free. Public: no names."""
    taken = {r['week_id'] for r in list_sponsorships()
             if not is_week_free(r, now)}
    return {
        'price_usd': str(PRICE_USD),
        'weeks': [
            {'week_id': wid,
             'week_start': week_start(wid).isoformat(),
             'available': wid not in taken}
            for wid in bookable_weeks(now)
        ],
    }


# ─── Validation ────────────────────────────────────────────────────

def _clean_text(value):
    return ' '.join(_CONTROL_RE.sub(' ', str(value or '')).split())


def validate_copy(data, require_email=True):
    """Normalize sponsor copy. Raises SponsorshipError on bad input.

    Plain text only — templates escape it, so markup here would show up
    literally rather than render.
    """
    name = _clean_text(data.get('name'))
    message = _clean_text(data.get('message'))
    url = str(data.get('url') or '').strip()
    email = str(data.get('contact_email') or '').strip().lower()

    if not name:
        raise SponsorshipError('Sponsor name is required')
    if len(name) > NAME_MAX:
        raise SponsorshipError(f'Sponsor name must be {NAME_MAX} characters or fewer')
    if not message:
        raise SponsorshipError('Message is required')
    if len(message) > MESSAGE_MAX:
        raise SponsorshipError(f'Message must be {MESSAGE_MAX} characters or fewer')
    # No quotes, angle brackets or backslashes: the URL lands in hrefs and
    # in Markdown link targets, and a real one never needs them unescaped.
    if (not re.match(r'^https://[^\s/<>"\'\\]+\.[^\s<>"\'\\]+$', url)
            or len(url) > 500):
        raise SponsorshipError('Link must be a full https:// URL')
    if require_email and not _EMAIL_RE.match(email):
        raise SponsorshipError('A valid contact email is required')
    return {'name': name, 'message': message, 'url': url,
            'contact_email': email}


def _validate_week(week_id, now=None, enforce_window=True):
    if not week_start(week_id):
        raise SponsorshipError('Week must look like 2026-W45')
    if enforce_window and not _is_bookable(week_id, now):
        raise SponsorshipError('That week is not open for booking')


# ─── Booking ───────────────────────────────────────────────────────

def _free_condition_values(now):
    return {
        ':rejected': 'rejected', ':refunded': 'refunded',
        ':cancelled': 'cancelled', ':held': 'held', ':now': _epoch(now),
    }


def _new_booking(week_id, copy, status, payment_method, now=None, **extra):
    stamp = _iso_now()
    return {
        **_key(week_id),
        'week_id': week_id,
        'booking_id': uuid.uuid4().hex,
        **copy,
        'status': status,
        'payment_method': payment_method,
        'price_usd': str(PRICE_USD),
        'created_at': stamp,
        'updated_at': stamp,
        **extra,
    }


def create_hold(data, now=None):
    """Hold a week for HOLD_SECONDS while the booker pays at PayPal.

    `replace_booking_id` lets a booker who cancelled at PayPal replace their
    own live hold. It must be the booking_id this function handed them, not
    their email: the email is unverified and often guessable, and replacing
    a hold mid-payment turns the real booker's payment into a conflict.

    Returns (booking, paypal_fields). Raises SponsorshipError(409) if the
    week is taken.
    """
    week_id = str(data.get('week_id') or '').strip()
    _validate_week(week_id, now)
    copy = validate_copy(data)
    if PRICE_USD <= 0:
        raise SponsorshipError('Sponsorships are not on sale right now', 503)

    item = _new_booking(week_id, copy, 'held', 'paypal', now,
                        hold_expires_at=_epoch(now) + HOLD_SECONDS)
    condition = _WEEK_FREE_CONDITION
    values = _free_condition_values(now)
    replace_id = str(data.get('replace_booking_id') or '').strip()
    if replace_id:
        condition += ' OR (#s = :held AND booking_id = :replace)'
        values[':replace'] = replace_id
    try:
        db._get_table().put_item(
            Item=item,
            ConditionExpression=condition,
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise SponsorshipError('Sorry, that week was just booked', 409)
        raise
    return _to_dict(item), paypal_fields(item)


def paypal_form_url():
    host = 'www.sandbox.paypal.com' if PAYPAL_SANDBOX else 'www.paypal.com'
    return f'https://{host}/cgi-bin/webscr'


def paypal_fields(booking, notify_url=None):
    """Fields for a PayPal Payments Standard "Buy Now" (_xclick) form.

    `custom` carries week and booking id so the IPN can find the exact hold
    it pays for — the week alone is not enough once a lapsed hold has been
    re-booked by someone else.
    """
    monday = week_start(booking['week_id'])
    fields = {
        'cmd': '_xclick',
        'business': PAYPAL_RECEIVER_EMAIL,
        'item_name': (f'DC Tech Events sponsorship, week of '
                      f'{monday.strftime("%b %-d, %Y")}'),
        'item_number': booking['week_id'],
        'amount': booking['price_usd'],
        'currency_code': 'USD',
        'no_shipping': '1',
        'custom': f"{booking['week_id']}|{booking['booking_id']}",
        'invoice': booking['booking_id'],
        'return': (f'{BASE_URL}/edit/sponsor.html?status=returned'
                   f"&week={booking['week_id']}"),
        'cancel_return': (f'{BASE_URL}/edit/sponsor.html?status=cancelled'
                          f"&week={booking['week_id']}"),
    }
    if notify_url:
        fields['notify_url'] = notify_url
    return fields


def redeem_coupon_booking(data, code, now=None):
    """Book a week with a coupon instead of a payment.

    One transaction: claim the coupon and take the week, or neither. The
    booking lands as `paid` — it still waits for review like any other.
    """
    week_id = str(data.get('week_id') or '').strip()
    _validate_week(week_id, now)
    copy = validate_copy(data)
    code = normalize_coupon(code)
    if not code:
        raise SponsorshipError('That coupon code is not valid')

    coupon = _get_coupon(code)
    if (not coupon or coupon.get('revoked') or coupon.get('redeemed_week')
            or _coupon_expired(coupon, now)):
        raise SponsorshipError('That coupon code is not valid')

    item = _new_booking(week_id, copy, 'paid', 'coupon', now,
                        coupon_code=code)
    table = db._get_table()
    try:
        table.meta.client.transact_write_items(TransactItems=[
            {'Update': {
                'TableName': table.name,
                'Key': {'PK': f'COUPON#{code}', 'SK': 'META'},
                'UpdateExpression': 'SET redeemed_week = :w, redeemed_at = :t',
                'ConditionExpression': ('attribute_exists(PK) AND '
                                        'attribute_not_exists(redeemed_week) AND '
                                        'attribute_not_exists(revoked)'),
                'ExpressionAttributeValues': {':w': week_id, ':t': _iso_now()},
            }},
            {'Put': {
                'TableName': table.name,
                'Item': item,
                'ConditionExpression': _WEEK_FREE_CONDITION,
                'ExpressionAttributeNames': {'#s': 'status'},
                'ExpressionAttributeValues': _free_condition_values(now),
            }},
        ])
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'TransactionCanceledException':
            raise
        reasons = [r.get('Code') for r in
                   exc.response.get('CancellationReasons') or []]
        if len(reasons) > 1 and reasons[1] == 'ConditionalCheckFailed':
            raise SponsorshipError('Sorry, that week was just booked', 409)
        raise SponsorshipError('That coupon code is not valid')

    booking = _to_dict(item)
    _notify_admin_paid(booking)
    _email_sponsor_received(booking)
    return booking


def manual_booking(week_id, data, reviewer, approve=True, now=None):
    """An admin booking: a comp, or a payment taken outside PayPal.

    Skips the booking window (an admin may fill this week), but never takes
    a week someone else holds.
    """
    _validate_week(week_id, now, enforce_window=False)
    copy = validate_copy(data, require_email=False)
    item = _new_booking(week_id, copy, 'approved' if approve else 'paid',
                        'manual', now, reviewed_by=reviewer)
    try:
        db._get_table().put_item(
            Item=item,
            ConditionExpression=_WEEK_FREE_CONDITION,
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=_free_condition_values(now),
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise SponsorshipError(f'{week_id} is already booked', 409)
        raise
    return _to_dict(item)


# ─── Review ────────────────────────────────────────────────────────

def _set_status(week_id, status, allowed_from, extra=None):
    """Move a booking to `status`, only from one of `allowed_from`."""
    extra = extra or {}
    names = {'#s': 'status'}
    values = {':s': status, ':u': _iso_now()}
    sets = ['#s = :s', 'updated_at = :u']
    for i, (k, v) in enumerate(extra.items()):
        names[f'#e{i}'] = k
        values[f':e{i}'] = v
        sets.append(f'#e{i} = :e{i}')
    allowed = {f':from{i}': s for i, s in enumerate(allowed_from)}
    values.update(allowed)
    try:
        resp = db._get_table().update_item(
            Key=_key(week_id),
            UpdateExpression='SET ' + ', '.join(sets),
            ConditionExpression=(f"attribute_exists(PK) AND #s IN "
                                 f"({', '.join(allowed)})"),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues='ALL_NEW',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            current = get_sponsorship(week_id)
            if not current:
                raise SponsorshipError(f'No booking for {week_id}', 404)
            raise SponsorshipError(
                f"{week_id} is {current.get('status')}, not one of "
                f"{', '.join(allowed_from)}", 409)
        raise
    return _to_dict(resp.get('Attributes'))


def approve(week_id, reviewer, edits=None):
    if edits:
        update_copy(week_id, edits)
    booking = _set_status(week_id, 'approved', APPROVABLE_STATUSES,
                          {'reviewed_by': reviewer or 'admin'})
    _email_sponsor(booking, 'approved')
    return booking


def reject(week_id, reviewer, reason=''):
    """Turn a booking down and free its week. Refunds are done by hand."""
    booking = _set_status(
        week_id, 'rejected', APPROVABLE_STATUSES + ('approved',),
        {'reviewed_by': reviewer or 'admin', 'reject_reason': reason or ''})
    _release_coupon(booking)
    _email_sponsor(booking, 'rejected')
    return booking


def cancel(week_id, reviewer):
    """Free a week without telling the booker (e.g. an abandoned hold)."""
    booking = _set_status(
        week_id, 'cancelled', APPROVABLE_STATUSES + ('approved',),
        {'reviewed_by': reviewer or 'admin'})
    _release_coupon(booking)
    return booking


def update_copy(week_id, fields):
    """Edit name/message/url/contact_email on an existing booking."""
    current = get_sponsorship(week_id)
    if not current:
        raise SponsorshipError(f'No booking for {week_id}', 404)
    merged = {k: fields.get(k, current.get(k))
              for k in ('name', 'message', 'url', 'contact_email')}
    copy = validate_copy(merged, require_email=False)
    names, values, sets = {}, {':u': _iso_now()}, ['updated_at = :u']
    for i, (k, v) in enumerate(copy.items()):
        names[f'#c{i}'] = k
        values[f':c{i}'] = v
        sets.append(f'#c{i} = :c{i}')
    resp = db._get_table().update_item(
        Key=_key(week_id),
        UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW',
    )
    return _to_dict(resp.get('Attributes'))


# ─── Coupons ───────────────────────────────────────────────────────

def normalize_coupon(code):
    code = re.sub(r'[\s-]', '', str(code or '')).upper()
    # Crockford: read the look-alikes as the digits they resemble.
    code = code.replace('O', '0').replace('I', '1').replace('L', '1')
    if len(code) != COUPON_LENGTH or any(c not in COUPON_ALPHABET for c in code):
        return ''
    return code


def _get_coupon(code):
    item = db._get_table().get_item(
        Key={'PK': f'COUPON#{code}', 'SK': 'META'}).get('Item')
    return _to_dict(item)


def _coupon_expired(coupon, now=None):
    expires_on = coupon.get('expires_on')
    return bool(expires_on) and _local_now(now).date().isoformat() > expires_on


def mint_coupons(count, note='', expires_on=None, created_by='admin'):
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise SponsorshipError('count must be a number')
    if not 1 <= count <= 50:
        raise SponsorshipError('Mint between 1 and 50 coupons at a time')
    if expires_on:
        try:
            date.fromisoformat(expires_on)
        except ValueError:
            raise SponsorshipError('expires_on must be YYYY-MM-DD')
    table = db._get_table()
    minted = []
    while len(minted) < count:
        code = ''.join(secrets.choice(COUPON_ALPHABET)
                       for _ in range(COUPON_LENGTH))
        item = {'PK': f'COUPON#{code}', 'SK': 'META', 'code': code,
                'note': str(note or ''), 'created_at': _iso_now(),
                'created_by': created_by}
        if expires_on:
            item['expires_on'] = expires_on
        try:
            table.put_item(Item=item,
                           ConditionExpression='attribute_not_exists(PK)')
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                continue  # 32^10 makes this vanishingly rare; just redraw.
            raise
        minted.append(_to_dict(item))
    return minted


def list_coupons():
    from boto3.dynamodb.conditions import Attr
    items = db._scan_all(
        db._get_table(),
        FilterExpression=Attr('PK').begins_with('COUPON#') & Attr('SK').eq('META'),
    )
    rows = [_to_dict(i) for i in items]
    rows.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    return rows


def revoke_coupon(code):
    code = normalize_coupon(code)
    if not code:
        raise SponsorshipError('Not a coupon code', 404)
    try:
        resp = db._get_table().update_item(
            Key={'PK': f'COUPON#{code}', 'SK': 'META'},
            UpdateExpression='SET revoked = :t',
            ConditionExpression='attribute_exists(PK)',
            ExpressionAttributeValues={':t': _iso_now()},
            ReturnValues='ALL_NEW',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise SponsorshipError(f'No coupon {code}', 404)
        raise
    return _to_dict(resp.get('Attributes'))


def _release_coupon(booking):
    """Give a coupon back when the booking it paid for is let go."""
    code = (booking or {}).get('coupon_code')
    if not code:
        return
    try:
        db._get_table().update_item(
            Key={'PK': f'COUPON#{code}', 'SK': 'META'},
            UpdateExpression='REMOVE redeemed_week, redeemed_at',
            ConditionExpression='redeemed_week = :w',
            ExpressionAttributeValues={':w': booking['week_id']},
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


# ─── PayPal IPN ────────────────────────────────────────────────────

def _ipn_verify_url():
    host = 'ipnpb.sandbox.paypal.com' if PAYPAL_SANDBOX else 'ipnpb.paypal.com'
    return f'https://{host}/cgi-bin/webscr'


def verify_with_paypal(raw_body):
    """Echo the message back to PayPal; only 'VERIFIED' means it came from them."""
    req = Request(
        _ipn_verify_url(),
        data=b'cmd=_notify-validate&' + raw_body.encode('latin-1', 'replace'),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'dctech-events-ipn/1.0'},
    )
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode('utf-8', 'replace').strip() == 'VERIFIED'


def _parse_ipn(raw_body):
    return dict(parse_qsl(raw_body, keep_blank_values=True,
                          encoding='latin-1'))


def _parse_custom(custom):
    week_id, _, booking_id = str(custom or '').partition('|')
    if not week_start(week_id) or not booking_id:
        return None, None
    return week_id, booking_id


def _record_payment(week_id, params):
    """Store an IPN once. Returns False if this exact message was seen."""
    txn_id = params.get('txn_id', '')
    status = params.get('payment_status', '')
    item = {
        'PK': f'SPONSOR#{week_id}', 'SK': f'PAYMENT#{txn_id}#{status}',
        'received_at': _iso_now(),
        **{k: params.get(k, '') for k in (
            'txn_id', 'parent_txn_id', 'payment_status', 'mc_gross',
            'mc_currency', 'receiver_email', 'payer_email', 'custom',
            'pending_reason', 'reason_code')},
    }
    try:
        db._get_table().put_item(Item=item,
                                 ConditionExpression='attribute_not_exists(PK)')
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise
    return True


def handle_ipn(raw_body, verify=None):
    """Process one IPN message. Returns a short outcome string for logs/tests.

    Never trusts the message until PayPal has verified it, and never moves a
    booking to `paid` unless the money went to our address, in USD, for the
    price the booking was quoted.
    """
    verify = verify or verify_with_paypal
    if not verify(raw_body):
        print('IPN failed verification')
        return 'invalid'

    params = _parse_ipn(raw_body)
    txn_id = params.get('txn_id')
    status = params.get('payment_status', '')
    week_id, booking_id = _parse_custom(params.get('custom'))
    if not txn_id or not week_id:
        _notify_admin('IPN with no booking attached', params)
        return 'unmatched'

    if not _record_payment(week_id, params):
        return 'duplicate'

    receiver = (params.get('receiver_email') or params.get('business') or '').lower()
    if receiver != PAYPAL_RECEIVER_EMAIL.lower():
        _notify_admin('IPN for a different receiver', params)
        return 'wrong_receiver'

    booking = get_sponsorship(week_id)
    ours = bool(booking) and booking.get('booking_id') == booking_id

    if status in ('Refunded', 'Reversed', 'Canceled_Reversal'):
        if ours and status != 'Canceled_Reversal':
            try:
                booking = _set_status(week_id, 'refunded',
                                      APPROVABLE_STATUSES + ('approved',))
                _release_coupon(booking)
            except SponsorshipError:
                pass
        _notify_admin(f'Payment {status.lower()} for {week_id}', params, booking)
        return status.lower()

    if status in ('Failed', 'Denied', 'Voided', 'Expired'):
        # A pending payment that never cleared (a bounced eCheck, a declined
        # review): payment_pending never lapses on its own, so free the week.
        if (ours and booking.get('status') == 'payment_pending'
                and booking.get('txn_id') == txn_id):
            try:
                booking = _set_status(week_id, 'cancelled', ('payment_pending',))
            except SponsorshipError:
                pass
        _notify_admin(f'Payment {status.lower()} for {week_id}', params, booking)
        return 'payment_failed'

    if status not in ('Pending', 'Completed'):
        _notify_admin(f'IPN status {status} for {week_id}', params, booking)
        return 'ignored'

    # Before Pending as well as Completed: the PayPal form is in the booker's
    # hands, and payment_pending holds a week indefinitely, so a one-cent
    # payment in a currency we don't hold (left Pending by PayPal) must not
    # be able to lock a week.
    try:
        gross = Decimal(params.get('mc_gross', ''))
    except InvalidOperation:
        gross = None
    expected = Decimal(str(booking['price_usd'])) if ours else PRICE_USD
    if params.get('mc_currency') != 'USD' or gross != expected:
        _notify_admin(f'Wrong amount for {week_id} — refund or follow up',
                      params, booking)
        return 'wrong_amount'

    if status == 'Pending':
        if ours and booking.get('status') == 'held':
            try:
                _set_status(week_id, 'payment_pending', ('held',),
                            {'txn_id': txn_id})
            except SponsorshipError:
                pass
        _notify_admin(f'Payment pending for {week_id}', params, booking)
        return 'pending'

    if not ours:
        _notify_admin(f'CONFLICT: payment for {week_id} but the hold was lost '
                      f'— refund or move to another week', params, booking)
        return 'conflict'

    if booking.get('status') in ('paid', 'approved'):
        return 'already_paid'

    try:
        booking = _set_status(
            week_id, 'paid', ('held', 'payment_pending'),
            {'txn_id': txn_id, 'payer_email': params.get('payer_email', '')})
    except SponsorshipError:
        _notify_admin(f'CONFLICT: payment for {week_id} arrived after the '
                      f'booking was {booking.get("status")}', params, booking)
        return 'conflict'

    _notify_admin_paid(booking)
    _email_sponsor_received(booking)
    return 'paid'


# ─── Email ─────────────────────────────────────────────────────────

ADMIN_PAGE_URL = f'{BASE_URL}/edit/sponsorships.html'


def _send(to, subject, text, reply_to=None):
    """Plain-text mail. Never raises: mail trouble must not fail a booking."""
    try:
        import boto3
        boto3.client('sesv2').send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[reply_to or REPLY_TO_EMAIL],
            Destination={'ToAddresses': [to]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200]},
                'Body': {
                    'Text': {'Data': text},
                    'Html': {'Data': '<pre style="font-family:inherit;'
                                     'white-space:pre-wrap">'
                                     f'{html_escape(text)}</pre>'},
                },
            }},
        )
    except Exception as exc:
        print(f'Sponsorship email to {to} failed: {exc}')


def _describe(booking):
    if not booking:
        return '(no booking on file)'
    return '\n'.join(f'{k}: {booking.get(k, "")}' for k in (
        'week_id', 'status', 'name', 'message', 'url', 'contact_email',
        'payment_method', 'coupon_code', 'price_usd', 'txn_id', 'booking_id'))


def _notify_admin(subject, params, booking=None):
    ipn = '\n'.join(f'{k}: {v}' for k, v in sorted((params or {}).items()))
    _send(ADMIN_EMAIL, f'[dctech.events] Sponsorship: {subject}',
          f'{subject}\n\nBooking:\n{_describe(booking)}\n\nIPN:\n{ipn}\n\n'
          f'{ADMIN_PAGE_URL}\n')


def _notify_admin_paid(booking):
    how = 'a coupon' if booking.get('payment_method') == 'coupon' else 'PayPal'
    _send(ADMIN_EMAIL,
          f"[dctech.events] Sponsorship to review: {booking['week_id']} — "
          f"{booking.get('name')}",
          f'Booked with {how}; waiting for your review.\n\n'
          f'{_describe(booking)}\n\n{ADMIN_PAGE_URL}\n',
          reply_to=booking.get('contact_email') or None)


def _week_phrase(week_id):
    return f'the week of {week_start(week_id).strftime("%B %-d, %Y")}'


def _email_sponsor_received(booking):
    if not booking.get('contact_email'):
        return
    _send(booking['contact_email'],
          'Your DC Tech Events sponsorship is booked',
          f"Thanks for sponsoring DC Tech Events for "
          f"{_week_phrase(booking['week_id'])}.\n\n"
          f"We'll review your message and confirm by email before your week "
          f"starts:\n\n  {booking['name']}: {booking['message']}\n  "
          f"{booking['url']}\n\nQuestions? Just reply.\n")


def _email_sponsor(booking, outcome):
    if not booking or not booking.get('contact_email'):
        return
    phrase = _week_phrase(booking['week_id'])
    if outcome == 'approved':
        _send(booking['contact_email'],
              'Your DC Tech Events sponsorship is confirmed',
              f"You're confirmed as the DC Tech Events sponsor for {phrase}. "
              f"Your message will run at the top of every page, at the top "
              f"of Monday's newsletter, and in our Monday and Thursday "
              f"posts:\n\n  {booking['name']}: {booking['message']}\n  "
              f"{booking['url']}\n\nThank you!\n")
    elif outcome == 'rejected':
        refund = ('' if booking.get('payment_method') != 'paypal'
                  else ' Your PayPal payment will be refunded in full.')
        _send(booking['contact_email'],
              'About your DC Tech Events sponsorship',
              f"We weren't able to run your sponsorship for {phrase}."
              f"{refund}\n\n{booking.get('reject_reason') or ''}\n\n"
              f"Reply to this email if you'd like to talk it over or pick "
              f"another week.\n")

