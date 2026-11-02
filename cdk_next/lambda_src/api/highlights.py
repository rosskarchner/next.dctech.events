"""
Highlighted events: a single occurrence shown in a "Highlighted" box at the
top of the homepage, category, month and week pages, and in the Monday
newsletter, for the WINDOW_DAYS before it happens.

Two kinds, both labelled on the site so readers can tell them apart:

  editor   an admin's free pick ("Editor's pick"), written straight to active
  paid     bought for PRICE_USD by anyone ("Paid"); live as soon as PayPal
           confirms the payment — no review queue, refunds by hand

Shared by the API Lambda (public checkout, the PayPal IPN, /edit admin) and
the MCP server, like sponsorships.py, and talks to the table only through
db._get_table() so the tests' FakeTable covers it.

Items:

  HIGHLIGHT#<target_key> / META       one per highlighted occurrence. The key
                                      is the lock: a second buyer's hold is a
                                      conditional put that fails while the
                                      first is live.
  HIGHLIGHT#<target_key> / PAYMENT#<txn>#<status>
                                      one per IPN message (paypal.py).
  HIGHLIGHT#<target_key> / RESOLVED   build-maintained matching state
                                      (site_generator/reconcile_highlights.py,
                                      which also sends the lost-event emails).
                                      Its own SK so the build trigger, which
                                      listens to META only, never re-fires on it.

target_key is `g-<guid>` for a stored event (iCal, submitted, manual) and
`r-<recurring_slug>-<YYYY-MM-DD>` for one occurrence of a recurring series:
recurring guids are recomputed every build, so slug + date is the identity
that survives, the same reasoning pipeline.py uses for per-date overrides.

Lifecycle:

  held ──IPN Completed──> active ──revoke──> revoked
    │                       └──IPN Refunded/Reversed──> refunded
    └──IPN Pending──> payment_pending ──IPN Completed──> active
                            └──IPN Failed/Denied──> cancelled

A target is free again once its row is revoked, refunded or cancelled, or
while it is only `held` and the hold has lapsed.
"""

import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

import db
import paypal
from paypal import BASE_URL, paypal_form_url, verify_with_paypal  # noqa: F401
from paypal import iso_now as _iso_now, send as _send

LOCAL_TZ = ZoneInfo('America/New_York')

PRICE_USD = Decimal(os.environ.get('HIGHLIGHT_PRICE_USD', '0') or '0')
ADMIN_EMAIL = paypal.ADMIN_EMAIL
SITE_BUCKET = os.environ.get('SITE_BUCKET', '')
EVENTS_KEY = 'events.json'

# Matches calgen's UPCOMING_WINDOW_DAYS: the homepage and the newsletter look
# this far ahead, so a highlight shows for exactly as long as its event could
# be listed there anyway.
WINDOW_DAYS = 21
HOLD_SECONDS = 30 * 60
# Per source IP, like sponsorship holds: enough to retry after cancelling at
# PayPal a few times, too few to hold a page of events hostage.
HOLDS_PER_IP_PER_HOUR = 4
NOTE_MAX = 200

KINDS = ('paid', 'editor')
STATUSES = ('held', 'payment_pending', 'active',
            'revoked', 'refunded', 'cancelled')
FREEING_STATUSES = ('revoked', 'refunded', 'cancelled')
LIVE_STATUSES = ('held', 'payment_pending', 'active')
LABELS = {'paid': 'Paid', 'editor': "Editor's pick"}

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_KEY_RE = re.compile(r'^(g-[0-9A-Za-z_-]+|r-[0-9a-z-]+-\d{4}-\d{2}-\d{2})$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')

# Same shape as sponsorships._WEEK_FREE_CONDITION, with revoked in place of
# rejected: nothing there, let go, or a lapsed hold.
_FREE_CONDITION = (
    'attribute_not_exists(PK) OR #s IN (:revoked, :refunded, :cancelled) '
    'OR (#s = :held AND hold_expires_at < :now)'
)


class HighlightError(ValueError):
    """A caller-facing failure: the message is safe to show the buyer."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


# ─── Time ──────────────────────────────────────────────────────────

def _epoch(now=None):
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        return int(now.timestamp())
    return int(now)


def _local_today(now=None):
    if now is None:
        return datetime.now(LOCAL_TZ).date()
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, LOCAL_TZ).date()
    return now.astimezone(LOCAL_TZ).date()


def window_start(event_date):
    """The first day a highlight for an event on `event_date` shows."""
    return event_date - timedelta(days=WINDOW_DAYS)


def mondays_between(first, last):
    """Mondays in [first, last], inclusive."""
    d = first + timedelta(days=(7 - first.weekday()) % 7)
    out = []
    while d <= last:
        out.append(d)
        d += timedelta(days=7)
    return out


# ─── Events ────────────────────────────────────────────────────────

def target_key_for(event):
    """The stable identity of one occurrence. See the module docstring."""
    slug = event.get('recurring_slug')
    if slug:
        return f"r-{slug}-{event.get('date')}"
    return f"g-{event.get('guid')}"


def _load_events_from_s3():
    """The latest built events.json, straight from the site bucket.

    S3 rather than https://dctech.events/events.json: nothing between the
    build and this read can serve a stale copy, and the API needs no calgen.
    """
    import boto3
    if not SITE_BUCKET:
        raise HighlightError('Highlights are not configured yet', 503)
    body = boto3.client('s3').get_object(
        Bucket=SITE_BUCKET, Key=EVENTS_KEY)['Body'].read()
    return json.loads(body)


# Swapped out by tests.
_events_loader = _load_events_from_s3


def find_event(guid):
    guid = str(guid or '').strip()
    if not guid or not re.match(r'^[0-9A-Za-z_-]{1,100}$', guid):
        raise HighlightError('That is not an event id', 404)
    for event in _events_loader():
        if event.get('guid') == guid:
            return event
    raise HighlightError("That event isn't on the calendar any more", 404)


def is_in_person(event):
    # Mirrors calgen's is_virtual_event: only an explicit 'virtual' is
    # virtual; manual events often carry no location_type at all.
    return event.get('location_type') != 'virtual'


def _event_date(event):
    try:
        return date.fromisoformat(str(event.get('date') or ''))
    except ValueError:
        return None


def last_day(event):
    """The last day an event runs: its end_date when that is a real date
    after its start, otherwise its start (calgen's event_last_day)."""
    start = _event_date(event)
    try:
        end = date.fromisoformat(str(event.get('end_date') or ''))
    except ValueError:
        return start
    return end if start and end > start else start


def _summary(event):
    return {k: event.get(k) for k in (
        'guid', 'title', 'date', 'end_date', 'time', 'url', 'group',
        'location', 'recurring_slug')}


def _snapshot(event):
    """What a highlight remembers about its event, for matching and email."""
    snap = {k: str(event.get(k) or '') for k in (
        'title', 'date', 'end_date', 'time', 'url', 'group', 'guid',
        'recurring_slug')}
    snap['title'] = snap['title'][:300]
    return snap


# ─── Reading ───────────────────────────────────────────────────────

def _key(target_key):
    return {'PK': f'HIGHLIGHT#{target_key}', 'SK': 'META'}


def _to_dict(item):
    if not item:
        return None
    return {k: db._to_plain(v) for k, v in item.items() if k not in ('PK', 'SK')}


def get_highlight(target_key):
    item = db._get_table().get_item(Key=_key(target_key)).get('Item')
    return _to_dict(item)


def is_free(item, now=None):
    if not item:
        return True
    status = item.get('status')
    if status in FREEING_STATUSES:
        return True
    return status == 'held' and int(item.get('hold_expires_at', 0)) < _epoch(now)


def list_highlights(status=None):
    from boto3.dynamodb.conditions import Attr
    items = db._scan_all(
        db._get_table(),
        FilterExpression=Attr('PK').begins_with('HIGHLIGHT#') & Attr('SK').eq('META'),
    )
    rows = [_to_dict(i) for i in items]
    if status:
        rows = [r for r in rows if r.get('status') == status]
    rows.sort(key=lambda r: (r.get('date', ''), r.get('time', '')))
    return rows


def _eligibility(event, now=None):
    """(eligible, reason) for highlighting `event` at all, by anyone."""
    today = _local_today(now)
    event_date = _event_date(event)
    if not event_date:
        return False, 'This event has no date'
    # A multi-day event can still be highlighted while it is under way.
    if last_day(event) < today:
        return False, 'This event has already happened'
    if not is_in_person(event):
        return False, 'Only in-person events can be highlighted'
    return True, ''


def quote(guid, now=None):
    """Everything the checkout page shows before the buyer commits."""
    event = find_event(guid)
    key = target_key_for(event)
    eligible, reason = _eligibility(event, now)
    existing = get_highlight(key)
    if eligible and not is_free(existing, now):
        eligible = False
        reason = ('This event is already highlighted'
                  if existing.get('status') == 'active'
                  else 'Someone is checking out this event right now — try again in a while')

    today = _local_today(now)
    event_date = _event_date(event)
    start = end = days = newsletters = None
    if event_date:
        start = max(today, window_start(event_date))
        # Shows through the event's last day, not just its first.
        end = max(last_day(event), start)
        days = max(0, (end - start).days + 1)
        # The newsletter goes out Monday morning, so today's has already
        # been sent: count Mondays strictly after today.
        newsletters = len(mondays_between(
            max(start, today + timedelta(days=1)), end))
    return {
        'event': _summary(event),
        'target_key': key,
        'eligible': eligible,
        'reason': reason,
        'price_usd': str(PRICE_USD),
        'start_date': start.isoformat() if start else None,
        'end_date': end.isoformat() if end else None,
        'days': days,
        'newsletters': newsletters,
    }


# ─── Writing ───────────────────────────────────────────────────────

def _free_condition_values(now):
    return {':revoked': 'revoked', ':refunded': 'refunded',
            ':cancelled': 'cancelled', ':held': 'held', ':now': _epoch(now)}


def _new_item(event, kind, status, now=None, **extra):
    stamp = _iso_now()
    key = target_key_for(event)
    return {
        **_key(key),
        'target_key': key,
        'booking_id': uuid.uuid4().hex,
        'kind': kind,
        'status': status,
        'price_usd': str(PRICE_USD) if kind == 'paid' else '0',
        'created_at': stamp,
        'updated_at': stamp,
        **_snapshot(event),
        **extra,
    }


def create_hold(data, now=None):
    """Hold an event for HOLD_SECONDS while the buyer pays at PayPal.

    `replace_booking_id` lets a buyer who cancelled at PayPal retry over
    their own live hold; it must be the booking_id handed back by the last
    call, never just an email (see sponsorships.create_hold).

    Returns (item, quote). Raises HighlightError(409) if it's taken.
    """
    email = str(data.get('contact_email') or '').strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HighlightError('A valid contact email is required')
    if PRICE_USD <= 0:
        raise HighlightError('Highlights are not on sale right now', 503)

    event = find_event(data.get('guid'))
    eligible, reason = _eligibility(event, now)
    if not eligible:
        raise HighlightError(reason)

    item = _new_item(event, 'paid', 'held', now, contact_email=email,
                     hold_expires_at=_epoch(now) + HOLD_SECONDS)
    condition = _FREE_CONDITION
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
            raise HighlightError('Sorry, this event was just highlighted', 409)
        raise
    return _to_dict(item)


def paypal_fields(item, notify_url=None):
    guid = item.get('guid', '')
    title = item.get('title', '')
    label = f'DC Tech Events highlight: {title}'
    if len(label) > 120:
        label = label[:117] + '...'
    return paypal.buy_now_fields(
        item_name=label,
        item_number=item['target_key'][:127],
        amount=item['price_usd'],
        custom=f"highlight|{item['target_key']}|{item['booking_id']}",
        invoice=f"hl-{item['booking_id']}",
        return_url=f'{BASE_URL}/edit/highlight.html?status=returned&guid={guid}',
        cancel_url=f'{BASE_URL}/edit/highlight.html?status=cancelled&guid={guid}',
        notify_url=notify_url,
    )


def add_editor_pick(guid, reviewer, note='', now=None):
    """Highlight an event for free. Never displaces a live paid highlight."""
    event = find_event(guid)
    eligible, reason = _eligibility(event, now)
    if not eligible:
        raise HighlightError(reason)
    note = ' '.join(_CONTROL_RE.sub(' ', str(note or '')).split())[:NOTE_MAX]
    item = _new_item(event, 'editor', 'active', now,
                     created_by=reviewer or 'admin', note=note)
    try:
        db._get_table().put_item(
            Item=item,
            ConditionExpression=_FREE_CONDITION,
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=_free_condition_values(now),
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise HighlightError(f"{item['title']} is already highlighted", 409)
        raise
    return _to_dict(item)


def _set_status(target_key, status, allowed_from, extra=None):
    """Move a highlight to `status`, only from one of `allowed_from`."""
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
            Key=_key(target_key),
            UpdateExpression='SET ' + ', '.join(sets),
            ConditionExpression=(f"attribute_exists(PK) AND #s IN "
                                 f"({', '.join(allowed)})"),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues='ALL_NEW',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            current = get_highlight(target_key)
            if not current:
                raise HighlightError(f'No highlight for {target_key}', 404)
            raise HighlightError(
                f"{target_key} is {current.get('status')}, not one of "
                f"{', '.join(allowed_from)}", 409)
        raise
    return _to_dict(resp.get('Attributes'))


def revoke(target_key, reviewer, reason=''):
    """Take a highlight down and free its event. Refunds are done by hand."""
    if not _KEY_RE.match(str(target_key or '')):
        raise HighlightError('That is not a highlight key', 404)
    return _set_status(target_key, 'revoked', LIVE_STATUSES,
                       {'revoked_by': reviewer or 'admin',
                        'revoke_reason': str(reason or '')[:NOTE_MAX]})


# ─── PayPal IPN ────────────────────────────────────────────────────

def is_highlight_custom(custom):
    return str(custom or '').startswith('highlight|')


def _parse_custom(custom):
    prefix, _, rest = str(custom or '').partition('|')
    key, _, booking_id = rest.partition('|')
    if prefix != 'highlight' or not _KEY_RE.match(key) or not booking_id:
        return None, None
    return key, booking_id


def handle_ipn(raw_body, verify=None):
    """Process one IPN for a highlight. Returns a short outcome for logs/tests.

    Same guarantees as sponsorships.handle_ipn: nothing is trusted until
    PayPal verifies it, and nothing goes live unless the money reached our
    account, in USD, for the price the hold was quoted.
    """
    verify = verify or verify_with_paypal
    if not verify(raw_body):
        print('Highlight IPN failed verification')
        return 'invalid'

    params = paypal.parse_ipn(raw_body)
    txn_id = params.get('txn_id')
    status = params.get('payment_status', '')
    key, booking_id = _parse_custom(params.get('custom'))
    if not txn_id or not key:
        _notify_admin('IPN with no highlight attached', params)
        return 'unmatched'

    if not paypal.record_payment(f'HIGHLIGHT#{key}', params):
        return 'duplicate'

    if not paypal.receiver_ok(params):
        _notify_admin('IPN for a different receiver', params)
        return 'wrong_receiver'

    item = get_highlight(key)
    ours = bool(item) and item.get('booking_id') == booking_id

    if status in ('Refunded', 'Reversed', 'Canceled_Reversal'):
        if ours and status != 'Canceled_Reversal':
            try:
                item = _set_status(key, 'refunded', LIVE_STATUSES)
            except HighlightError:
                pass
        _notify_admin(f'Payment {status.lower()} for {key}', params, item)
        return status.lower()

    if status in ('Failed', 'Denied', 'Voided', 'Expired'):
        if (ours and item.get('status') == 'payment_pending'
                and item.get('txn_id') == txn_id):
            try:
                item = _set_status(key, 'cancelled', ('payment_pending',))
            except HighlightError:
                pass
        _notify_admin(f'Payment {status.lower()} for {key}', params, item)
        return 'payment_failed'

    if status not in ('Pending', 'Completed'):
        _notify_admin(f'IPN status {status} for {key}', params, item)
        return 'ignored'

    expected = Decimal(str(item['price_usd'])) if ours else PRICE_USD
    if not paypal.amount_ok(params, expected):
        _notify_admin(f'Wrong amount for {key} — refund or follow up',
                      params, item)
        return 'wrong_amount'

    if status == 'Pending':
        if ours and item.get('status') == 'held':
            try:
                _set_status(key, 'payment_pending', ('held',), {'txn_id': txn_id})
            except HighlightError:
                pass
        _notify_admin(f'Payment pending for {key}', params, item)
        return 'pending'

    if not ours:
        _notify_admin(f'CONFLICT: payment for {key} but the hold was lost '
                      f'— refund it', params, item)
        return 'conflict'

    if item.get('status') == 'active':
        return 'already_active'

    try:
        item = _set_status(
            key, 'active', ('held', 'payment_pending'),
            {'txn_id': txn_id, 'payer_email': params.get('payer_email', '')})
    except HighlightError:
        _notify_admin(f'CONFLICT: payment for {key} arrived after the '
                      f'highlight was {item.get("status")}', params, item)
        return 'conflict'

    _email_receipt(item)
    return 'active'


# ─── Email ─────────────────────────────────────────────────────────

ADMIN_PAGE_URL = f'{BASE_URL}/edit/highlights.html'
EXPLAINER_URL = f'{BASE_URL}/highlights/'


def _describe(item):
    if not item:
        return '(no highlight on file)'
    return '\n'.join(f'{k}: {item.get(k, "")}' for k in (
        'target_key', 'kind', 'status', 'title', 'date', 'time', 'url',
        'contact_email', 'price_usd', 'txn_id', 'booking_id'))


def _notify_admin(subject, params, item=None):
    ipn = '\n'.join(f'{k}: {v}' for k, v in sorted((params or {}).items()))
    _send(ADMIN_EMAIL, f'[dctech.events] Highlight: {subject}',
          f'{subject}\n\nHighlight:\n{_describe(item)}\n\nIPN:\n{ipn}\n\n'
          f'{ADMIN_PAGE_URL}\n')


def _pretty_date(iso):
    try:
        return date.fromisoformat(iso).strftime('%A, %B %-d, %Y')
    except (TypeError, ValueError):
        return iso or ''


def _email_receipt(item):
    if not item or not item.get('contact_email'):
        return
    event_date = _event_date(item)
    start = (max(date.today(), window_start(event_date))
             if event_date else None)
    when = (f'It will show from {_pretty_date(start.isoformat())} through '
            f'the last day of the event' if start else 'It is live now')
    _send(item['contact_email'],
          'Your DC Tech Events highlight is live',
          f"Thanks! Your highlight for \"{item.get('title')}\" "
          f"({_pretty_date(item.get('date'))}) is paid and live.\n\n"
          f"{when}, in the Highlighted box on the DC Tech Events homepage "
          f"and on matching category, week and month pages, and in the "
          f"Monday newsletter for subscribers whose preferences match. "
          f"It is labeled \"Paid\".\n\n"
          f"It can take a few minutes for the site to rebuild.\n\n"
          f"Event: {item.get('url')}\n"
          f"How highlights work: {EXPLAINER_URL}\n\n"
          f"Questions, or need a refund? Just reply.\n")

