"""
DynamoDB CRUD operations for the next-stack config table (dctech-events-next).

Forked nearly verbatim from dctech.events/backend/db.py, with the additions
described in next-architecture-plan.md:
  - table defaults to dctech-events-next
  - EVENT items carry review_status (GSI5) and source may be ical/recurring
  - RECURRING#{slug} entities for recurring-event definitions
  - ICAL#{group_id} cache entities replacing calgen's _cache/ical files
  - a generic put_event() used by migration, the iCal aggregator, and MCP
"""

import os
import re
import time
import uuid
from datetime import date as _date_type, datetime, timedelta, timezone as _tz
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

CONFIG_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'dctech-events-next')

REVIEW_STATUSES = (
    'pending_qa', 'approved', 'flagged', 'pending_discovery_review', 'discovered',
)

# Distinguishes "caller passed None" from "caller passed nothing" — needed
# because None is a meaningful value for update_event's expect_overrides_rev.
_UNSET = object()

_table = None


def is_safe_url(url):
    """
    Check if a URL is safe to render in an href.
    Only allows http:// and https:// schemes.
    """
    if not url:
        return True  # Empty is fine
    url = str(url).strip().lower()
    return url.startswith('http://') or url.startswith('https://')


def _normalize_draft_status(status):
    """Normalize draft statuses for storage and querying."""
    return str(status or '').strip().lower()


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource('dynamodb')
        _table = dynamodb.Table(CONFIG_TABLE_NAME)
    return _table


def _query_all(table, **kwargs):
    response = table.query(**kwargs)
    items = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.query(**kwargs, ExclusiveStartKey=response['LastEvaluatedKey'])
        items.extend(response.get('Items', []))
    return items


def _scan_all(table, **kwargs):
    response = table.scan(**kwargs)
    items = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
    return items


def _to_plain(val):
    if isinstance(val, Decimal):
        return int(val) if val == int(val) else float(val)
    if isinstance(val, list):
        return [_to_plain(v) for v in val]
    if isinstance(val, dict):
        return {k: _to_plain(v) for k, v in val.items()}
    return val


def _from_plain(val):
    """Convert floats to Decimal for DynamoDB storage."""
    if isinstance(val, float):
        return Decimal(str(val))
    if isinstance(val, list):
        return [_from_plain(v) for v in val]
    if isinstance(val, dict):
        return {k: _from_plain(v) for k, v in val.items()}
    return val


# ─── DRAFT operations ─────────────────────────────────────────────

def normalize_categories(data):
    categories = data.get('categories', [])
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(',') if c.strip()]
    return categories


def build_event_draft_data(data):
    """Normalize a submitted event into the draft shape create_draft stores.

    Here rather than in routes/submit.py for the same reason as promote_draft
    below: the MCP Lambda bundles db.py but not the route modules, so a copy
    there would be a second set of submission rules free to drift from this
    one. The web form and submit_event must produce identical drafts, or a
    submission's shape would depend on which door it came through.

    Returns (draft_data, error_message); exactly one of the two is None.
    """
    title = (data.get('title') or data.get('name') or '').strip()
    date_val = data.get('date', '').strip()
    time_str = data.get('time', '').strip() or None
    timing = data.get('timing', 'specific')

    start_dt = data.get('start_datetime', '').strip()
    if start_dt and not date_val:
        if 'T' in start_dt:
            date_val, time_str = start_dt.split('T')
        else:
            date_val = start_dt

    if not title or not date_val:
        return None, 'Event title and date are required.'

    if timing == 'specific' and not time_str:
        hour = data.get('time_hour', '')
        minute = data.get('time_minute', '00')
        ampm = data.get('time_ampm', 'PM')
        if hour:
            h = int(hour)
            if ampm == 'PM' and h != 12:
                h += 12
            elif ampm == 'AM' and h == 12:
                h = 0
            time_str = f'{h:02d}:{minute}'

    draft_data = {
        'title': title,
        'date': date_val,
        'time': time_str,
        'url': data.get('url', ''),
        'city': data.get('city', ''),
        'state': data.get('state', ''),
        'cost': data.get('cost', ''),
        'end_date': data.get('end_date', ''),
        'all_day': timing == 'allday',
        'description': data.get('description', ''),
        'location': data.get('location', ''),
        'categories': normalize_categories(data),
    }
    return draft_data, None


def create_draft(draft_type, data, submitter_email, submitter_id=None):
    """Create a new DRAFT entity (pending submission)."""
    table = _get_table()
    draft_id = str(uuid.uuid4())[:8]
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    status = _normalize_draft_status('pending')

    # Sanitize URLs if present
    sanitized_data = data.copy()
    for field in ['url', 'website', 'ical', 'ical_url', 'fallback_url']:
        if field in sanitized_data and not is_safe_url(sanitized_data[field]):
            sanitized_data[field] = ''

    # We use sub (submitter_id) for indexing if available, otherwise email
    user_id = submitter_id or submitter_email

    item = {
        'PK': f'DRAFT#{draft_id}',
        'SK': 'META',
        'GSI1PK': f'STATUS#{status}',
        'GSI1SK': now,
        'GSI3PK': f'USER#{user_id}',  # For querying by submitter
        'GSI3SK': now,  # Sort by creation time
        'draft_type': draft_type,
        'submitter_email': submitter_email,
        'submitter_id': submitter_id,
        'created_at': now,
        'status': status,
        **{k: _from_plain(v) for k, v in sanitized_data.items() if v is not None},
    }

    table.put_item(Item=item)
    return draft_id


def get_drafts_by_status(status='pending'):
    """Get all drafts with a given status."""
    table = _get_table()
    normalized_status = _normalize_draft_status(status)

    items = _query_all(table, IndexName='GSI1', KeyConditionExpression=Key('GSI1PK').eq(f'STATUS#{normalized_status}'), ScanIndexForward=False)

    return [_draft_item_to_dict(item) for item in items]


def get_drafts_by_submitter(user_id):
    """Get all drafts submitted by a specific user (by sub or email)."""
    if not user_id:
        return []
    table = _get_table()

    # Use GSI3 to efficiently query by user identifier
    items = _query_all(table, IndexName='GSI3', KeyConditionExpression=Key('GSI3PK').eq(f'USER#{user_id}'), ScanIndexForward=False)

    return [_draft_item_to_dict(item) for item in items]


def get_draft(draft_id):
    """Get a single draft by ID."""
    table = _get_table()
    response = table.get_item(Key={'PK': f'DRAFT#{draft_id}', 'SK': 'META'})
    item = response.get('Item')
    return _draft_item_to_dict(item) if item else None


def update_draft_status(draft_id, new_status, reviewer_email=None, commit_url=None):
    """Update draft status (approve/reject)."""
    table = _get_table()
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    normalized_status = _normalize_draft_status(new_status)

    update_expr = 'SET #status = :status, GSI1PK = :gsi1pk, updated_at = :now'
    expr_values = {
        ':status': normalized_status,
        ':gsi1pk': f'STATUS#{normalized_status}',
        ':now': now,
    }
    expr_names = {'#status': 'status'}

    if reviewer_email:
        update_expr += ', reviewer_email = :reviewer'
        expr_values[':reviewer'] = reviewer_email

    if commit_url:
        update_expr += ', commit_url = :commit_url'
        expr_values[':commit_url'] = commit_url

    table.update_item(
        Key={'PK': f'DRAFT#{draft_id}', 'SK': 'META'},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
        ExpressionAttributeNames=expr_names,
    )


def _draft_item_to_dict(item):
    """Convert a DynamoDB DRAFT item to a dict."""
    draft_id = item['PK'].split('#', 1)[1]
    result = {'id': draft_id}
    for field in ['draft_type', 'status', 'submitter_email', 'reviewer_email',
                  'created_at', 'updated_at', 'title', 'date', 'time',
                  'location', 'url', 'cost', 'description', 'group_name',
                  'name', 'website', 'ical', 'ical_url', 'categories',
                  'commit_url']:
        if field in item:
            result[field] = _to_plain(item[field])
    return result


# ─── GROUP operations ──────────────────────────────────────────────

def get_all_groups():
    """Get all groups (active and inactive)."""
    table = _get_table()
    items = []

    for active_flag in ['ACTIVE#1', 'ACTIVE#0']:
        items += _query_all(table, IndexName='GSI1', KeyConditionExpression=Key('GSI1PK').eq(active_flag))

    return [_group_item_to_dict(item) for item in items]


def get_group(slug):
    """Get a single group by slug."""
    table = _get_table()
    response = table.get_item(Key={'PK': f'GROUP#{slug}', 'SK': 'META'})
    item = response.get('Item')
    return _group_item_to_dict(item) if item else None


def put_group(slug, data):
    """Create or update a GROUP entity."""
    table = _get_table()
    active = data.get('active', True)
    name = data.get('name', '')

    # Sanitize URLs
    sanitized_data = data.copy()
    for field in ['website', 'ical', 'fallback_url', 'url_override']:
        if field in sanitized_data and not is_safe_url(sanitized_data[field]):
            sanitized_data[field] = ''

    item = {
        'PK': f'GROUP#{slug}',
        'SK': 'META',
        'GSI1PK': f'ACTIVE#{1 if active else 0}',
        'GSI1SK': f'NAME#{name}',
        **{k: _from_plain(v) for k, v in sanitized_data.items() if v is not None},
    }

    categories = data.get('categories', [])
    if categories:
        item['GSI2PK'] = f'CATEGORY#{categories[0]}'
        item['GSI2SK'] = f'GROUP#{slug}'

    table.put_item(Item=item)


def delete_group(slug):
    """Delete a GROUP entity."""
    table = _get_table()
    table.delete_item(Key={'PK': f'GROUP#{slug}', 'SK': 'META'})


def _group_item_to_dict(item):
    """Convert a DynamoDB GROUP item to a dict."""
    slug = item['PK'].split('#', 1)[1]
    group = {'id': slug, 'name': item.get('name', ''), 'active': item.get('active', True)}
    for field in ['website', 'ical', 'fallback_url', 'categories',
                  'suppress_urls', 'suppress_guid', 'scan_for_metadata',
                  'url_override', 'skip_phrases']:
        if field in item:
            group[field] = _to_plain(item[field])
    return group


# ─── EVENT operations ──────────────────────────────────────────────

EVENT_FIELDS = ['title', 'date', 'time', 'end_date', 'end_time', 'location',
                'url', 'cost', 'description', 'group', 'group_id',
                'group_website', 'categories', 'city', 'state', 'all_day',
                'location_type', 'slug', 'submitted_by', 'source', 'hidden',
                'duplicate_of', 'overrides', 'review_status', 'createdAt']


def put_event(guid, data, source='manual', review_status='approved', created_at=None):
    """Create or replace an EVENT#{guid} entity with full GSI wiring.

    Used by the migration script, the iCal aggregator, and MCP tools.
    """
    table = _get_table()
    now = created_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    date_val = str(data.get('date', '') or '')
    time_val = data.get('time') or '00:00'
    if isinstance(time_val, dict):  # multi-day events can carry per-date times
        time_val = next(iter(time_val.values()), '00:00') or '00:00'

    item = {
        'PK': f'EVENT#{guid}',
        'SK': 'META',
        'GSI1PK': f'DATE#{date_val}',
        'GSI1SK': f'TIME#{time_val}',
        'GSI3PK': f'CREATED#{now[:7]}',
        'GSI3SK': now,
        'source': source,
        'status': 'ACTIVE',
        'createdAt': now,
        'review_status': review_status,
        'GSI5PK': f'REVIEW#{review_status}',
        'GSI5SK': f'{date_val}#{time_val}',
    }
    if date_val:
        item['GSI4PK'] = 'EVT#ACTIVE'
        item['GSI4SK'] = f'{date_val}#{time_val}' if time_val else date_val

    for field in EVENT_FIELDS:
        if field == 'source':
            continue  # the `source` parameter is authoritative, set above
        if field in data and data[field] is not None:
            val = data[field]
            if field == 'url' and not is_safe_url(val):
                val = ''
            item[field] = _from_plain(val)

    table.put_item(Item=item)
    return guid


def get_events_by_date(date_prefix=None):
    """Get events, optionally filtered by date prefix (e.g. '2026-02')."""
    table = _get_table()

    # GSI1PK stores full dates like DATE#2026-02-19, so we scan with a filter
    # for prefix matching (begins_with is not supported on partition keys in queries)
    scan_kwargs = {
        'FilterExpression': Attr('PK').begins_with('EVENT#') & Attr('SK').eq('META'),
    }
    if date_prefix:
        scan_kwargs['FilterExpression'] = (
            Attr('GSI1PK').begins_with(f'DATE#{date_prefix}') & Attr('SK').eq('META')
        )

    items = _scan_all(table, **scan_kwargs)

    return [_event_item_to_dict(item) for item in items]


def _event_item_to_dict(item):
    """Convert a DynamoDB EVENT item to a dict."""
    guid = item['PK'].split('#', 1)[1]
    event = {'guid': guid, 'id': guid}
    for field in EVENT_FIELDS + ['status']:
        if field in item:
            event[field] = _to_plain(item[field])
    return event


def get_all_events(date_prefix=None, filter_type=None, include_past=False):
    """Query config table for active events via GSI4, optionally filtered by YYYY-MM prefix."""
    table = _get_table()

    if date_prefix:
        year, month = date_prefix.split('-')
        start = f"{year}-{month}-01"
        next_month = int(month) + 1
        if next_month > 12:
            end = f"{int(year) + 1}-01-01"
        else:
            end = f"{year}-{next_month:02d}-01"
        kce = Key('GSI4PK').eq('EVT#ACTIVE') & Key('GSI4SK').between(start, end)
    elif include_past:
        kce = Key('GSI4PK').eq('EVT#ACTIVE')
    else:
        today = _date_type.today().isoformat()
        kce = Key('GSI4PK').eq('EVT#ACTIVE') & Key('GSI4SK').gte(today)

    items = _query_all(table, IndexName='GSI4', KeyConditionExpression=kce)

    results = [_config_event_to_dict(item) for item in items]

    if filter_type == 'uncategorized':
        results = [e for e in results if not e.get('categories')]

    return sorted(
        results,
        key=lambda x: (str(x.get('date', '') or ''), str(x.get('time', '') or '')),
    )


def _config_event_to_dict(item):
    """Convert a config table EVENT item to a dict suitable for templates."""
    guid = item['PK'].split('#', 1)[1]
    event = {'guid': guid, 'eventId': guid}
    for field in ['title', 'date', 'time', 'end_date', 'url',
                  'location', 'cost', 'source', 'group', 'group_website',
                  'group_id', 'categories', 'city', 'state', 'all_day',
                  'hidden', 'duplicate_of', 'createdAt', 'review_status',
                  'overrides', 'description', 'location_type', 'slug']:
        if field in item:
            event[field] = _to_plain(item[field])
    return event


def get_events_by_review_status(review_status, limit=None):
    """Query GSI5 for events awaiting QA / discovery review."""
    table = _get_table()
    kwargs = {
        'IndexName': 'GSI5',
        'KeyConditionExpression': Key('GSI5PK').eq(f'REVIEW#{review_status}'),
    }
    if limit:
        kwargs['Limit'] = limit
    items = _query_all(table, **kwargs)
    if limit:
        # DynamoDB's Limit caps items *per page*, and _query_all then follows
        # LastEvaluatedKey to the end — so the kwarg above keeps the per-page
        # read cost down but does not bound the total. Truncating here is what
        # actually honours the caller. Without it `limit` silently returned the
        # whole queue, which made a bounded QC dry run impossible.
        items = items[:limit]
    return [_event_item_to_dict(item) for item in items]


def set_event_review_status(guid, review_status):
    """Update an event's review_status and its GSI5 keys."""
    event = get_event_from_config(guid)
    if not event:
        return None
    table = _get_table()
    date_val = str(event.get('date', '') or '')
    time_val = event.get('time') or '00:00'
    table.update_item(
        Key={'PK': f'EVENT#{guid}', 'SK': 'META'},
        UpdateExpression='SET review_status = :rs, GSI5PK = :pk, GSI5SK = :sk',
        ExpressionAttributeValues={
            ':rs': review_status,
            ':pk': f'REVIEW#{review_status}',
            ':sk': f'{date_val}#{time_val}',
        },
    )
    return guid


def get_materialized_event(guid):
    """Get a single event from the config table by GUID."""
    return get_event_from_config(guid)


def get_event_from_config(guid):
    """Get an EVENT#{guid} entity from the config table, or None if not found."""
    table = _get_table()
    response = table.get_item(Key={'PK': f'EVENT#{guid}', 'SK': 'META'})
    item = response.get('Item')
    return _event_item_to_dict(item) if item else None


def update_event(guid, data, overrides=None, *, expect_overrides_rev=_UNSET):
    """Update an EVENT#{guid} entity in the config table with the given fields.

    `expect_overrides_rev` makes the write conditional on the overlay's `_rev`,
    which is what stops two overlay writers from silently clobbering each other.
    The overlay is stored as a whole map, so a lost race does not lose one field
    — it loses the loser's entire overlay including its `_qa_run` stamp, which
    would leave revert_qa_run restoring a state that never existed.

    * `_UNSET` (default) — no condition. Every pre-existing caller keeps its
      old behaviour, which is why this is opt-in rather than automatic.
    * `None` — require that no overlay revision exists yet (first write).
    * int — require that revision exactly.

    Raises botocore ConditionalCheckFailedException on a mismatch;
    set_event_overlay is what turns that into a retry or an OverlayConflict.
    """
    table = _get_table()
    date = data.get('date', '')
    time_val = data.get('time', '00:00')

    update_parts = ['GSI1PK = :gsi1pk', 'GSI1SK = :gsi1sk']
    expr_values = {
        ':gsi1pk': f'DATE#{date}',
        ':gsi1sk': f'TIME#{time_val}',
    }
    expr_names = {}

    updatable_fields = ['title', 'url', 'date', 'time', 'end_date', 'cost',
                        'city', 'state', 'all_day', 'categories', 'location',
                        'hidden', 'duplicate_of']
    for field in updatable_fields:
        if field in data and data[field] is not None:
            val = data[field]
            if field == 'url' and not is_safe_url(val):
                val = ''

            safe_key = f'#f_{field}'
            expr_names[safe_key] = field
            update_parts.append(f'{safe_key} = :{field}')
            expr_values[f':{field}'] = _from_plain(val)

    if overrides is not None:
        expr_names['#f_overrides'] = 'overrides'
        update_parts.append('#f_overrides = :overrides')
        expr_values[':overrides'] = _from_plain(overrides)

    update_expr = 'SET ' + ', '.join(update_parts)
    kwargs = {
        'Key': {'PK': f'EVENT#{guid}', 'SK': 'META'},
        'UpdateExpression': update_expr,
        'ExpressionAttributeValues': expr_values,
    }
    if expr_names:
        kwargs['ExpressionAttributeNames'] = expr_names
    if expect_overrides_rev is not _UNSET:
        if expect_overrides_rev is None:
            kwargs['ConditionExpression'] = (
                Attr('overrides').not_exists() | Attr('overrides._rev').not_exists()
            )
        else:
            kwargs['ConditionExpression'] = Attr('overrides._rev').eq(
                expect_overrides_rev)
    table.update_item(**kwargs)


def promote_draft_to_event(draft):
    """Promote an approved event draft to an EVENT entity in the config table.

    Unlike production, there is no git-commit hop — the Static Site Generator
    reads DynamoDB directly, so writing the item is the whole promotion.
    """
    guid = draft['id']
    data = {k: draft.get(k) for k in
            ['title', 'url', 'date', 'time', 'end_date', 'cost', 'city',
             'state', 'all_day', 'categories', 'location', 'description']
            if draft.get(k) is not None}
    data['submitted_by'] = draft.get('submitter_email', '')
    put_event(guid, data, source='submitted', review_status='approved')
    return guid


# ─── Event overlays ───────────────────────────────────────────────
#
# An overlay is the `overrides` map on an EVENT# item: the editorial layer that
# sits on top of whatever the event's source said. It lives here rather than in
# the MCP server because two callers must produce byte-identical results — the
# QA agent through mcp/server.py, and a human through the /edit UI — and
# build_lambdas.sh copies this module into the MCP bundle. Same reasoning as
# build_event_draft_data / promote_draft for the submission path.
#
# Keys starting with an underscore are private bookkeeping, never overlay
# values. export_dynamo_to_calgen strips them, so calgen never sees them, and
# check_overlay_fields refuses to let a caller write them directly:
#
#   _comment      why the most recent write happened
#   _qa_run       {run_id, prior: {field: old}, added: [field]} — most recent
#                 run only, which is all revert_qa_run can undo
#   _rev          revision counter, the conditional-write token
#   _edited_by    'someone@example.com' | 'agent:qa' | 'agent:mcp'
#   _edited_at    ISO8601 Z
#   _field_edits  {field: {by, at}} — per-field provenance, so a reviewer can
#                 tell which half of an overlay was the agent's

# Set by the pipeline from the source feed. Overriding these would mean
# overriding the event's identity, not its presentation. calgen keeps its own
# copy of this set for the render side — see packages/calgen/src/calgen/overlay.py.
OVERLAY_PROTECTED_FIELDS = frozenset({
    'group', 'group_id', 'group_website', 'date', 'end_date', 'guid', 'source',
})

# What a human or agent may actually write. An allowlist, not a denylist:
# before this existed, `time`, `url`, `description` and even a typo like
# `titel` were all writable through set_overlay and would sit in the overlay
# doing nothing. The /edit UI generates its form from this tuple, so it can
# never offer a field the write path rejects.
OVERLAY_EDITABLE_FIELDS = (
    'title', 'time', 'end_time', 'location', 'location_type', 'url', 'cost',
    'city', 'state', 'description', 'categories', 'all_day',
    'hidden', 'duplicate_of',
)

_OVERLAY_PRIVATE_KEYS = ('_comment', '_qa_run', '_rev', '_edited_by',
                         '_edited_at', '_field_edits')


class OverlayConflict(Exception):
    """Someone else wrote this overlay between the caller's read and write."""

    def __init__(self, guid, current_rev, current_overlay):
        super().__init__(
            f'Overlay for {guid} changed since it was read '
            f'(now at revision {current_rev})'
        )
        self.guid = guid
        self.current_rev = current_rev
        self.current_overlay = current_overlay


def public_overlay(overrides):
    """The overlay minus its private bookkeeping — what actually renders."""
    return {k: v for k, v in (overrides or {}).items() if not k.startswith('_')}


def overlay_bookkeeping(overrides):
    """The private half, for a UI that wants to show provenance."""
    overrides = overrides or {}
    stamp = overrides.get('_qa_run') or {}
    return {
        'rev': _to_plain(overrides.get('_rev') or 0),
        'comment': overrides.get('_comment', ''),
        'edited_by': overrides.get('_edited_by', ''),
        'edited_at': overrides.get('_edited_at', ''),
        'field_edits': _to_plain(overrides.get('_field_edits') or {}),
        'run_id': stamp.get('run_id'),
        'prior': _to_plain(stamp.get('prior') or {}),
        'added': list(stamp.get('added') or []),
    }


def valid_category_slugs():
    return set(get_all_categories().keys())


def validate_category_slugs(categories):
    unknown = sorted(set(categories or []) - valid_category_slugs())
    if unknown:
        raise ValueError(
            f'Unknown category slug(s): {unknown}. '
            f'Valid slugs: {sorted(valid_category_slugs())}'
        )


def check_overlay_fields(fields):
    """Reject anything a caller must not write. Raises ValueError."""
    protected = sorted(set(fields) & OVERLAY_PROTECTED_FIELDS)
    if protected:
        raise ValueError(f'Cannot set protected overlay field(s): {protected}')
    private = sorted(k for k in fields if k.startswith('_'))
    if private:
        raise ValueError(f'Cannot set reserved overlay key(s): {private}')
    unknown = sorted(set(fields) - set(OVERLAY_EDITABLE_FIELDS))
    if unknown:
        raise ValueError(
            f'Not an overlay-editable field: {unknown}. '
            f'Editable: {sorted(OVERLAY_EDITABLE_FIELDS)}'
        )


def get_event_overlay(guid):
    """The raw overlay for one event, private keys included. {} if none."""
    event = get_event_from_config(guid)
    if not event:
        return {}
    return event.get('overrides') or {}


def effective_event(event):
    """The event as it will render: the record with its overlay merged on top.

    Only allowlisted fields are merged, so legacy junk or a field written
    before the allowlist existed cannot change what a caller sees here.
    """
    merged = dict(event or {})
    overlay = (event or {}).get('overrides') or {}
    for field in OVERLAY_EDITABLE_FIELDS:
        if field in overlay:
            merged[field] = _to_plain(overlay[field])
    return merged


def _now_iso():
    return datetime.now(_tz.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _validate_overlay_values(guid, fields):
    """Value-level checks the field allowlist cannot express."""
    if 'categories' in fields:
        validate_category_slugs(fields['categories'])

    target = fields.get('duplicate_of')
    if target:
        if target == guid:
            raise ValueError('An event cannot be a duplicate of itself')
        if not get_event_from_config(target):
            # A duplicate_of pointing outside the corpus is silently ignored at
            # render (calgen's remove_duplicates requires the target be present),
            # so the merge would look done and the event would keep showing.
            raise ValueError(f'No such event to merge into: {target}')


def _stamp_qa_run(existing, fields, clear, run_id):
    """Record what this run found, so revert_qa_run can put it back.

    The snapshot is of the state before the run *first* touched each field, so a
    second write in the same run does not overwrite it. Fields with no prior
    value go in `added` rather than getting a sentinel in `prior`, because a
    sentinel would not survive the DynamoDB round trip.
    """
    stamp = dict(existing.get('_qa_run') or {})
    if stamp.get('run_id') != run_id:
        stamp = {'run_id': run_id, 'prior': {}, 'added': []}
    prior = dict(stamp.get('prior') or {})
    added = list(stamp.get('added') or [])
    for key in list(fields) + list(clear):
        if key in prior or key in added:
            continue
        if key in existing:
            prior[key] = existing[key]
        else:
            added.append(key)
    stamp['prior'] = prior
    stamp['added'] = added
    return stamp


def _build_overlay(existing, fields, clear, comment, run_id, actor):
    """The overlay map to store, given the one that is already there."""
    updated = dict(existing)

    if run_id:
        updated['_qa_run'] = _stamp_qa_run(existing, fields, clear, run_id)

    for key in clear:
        updated.pop(key, None)
    updated.update(fields)

    if comment:
        updated['_comment'] = comment

    who = actor or (f'agent:{"qa" if run_id else "mcp"}')
    when = _now_iso()
    updated['_rev'] = int(_to_plain(existing.get('_rev') or 0)) + 1
    updated['_edited_by'] = who
    updated['_edited_at'] = when

    # Per-field provenance, so "the agent set the location, you set the title"
    # is answerable. Entries for cleared fields go away with the field.
    field_edits = dict(_to_plain(existing.get('_field_edits') or {}))
    for key in clear:
        field_edits.pop(key, None)
    for key in fields:
        field_edits[key] = {'by': who, 'at': when}
    if field_edits:
        updated['_field_edits'] = field_edits
    else:
        updated.pop('_field_edits', None)

    return updated


def _put_overlay(guid, event, overlay, expected_rev):
    """One conditional write of an overlay map."""
    update_event(
        guid,
        # Pass the event's real date and time: update_event rewrites GSI1 from
        # them unconditionally, so omitting them writes 'DATE#' and corrupts
        # the index.
        {'date': event.get('date', ''), 'time': event.get('time') or '00:00'},
        overrides=overlay,
        expect_overrides_rev=expected_rev,
    )


def _overlay_result(guid, event, overlay):
    effective = effective_event({**event, 'overrides': overlay})
    book = overlay_bookkeeping(overlay)
    return {
        'guid': guid,
        'overlay': public_overlay(overlay),
        'effective': effective,
        'rev': book['rev'],
        'edited_by': book['edited_by'],
        'edited_at': book['edited_at'],
    }


_OVERLAY_WRITE_ATTEMPTS = 3


def set_event_overlay(guid, fields, comment=None, *, run_id=None, actor=None,
                      clear=(), expected_rev=_UNSET):
    """Merge `fields` into an event's overlay. The single overlay writer.

    `clear` removes overlay fields outright, which is how the UI says "stop
    overriding this and fall back to what the source says".

    `run_id` tags the write as part of a batch (the QA agent's weekly pass) and
    records each field's prior value so revert_qa_run can restore it. Leave it
    unset for a hand edit; pass `actor` instead.

    `expected_rev` is the revision the caller last read:

    * omitted — re-read, re-merge and retry on a collision. The default because
      it is the forgiving one: correct for the agent and for bulk actions, where
      a field-level merge is exactly the intended outcome and there is no human
      to consult.
    * an int, or None meaning "there was no overlay" — the write is conditional,
      and a concurrent change raises OverlayConflict so the caller can show the
      user what happened. This is what a UI editing from a form wants, and it is
      opt-in so that forgetting it cannot make an ordinary write fail.
    """
    check_overlay_fields(fields)
    check_overlay_fields({k: None for k in clear})
    _validate_overlay_values(guid, fields)

    conditional = expected_rev is not _UNSET
    attempts = 1 if conditional else _OVERLAY_WRITE_ATTEMPTS

    for attempt in range(attempts):
        event = get_event_from_config(guid)
        if not event:
            raise ValueError(f'No such event: {guid}')
        existing = dict(event.get('overrides') or {})
        stored_rev = int(_to_plain(existing.get('_rev') or 0)) or None

        if conditional and expected_rev != stored_rev:
            raise OverlayConflict(guid, stored_rev, public_overlay(existing))

        overlay = _build_overlay(existing, fields, clear, comment, run_id, actor)
        try:
            _put_overlay(guid, event, overlay,
                         expected_rev if conditional else stored_rev)
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            if conditional:
                current = get_event_overlay(guid)
                raise OverlayConflict(
                    guid, _to_plain(current.get('_rev') or 0),
                    public_overlay(current)) from exc
            if attempt == attempts - 1:
                raise
            continue
        return _overlay_result(guid, event, overlay)


def clear_event_overlay(guid, *, actor=None, comment=None, expected_rev=_UNSET):
    """Drop every public overlay field, keeping the audit trail.

    Deliberately not `overrides = {}`: an all-private map reads as no overlay to
    both public_overlay and the exporter, so the site sees the source's own
    values again while who-cleared-it-and-when survives. The `_qa_run` stamp
    goes, because there is no longer anything for it to revert.
    """
    event = get_event_from_config(guid)
    if not event:
        raise ValueError(f'No such event: {guid}')
    existing = dict(event.get('overrides') or {})
    stored_rev = int(_to_plain(existing.get('_rev') or 0)) or None
    if expected_rev is not _UNSET and expected_rev != stored_rev:
        raise OverlayConflict(guid, stored_rev, public_overlay(existing))

    overlay = {
        '_rev': (stored_rev or 0) + 1,
        '_edited_by': actor or 'agent:mcp',
        '_edited_at': _now_iso(),
        '_comment': comment or 'overlay cleared',
    }
    try:
        _put_overlay(guid, event, overlay,
                     expected_rev if expected_rev is not _UNSET else stored_rev)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        current = get_event_overlay(guid)
        raise OverlayConflict(guid, _to_plain(current.get('_rev') or 0),
                              public_overlay(current)) from exc
    return _overlay_result(guid, event, overlay)


def events_in_qa_run(run_id):
    """Every (event, stamp) pair carrying an overlay written by `run_id`.

    Scans past events too: the run may have hidden an event or a dated one, and
    those still need to be revertible.
    """
    matched = []
    for event in get_all_events(include_past=True):
        stamp = (event.get('overrides') or {}).get('_qa_run') or {}
        if stamp.get('run_id') == run_id:
            matched.append((event, stamp))
    return matched


def describe_qa_run(run_id):
    """What one run changed, and what each field was before it."""
    return [
        {
            'guid': event['guid'],
            'title': event.get('title', ''),
            'date': event.get('date', ''),
            'group': event.get('group', ''),
            'comment': (event.get('overrides') or {}).get('_comment', ''),
            'applied': public_overlay(event.get('overrides')),
            'restores_to': _to_plain(stamp.get('prior') or {}),
            'removes': list(stamp.get('added') or []),
        }
        for event, stamp in events_in_qa_run(run_id)
    ]


def revert_qa_run(run_id, *, actor=None):
    """Undo one run: fields it introduced go, fields it overwrote come back.

    Overlay fields written by another run or by hand are untouched, so this is
    safe on an event edited since — for those fields. A field the run wrote and
    a human then overwrote is restored to the run's snapshot, losing the human's
    value; the UI warns about exactly that before letting someone overwrite an
    agent field.
    """
    reverted = []
    for event, stamp in events_in_qa_run(run_id):
        overrides = dict(event.get('overrides') or {})
        for key in stamp.get('added') or []:
            overrides.pop(key, None)
        for key, value in (stamp.get('prior') or {}).items():
            overrides[key] = value
        overrides.pop('_qa_run', None)
        # The comment described this run's edit; it describes nothing once the
        # edit is gone.
        overrides.pop('_comment', None)

        field_edits = dict(_to_plain(overrides.get('_field_edits') or {}))
        for key in stamp.get('added') or []:
            field_edits.pop(key, None)
        if field_edits:
            overrides['_field_edits'] = field_edits
        else:
            overrides.pop('_field_edits', None)

        overrides['_rev'] = int(_to_plain(overrides.get('_rev') or 0)) + 1
        overrides['_edited_by'] = actor or 'agent:revert'
        overrides['_edited_at'] = _now_iso()

        _put_overlay(event['guid'], event, overrides, _UNSET)
        reverted.append({'guid': event['guid'],
                         'title': event.get('title', ''),
                         'overlay': public_overlay(overrides)})
    return {'run_id': run_id, 'reverted': len(reverted), 'events': reverted}


# ─── Bulk moderation ──────────────────────────────────────────────
#
# All of these go through set_event_overlay, and that is the whole point.
# They used to write the *top-level* `hidden` / `categories` / `duplicate_of`
# columns, which meant:
#
#   * For iCal events they did nothing at all. An iCal event's EVENT# row
#     contributes only _overlay/{guid}.yaml to the build — its body comes from
#     _cache/ical/, and export_dynamo_to_calgen skips writing a _single_events
#     file for any source outside ('manual','submitted',None). So the top-level
#     column never reached calgen. The bulk toolbar these were written for was
#     a no-op on most of the calendar.
#   * `categories` was reset from the group by the aggregator every four hours
#     anyway, so even where it landed it did not last.
#   * They called update_event with no `date`, and update_event rewrites GSI1
#     from `data['date']` unconditionally — so they wrote the literal 'DATE#'
#     and corrupted the index. Latent only because nothing reads
#     get_events_by_date. Going through the overlay writer fixes this for free:
#     it passes the event's real date and time.
#
# Each returns {'updated': int, 'failed': [{'guid', 'error'}]} rather than
# raising, because one bad guid in a selection of forty should not read as
# total failure. None of them use the conditional write: a human ticking forty
# boxes must not get a conflict because the agent touched one of them.


def _bulk_overlay(guids, fields, comment, actor, clear=()):
    updated, failed = 0, []
    for guid in guids:
        try:
            set_event_overlay(guid, dict(fields), comment,
                              actor=actor, clear=clear)
            updated += 1
        except (ValueError, OverlayConflict) as exc:
            failed.append({'guid': guid, 'error': str(exc)})
    return {'updated': updated, 'failed': failed}


def bulk_delete_events(guids, actor_email, comment='hidden in bulk'):
    """Hide multiple events. Named for history; it has never deleted anything."""
    return _bulk_overlay(guids, {'hidden': True}, comment, actor_email)


def bulk_unhide_events(guids, actor_email, comment='unhidden in bulk'):
    """Stop hiding multiple events.

    Clears the overlay field rather than setting `hidden: False`, so the event
    falls back to whatever its source says instead of carrying a permanent
    override that happens to agree.
    """
    return _bulk_overlay(guids, {}, comment, actor_email, clear=('hidden',))


def bulk_hard_delete_events(guids, actor_email):
    """Permanently delete multiple events.

    Not editorial and not revertible, so this is deliberately not reachable
    from the /edit UI — a moderator gets `hide`. It also does not do what it
    appears to for an iCal event: the aggregator rewrites the row within four
    hours and the deletion takes the overlay with it, so the event comes back
    unmoderated. Kept for the rare stale manual record, via MCP.
    """
    table = _get_table()
    for guid in guids:
        table.delete_item(Key={'PK': f'EVENT#{guid}', 'SK': 'META'})


def delete_event(guid):
    """Permanently delete a single event. See bulk_hard_delete_events."""
    table = _get_table()
    table.delete_item(Key={'PK': f'EVENT#{guid}', 'SK': 'META'})


def bulk_set_category(guids, category_slug, actor_email,
                      comment='category added in bulk'):
    """Add one category to multiple events, keeping the ones already there.

    Reads the *effective* categories, so repeatedly adding does not drop a
    category an earlier overlay set.
    """
    updated, failed = 0, []
    for guid in guids:
        try:
            event = get_event_from_config(guid)
            if not event:
                raise ValueError(f'No such event: {guid}')
            cats = list(effective_event(event).get('categories') or [])
            if category_slug in cats:
                continue
            cats.append(category_slug)
            set_event_overlay(guid, {'categories': cats}, comment,
                              actor=actor_email)
            updated += 1
        except (ValueError, OverlayConflict) as exc:
            failed.append({'guid': guid, 'error': str(exc)})
    return {'updated': updated, 'failed': failed}


def bulk_combine_events(guids, target_guid, actor_email,
                        comment='merged in bulk', hide_children=True):
    """Mark multiple events as duplicates of `target_guid`.

    `hide_children` also sets `hidden`, which is redundant at render — calgen's
    remove_duplicates already drops a child, and it credits the parent's
    `also_published_by` before the hidden filter runs — but it is deliberately
    belt-and-braces: if the canonical event later leaves its feed, the dangling
    `duplicate_of` stops being honoured and the child would resurrect as a stray
    listing. Hidden, it stays gone until a human looks at it.
    """
    guids = [g for g in guids if g != target_guid]
    fields = {'duplicate_of': target_guid}
    if hide_children:
        fields['hidden'] = True
    return _bulk_overlay(guids, fields, comment, actor_email)


def bulk_unmerge_events(guids, actor_email, comment='unmerged in bulk'):
    """Undo a merge: stop pointing at a canonical event, and stop hiding."""
    return _bulk_overlay(guids, {}, comment, actor_email,
                         clear=('duplicate_of', 'hidden'))


# ─── RECURRING EVENT operations ───────────────────────────────────

def put_recurring_event(slug, data):
    """Create or update a RECURRING#{slug} entity (rrule-based definition)."""
    table = _get_table()
    item = {
        'PK': f'RECURRING#{slug}',
        'SK': 'META',
        'GSI1PK': 'RECURRING',
        'GSI1SK': slug,
        **{k: _from_plain(v) for k, v in data.items() if v is not None},
    }
    if 'url' in item and not is_safe_url(item.get('url')):
        item['url'] = ''
    table.put_item(Item=item)
    return slug


def get_recurring_event(slug):
    table = _get_table()
    response = table.get_item(Key={'PK': f'RECURRING#{slug}', 'SK': 'META'})
    item = response.get('Item')
    return _recurring_item_to_dict(item) if item else None


def get_all_recurring_events():
    table = _get_table()
    items = _query_all(table, IndexName='GSI1',
                       KeyConditionExpression=Key('GSI1PK').eq('RECURRING'))
    return [_recurring_item_to_dict(item) for item in items]


def delete_recurring_event(slug):
    table = _get_table()
    table.delete_item(Key={'PK': f'RECURRING#{slug}', 'SK': 'META'})


def _recurring_item_to_dict(item):
    slug = item['PK'].split('#', 1)[1]
    result = {'id': slug}
    for k, v in item.items():
        if k in ('PK', 'SK') or k.startswith('GSI'):
            continue
        result[k] = _to_plain(v)
    return result


# ─── ICAL CACHE operations ────────────────────────────────────────
# Replaces calgen's filesystem _cache/ical/{group_id}.json + .meta files.

ICAL_CACHE_TTL_DAYS = 30


def put_ical_cache(group_id, events, meta=None):
    """Store fetched iCal events + fetch metadata for a group."""
    table = _get_table()
    now = datetime.now(_tz.utc)
    item = {
        'PK': f'ICAL#{group_id}',
        'SK': 'META',
        'events': _from_plain(events),
        'meta': _from_plain(meta or {}),
        'updated_at': now.isoformat(),
        'ttl': int((now + timedelta(days=ICAL_CACHE_TTL_DAYS)).timestamp()),
    }
    table.put_item(Item=item)


def get_ical_cache(group_id):
    """Return {'events': [...], 'meta': {...}} for a group, or None."""
    table = _get_table()
    response = table.get_item(Key={'PK': f'ICAL#{group_id}', 'SK': 'META'})
    item = response.get('Item')
    if not item:
        return None
    return {
        'group_id': group_id,
        'events': _to_plain(item.get('events', [])),
        'meta': _to_plain(item.get('meta', {})),
        'updated_at': item.get('updated_at'),
    }


def get_all_ical_caches():
    """Scan all ICAL# cache items (used by the site-generator export)."""
    table = _get_table()
    items = _scan_all(
        table,
        FilterExpression=Attr('PK').begins_with('ICAL#') & Attr('SK').eq('META'),
    )
    return [{
        'group_id': item['PK'].split('#', 1)[1],
        'events': _to_plain(item.get('events', [])),
        'meta': _to_plain(item.get('meta', {})),
        'updated_at': item.get('updated_at'),
    } for item in items]


# ─── CANDIDATE operations (Discovery agent scaffold) ──────────────

def put_candidate(candidate_hash, data):
    """Store a discovered-event candidate (never pollutes real EVENT# records)."""
    table = _get_table()
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    item = {
        'PK': f'CANDIDATE#{candidate_hash}',
        'SK': 'META',
        'GSI5PK': 'REVIEW#pending_discovery_review',
        'GSI5SK': now,
        'created_at': now,
        **{k: _from_plain(v) for k, v in data.items() if v is not None},
    }
    table.put_item(Item=item)
    return candidate_hash


def get_candidates():
    table = _get_table()
    items = _query_all(
        table, IndexName='GSI5',
        KeyConditionExpression=Key('GSI5PK').eq('REVIEW#pending_discovery_review'))
    results = []
    for item in items:
        if not item['PK'].startswith('CANDIDATE#'):
            continue
        result = {'id': item['PK'].split('#', 1)[1]}
        for k, v in item.items():
            if k in ('PK', 'SK') or k.startswith('GSI'):
                continue
            result[k] = _to_plain(v)
        results.append(result)
    return results


# ─── CATEGORY operations ──────────────────────────────────────────

def get_all_categories():
    """Get all categories."""
    table = _get_table()

    items = _scan_all(table, FilterExpression=Attr('PK').begins_with('CATEGORY#') & Attr('SK').eq('META'))

    categories = {}
    for item in items:
        slug = item['PK'].split('#', 1)[1]
        categories[slug] = {
            'slug': slug,
            'name': item.get('name', ''),
            'description': item.get('description', ''),
        }
    return categories


def put_category(slug, data):
    """Create or update a CATEGORY entity."""
    table = _get_table()
    item = {
        'PK': f'CATEGORY#{slug}',
        'SK': 'META',
        **{k: _from_plain(v) for k, v in data.items() if v is not None},
    }
    table.put_item(Item=item)


def delete_category(slug):
    """Delete a CATEGORY entity."""
    table = _get_table()
    table.delete_item(Key={'PK': f'CATEGORY#{slug}', 'SK': 'META'})


# ─── POST operations (free-form /updates posts) ───────────────────

POST_FIELDS = ['title', 'slug', 'body', 'published_on', 'status', 'summary',
               'author', 'created_at', 'updated_at']


def get_all_posts():
    """Every free-form post, drafts included. Newest first."""
    table = _get_table()
    items = _scan_all(table, FilterExpression=Attr('PK').begins_with('POST#')
                      & Attr('SK').eq('META'))
    posts = [_post_item_to_dict(item) for item in items]
    posts.sort(key=lambda p: (p.get('published_on') or '', p.get('slug', '')),
               reverse=True)
    return posts


def get_post(slug):
    """A single free-form post by slug."""
    table = _get_table()
    response = table.get_item(Key={'PK': f'POST#{slug}', 'SK': 'META'})
    item = response.get('Item')
    return _post_item_to_dict(item) if item else None


def put_post(slug, data):
    """Create or update a free-form POST entity."""
    table = _get_table()
    item = {
        'PK': f'POST#{slug}',
        'SK': 'META',
        'slug': slug,
        **{k: _from_plain(v) for k, v in data.items()
           if k in POST_FIELDS and v is not None},
    }
    table.put_item(Item=item)


def delete_post(slug):
    """Delete a free-form POST entity."""
    table = _get_table()
    table.delete_item(Key={'PK': f'POST#{slug}', 'SK': 'META'})


def _post_item_to_dict(item):
    """Convert a DynamoDB POST item to a dict."""
    slug = item['PK'].split('#', 1)[1]
    post = {'slug': slug, 'status': item.get('status', 'draft')}
    for field in POST_FIELDS:
        if field in item and field != 'slug':
            post[field] = _to_plain(item[field])
    return post


# ─── Magic-link rate limiting ─────────────────────────────────────

# /api/submit-link is unauthenticated and sends mail to whatever address it
# is given, so without a limit it is an open relay for filling a stranger's
# inbox. One record per address, self-expiring via the table's `ttl`.
MAGIC_LINK_COOLDOWN_SECONDS = 60
MAGIC_LINK_MAX_PER_DAY = 6


def check_and_record_link_request(email, now=None):
    """Consume one magic-link send for `email`.

    Returns (allowed, retry_after_seconds). Records the send when allowed.
    """
    now = int(now if now is not None else time.time())
    table = _get_table()
    key = {'PK': f'MAGICLINK#{email}', 'SK': 'META'}

    item = (table.get_item(Key=key).get('Item') or {})
    window_start = int(item.get('window_start', 0))
    sent = int(item.get('sent', 0))
    last_sent = int(item.get('last_sent', 0))

    # A day since the window opened resets the allowance.
    if now - window_start >= 86400:
        window_start, sent = now, 0

    if last_sent and now - last_sent < MAGIC_LINK_COOLDOWN_SECONDS:
        return False, MAGIC_LINK_COOLDOWN_SECONDS - (now - last_sent)

    if sent >= MAGIC_LINK_MAX_PER_DAY:
        return False, (window_start + 86400) - now

    table.put_item(Item={
        **key,
        'window_start': window_start,
        'sent': sent + 1,
        'last_sent': now,
        # Outlive the daily window so the counter is not reset early by TTL.
        'ttl': window_start + 86400 * 2,
    })
    return True, 0


# ─── Newsletter opt-in (from the submission form) ─────────────────

def subscribe_to_newsletter(email, contact_list, topic):
    """Opt `email` in to a newsletter topic.

    Called only for addresses already proven — via a magic link or Cognito —
    to belong to the submitter, so this skips the double opt-in confirmation
    the public signup form uses. Raises on failure; callers decide whether a
    newsletter problem should sink the surrounding request.
    """
    ses = boto3.client('sesv2')
    try:
        ses.create_contact(
            ContactListName=contact_list,
            EmailAddress=email,
            TopicPreferences=[
                {'TopicName': topic, 'SubscriptionStatus': 'OPT_IN'}],
        )
        return 'created'
    except ses.exceptions.AlreadyExistsException:
        contact = ses.get_contact(
            ContactListName=contact_list, EmailAddress=email)
        preferences = contact.get('TopicPreferences', [])
        for pref in preferences:
            if pref['TopicName'] == topic:
                pref['SubscriptionStatus'] = 'OPT_IN'
                break
        else:
            preferences.append(
                {'TopicName': topic, 'SubscriptionStatus': 'OPT_IN'})
        ses.update_contact(
            ContactListName=contact_list,
            EmailAddress=email,
            TopicPreferences=preferences,
        )
        return 'updated'


# ─── Trusted submitters ───────────────────────────────────────────

# Trust is keyed by normalized email, not submitter_id: magic-link submitters
# have no Cognito `sub` (their id is "magiclink:<email>"), so email is the only
# identity shared by both auth paths — and the one an admin actually recognizes
# in the queue. Safe because both paths prove control of the address before a
# submission is accepted.

def _trust_key(email):
    return {'PK': f"TRUSTED#{str(email or '').strip().lower()}", 'SK': 'META'}


def is_trusted_submitter(email):
    """True if this address may skip the moderation queue."""
    email = str(email or '').strip().lower()
    if not email:
        return False
    table = _get_table()
    return 'Item' in table.get_item(Key=_trust_key(email))


def trust_submitter(email, trusted_by=None, note=None):
    """Mark an address as trusted. Idempotent."""
    email = str(email or '').strip().lower()
    if not email:
        return None
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    existing = _get_table().get_item(Key=_trust_key(email)).get('Item') or {}
    item = {
        **_trust_key(email),
        'email': email,
        # GSI1 so the admin list is a query rather than a table scan.
        'GSI1PK': 'TRUSTED',
        'GSI1SK': f'EMAIL#{email}',
        'trusted_by': trusted_by or existing.get('trusted_by', ''),
        'trusted_at': existing.get('trusted_at', now),
        'updated_at': now,
    }
    if note:
        item['note'] = note
    _get_table().put_item(Item=item)
    return email


def untrust_submitter(email):
    """Revoke trust. Future submissions go back through the queue."""
    email = str(email or '').strip().lower()
    if not email:
        return False
    _get_table().delete_item(Key=_trust_key(email))
    return True


def list_trusted_submitters():
    """Every trusted address, oldest first."""
    table = _get_table()
    items = _query_all(table, IndexName='GSI1',
                       KeyConditionExpression=Key('GSI1PK').eq('TRUSTED'))
    out = []
    for item in items:
        out.append({
            'email': item.get('email', item['PK'].split('#', 1)[1]),
            'trusted_by': item.get('trusted_by', ''),
            'trusted_at': item.get('trusted_at', ''),
            'note': _to_plain(item.get('note', '')),
        })
    out.sort(key=lambda t: t.get('trusted_at', ''))
    return out


# ─── Draft promotion (shared by the REST admin routes and MCP) ────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')


def promote_draft(draft_id, draft_type, merged):
    """Publish an approved draft: DRAFT# → EVENT# or GROUP#.

    Lives here rather than in routes/admin.py because the MCP Lambda bundles
    db.py but not the route modules — keeping it there would have meant a
    second copy of the approval rules, free to drift from this one.

    Returns the id of the published entity (event guid or group slug).
    """
    if draft_type == 'group':
        slug = slugify(merged.get('name', draft_id))
        group_data = {
            'name': merged.get('name', ''),
            'website': merged.get('website', ''),
            'active': True,
        }
        if merged.get('ical_url') or merged.get('ical'):
            group_data['ical'] = merged.get('ical') or merged.get('ical_url')
        if merged.get('fallback_url'):
            group_data['fallback_url'] = merged['fallback_url']
        if merged.get('categories'):
            group_data['categories'] = merged['categories']
        put_group(slug, group_data)
        return slug

    merged = dict(merged)
    merged.setdefault('id', draft_id)
    return promote_draft_to_event(merged)
