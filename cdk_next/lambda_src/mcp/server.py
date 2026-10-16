#!/usr/bin/env python3
"""
MCP server for the next.dctech.events stack.

Re-implements calgen's MCP tool surface (mcp_server.py) against DynamoDB
instead of YAML files: same tool names and shapes where they apply.
Dropped (not applicable in the DynamoDB world): bootstrap_site,
write_regions, refresh_and_run_pipeline. Added: trigger_rebuild (CodeBuild).

Served over streamable HTTP behind API Gateway with an AWS_IAM authorizer —
trusted agents/Lambdas only, not end users.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import db
# Both copied from packages/calgen by build_lambdas.sh (conftest.py does the
# same for tests), so the re-check parses pages exactly as the aggregator does.
import json_ld
from ssrf_guard import safe_get

USER_AGENT = 'dctech-events/1.0 (+https://dctech.events)'
CODEBUILD_PROJECT_NAME = os.environ.get('CODEBUILD_PROJECT_NAME', '')

# Stateless + JSON responses: required for Lambda (no long-lived SSE streams).
# DNS-rebinding protection is host-header-based and breaks behind API Gateway;
# access control here is SigV4 (AWS_IAM authorizer), so disable it.
mcp = FastMCP(
    'dctech-events-next', stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False),
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


# Category and overlay rules live in db.py, not here: the /edit UI writes
# overlays through the same functions, and a second copy of the rules would let
# the browser path and the agent path drift apart. See the "Event overlays"
# section of db.py.
_check_categories = db.validate_category_slugs

# Compact projection to keep responses well under MCP message size limits.
# Shared by get_events and list_pending_qa; get_event returns the full record.
_EVENT_FIELDS = [
    'guid', 'title', 'date', 'url', 'location', 'location_type',
    'group', 'group_id', 'source', 'categories',
]


def _get_or_404(fetch_fn, id_, label):
    """fetch_fn(id_), raising a consistent ValueError if it comes back falsy.

    Returns the fetched value so callers that need it don't fetch twice.
    """
    result = fetch_fn(id_)
    if not result:
        raise ValueError(f'No such {label}: {id_}')
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Groups
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_groups() -> list:
    """List all meetup groups: slug, name, website, ical feed, categories, active."""
    return [
        {
            'slug': g['id'],
            'name': g.get('name', ''),
            'website': g.get('website', ''),
            'ical': g.get('ical', ''),
            'categories': g.get('categories', []),
            'active': g.get('active', True),
        }
        for g in db.get_all_groups()
    ]


@mcp.tool()
def verify_ical_feed(url: str) -> dict:
    """
    GET an iCal feed URL and check it returns calendar data.

    Always uses GET (some providers, e.g. Meetup.com, 404 on HEAD but work on
    GET). Returns {ok, status_code, reason}.
    """
    try:
        resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=15)
    except Exception as e:
        return {'ok': False, 'status_code': None, 'reason': str(e)}
    ok = resp.status_code == 200 and 'VCALENDAR' in resp.text
    if ok:
        reason = 'ok'
    elif resp.status_code != 200:
        reason = f'HTTP {resp.status_code}'
    else:
        reason = 'response body does not contain VCALENDAR'
    return {'ok': ok, 'status_code': resp.status_code, 'reason': reason}


@mcp.tool()
def add_group(name: str, website: str, ical: str, categories: list, active: bool = True) -> dict:
    """
    Add a new meetup group. `categories` must be valid category slugs — see
    list_categories.
    """
    _check_categories(categories)
    slug = _slugify(name)
    if db.get_group(slug):
        raise ValueError(f'Group already exists: {slug}')
    db.put_group(slug, {
        'name': name,
        'active': active,
        'website': website,
        'ical': ical,
        'categories': categories,
    })
    return {'slug': slug}


@mcp.tool()
def set_group_active(slug: str, active: bool, reason: str | None = None) -> dict:
    """Enable/disable a group (e.g. disable a group with a permanently broken feed)."""
    group = _get_or_404(db.get_group, slug, 'group')
    data = {k: v for k, v in group.items() if k != 'id'}
    data['active'] = active
    if reason:
        data['status_reason'] = reason
    db.put_group(slug, data)
    return {'slug': slug, 'active': active}


# ─────────────────────────────────────────────────────────────────────────────
# Single events
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_single_events() -> list:
    """List all manually-added single events (one-off conferences/events)."""
    events = db.get_all_events(include_past=True)
    return [
        {
            'file_id': e.get('slug', e['guid']),
            'guid': e['guid'],
            'title': e.get('title', ''),
            'date': e.get('date', ''),
            'end_date': e.get('end_date'),
            'url': e.get('url', ''),
            'location': e.get('location', ''),
            'cost': e.get('cost'),
            'categories': e.get('categories', []),
        }
        for e in events
        if e.get('source') in ('manual', 'submitted')
    ]


@mcp.tool()
def add_single_event(
    title: str, date: str, url: str, location: str,
    end_date: str | None = None, cost: str | None = None,
    categories: list | None = None,
) -> dict:
    """
    Add a one-off event. date/end_date are 'YYYY-MM-DD'. Returns the guid
    assigned to this event (so you can attach an overlay to it later).
    """
    from event_utils import calculate_event_hash
    categories = categories or []
    _check_categories(categories)
    date = db.validate_event_date(date)
    end_date = db.validate_event_date(end_date, 'end_date')
    guid = calculate_event_hash(date, '', title, url)
    if db.get_event_from_config(guid):
        raise ValueError(f'Event already exists: {guid}')
    data = {'title': title, 'date': date, 'url': url, 'location': location,
            'slug': f'{date}-{_slugify(title)}', 'categories': categories}
    if end_date:
        data['end_date'] = end_date
    if cost:
        data['cost'] = cost
    db.put_event(guid, data, source='manual', review_status='approved')
    return {'file_id': data['slug'], 'guid': guid}


@mcp.tool()
def update_single_event(guid: str, fields: dict) -> dict:
    """Correct the stored record of an event that has no upstream feed.

    The create/update pair with add_single_event, for fixing what the author
    got wrong. This is *not* the editorial path — use set_overlay for that, so
    the change is attributable and revertible.

    Refuses iCal events: the aggregator rewrites those rows from the feed every
    four hours, so an edit here would report success and vanish. Overlays are
    the only thing that survives, which is what set_overlay writes.
    """
    if 'categories' in fields:
        _check_categories(fields['categories'])
    fields = dict(fields)
    for key in ('date', 'end_date'):
        if key in fields:
            fields[key] = db.validate_event_date(fields[key], key)
    if 'time' in fields:
        fields['time'] = db.validate_event_time(fields['time'])
    event = _get_or_404(db.get_event_from_config, guid, 'event')
    if event.get('source') == 'ical':
        raise ValueError(
            f'{guid} is an iCal event — its record is rewritten from the feed '
            f'every few hours. Use set_overlay to change it.'
        )
    merged = {k: v for k, v in event.items() if k not in ('id', 'guid')}
    merged.update(fields)
    db.put_event(guid, merged, source=event.get('source', 'manual'),
                 review_status=event.get('review_status', 'approved'),
                 created_at=event.get('createdAt'))
    return {'guid': guid}


@mcp.tool()
def delete_single_event(guid: str) -> dict:
    """Delete a single event (e.g. a past event whose URL now 404s)."""
    _get_or_404(db.get_event_from_config, guid, 'event')
    db.delete_event(guid)
    return {'deleted': guid}


# ─────────────────────────────────────────────────────────────────────────────
# Recurring events
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_recurring_events() -> list:
    """List recurring events with their RRULEs."""
    return [
        {
            'file_id': e['id'],
            'title': e.get('title', ''),
            'date': e.get('date', ''),
            'time': e.get('time'),
            'url': e.get('url', ''),
            'location': e.get('location', ''),
            'cost': e.get('cost'),
            'categories': e.get('categories', []),
            'rrule': e.get('rrule', ''),
            'description': e.get('description'),
        }
        for e in db.get_all_recurring_events()
    ]


@mcp.tool()
def add_recurring_event(
    title: str, date: str, rrule: str, url: str, location: str,
    time: str | None = None, cost: str | None = None,
    categories: list | None = None, description: str | None = None,
) -> dict:
    """
    Add a recurring event.

    `date` ('YYYY-MM-DD') is the first/anchor occurrence. `rrule` is an
    RFC-5545 RRULE string, e.g. 'FREQ=WEEKLY;BYDAY=TU' or
    'FREQ=MONTHLY;BYDAY=3TH' (3rd Thursday monthly). Occurrences are expanded
    up to 90 days out by the site build.
    """
    categories = categories or []
    _check_categories(categories)
    date = db.validate_event_date(date)
    time = db.validate_event_time(time)
    if not rrule.strip():
        raise ValueError('rrule must be a non-empty RFC-5545 RRULE string')
    file_id = _slugify(title)
    if db.get_recurring_event(file_id):
        raise ValueError(f'Recurring event already exists: {file_id}')
    data = {'title': title, 'date': date, 'url': url, 'location': location,
            'categories': categories, 'rrule': rrule}
    if time:
        data['time'] = time
    if cost:
        data['cost'] = cost
    if description:
        data['description'] = description
    db.put_recurring_event(file_id, data)
    return {'file_id': file_id}


@mcp.tool()
def update_recurring_event(file_id: str, fields: dict) -> dict:
    """Update fields (including `rrule`) on an existing recurring event.

    A full merge-and-replace onto the record's top-level fields — the same
    operation an approved recurring_series correction performs via
    db.merge_recurring_event_fields. No allowlist here (unlike a public
    correction's RECURRING_CORRECTION_FIELDS): this tool is trusted.
    """
    if 'categories' in fields:
        _check_categories(fields['categories'])
    db.merge_recurring_event_fields(file_id, fields)
    return {'file_id': file_id}


@mcp.tool()
def delete_recurring_event(file_id: str) -> dict:
    """Delete a recurring event (e.g. a series that has ended)."""
    _get_or_404(db.get_recurring_event, file_id, 'recurring event')
    db.delete_recurring_event(file_id)
    return {'deleted': file_id}


@mcp.tool()
def get_recurring_instance_overrides(file_id: str) -> dict:
    """Every approved per-occurrence correction for one recurring series:
    {date: {field: value, ...}}. Empty if none."""
    return db.get_recurring_instance_overrides(file_id)


# ─────────────────────────────────────────────────────────────────────────────
# Categories
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_categories() -> list:
    """List valid category slugs with their display name and description."""
    return [
        {'slug': slug, 'name': cat.get('name', slug), 'description': cat.get('description', '')}
        for slug, cat in db.get_all_categories().items()
    ]


@mcp.tool()
def add_category(slug: str, name: str, description: str) -> dict:
    """Add a new category definition. Setup-time only."""
    if slug in db.get_all_categories():
        raise ValueError(f'Category already exists: {slug}')
    db.put_category(slug, {'name': name, 'description': description})
    return {'slug': slug}


# ─────────────────────────────────────────────────────────────────────────────
# Overlays (stored as the `overrides` field on EVENT items)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_overlay(guid: str) -> dict:
    """Return the overlay (overrides) fields for one event guid (empty dict if none)."""
    return db.get_event_overlay(guid)


@mcp.tool()
def set_overlay(guid: str, fields: dict, comment: str,
                run_id: str | None = None) -> dict:
    """
    Merge `fields` into the event's `overrides`, preserving existing overlay
    fields. `comment` is recorded alongside as `_comment`.

    Editable fields: title, time, end_time, location, location_type, url, cost,
    city, state, description, categories, all_day, hidden, duplicate_of.
    Protected fields (set by the pipeline from the source feed, not
    overridable) are rejected: group, group_id, group_website, date,
    end_date, guid, source.

    `duplicate_of` must name an event that exists — a dangling one is silently
    ignored at render, so the merge would look done and the event would keep
    showing.

    `location_type` must be 'physical', 'virtual' or 'hybrid'. Only 'virtual'
    changes where the event shows (it leaves the week pages, the newsletter and
    the announcement queues and appears on /virtual/); anything else is rejected
    rather than stored, because an unrecognised value would match neither case.

    `run_id` tags the write as part of a batch (the QA agent's weekly pass),
    recording each field's prior value so revert_qa_run can restore it. Pass
    it for automated writes; leave it unset for hand edits.
    """
    result = db.set_event_overlay(guid, fields, comment, run_id=run_id)
    return {'guid': result['guid'], 'overlay': result['overlay']}


@mcp.tool()
def get_event(guid: str) -> dict:
    """
    Return the full stored record for one event, including `description` and
    `overrides` — fields that get_events omits from its compact projection.

    Use this to pull detail on the handful of events you are actually making a
    decision about; use get_events for the bulk corpus.
    """
    return _get_or_404(db.get_event_from_config, guid, 'event')


@mcp.tool()
def list_pending_qa(limit: int | None = 200, include_past: bool = False) -> list:
    """
    Events awaiting quality-control review (review_status=pending_qa, GSI5) —
    the QA agent's work queue, soonest first.

    Events dated before today (Eastern time) are left out unless
    include_past=True. They can no longer affect the site, and because the queue
    is ordered by date they would otherwise sit at the front of every batch:
    iCal events enter the queue as pending_qa and only resolve_qa_review takes
    them out, so the stale tail grows by a day every day.

    This is the list of events to make decisions *about*. It is not the set to
    compare against: duplicate detection needs the full corpus from get_events,
    because the other half of a duplicate pair was usually approved in an
    earlier run and has already left this queue.
    """
    events = db.get_events_by_review_status(
        'pending_qa', limit=limit, include_past=include_past)
    return [{k: e.get(k) for k in _EVENT_FIELDS} for e in events]


@mcp.tool()
def resolve_qa_review(guid: str, status: str) -> dict:
    """
    Take an event out of the pending_qa queue once it has been reviewed:
    'approved' (checked, nothing further needed) or 'flagged' (needs a human).

    Overlay fixes are recorded separately via set_overlay — an approved event
    may well carry overlays; approval means "reviewed", not "unchanged".

    Also stamps last_checked, since reviewing a newly-arrived event is as
    fresh a look as the two-week re-check — otherwise every event a run
    approves comes straight back as the head of list_events_due_for_check.
    """
    if status not in ('approved', 'flagged'):
        raise ValueError(f"status must be 'approved' or 'flagged', got: {status!r}")
    _get_or_404(db.get_event_from_config, guid, 'event')
    db.set_event_review_status(guid, status)
    db.mark_event_checked(guid, 'ok' if status == 'approved' else 'flagged',
                          'initial QA review')
    return {'guid': guid, 'review_status': status}


# ─────────────────────────────────────────────────────────────────────────────
# Two-week re-check
#
# pending_qa looks at an event once, when it first appears. Pages change after
# that — an in-person meetup goes online, a venue moves, a talk is cancelled —
# and nothing re-read them: the aggregator's JSON-LD cache kept its first read
# forever (next_dctech_events-ba5). These three tools are the QC agent's loop
# over every upcoming event: list what is due, check each against its page,
# stamp what was checked.
# ─────────────────────────────────────────────────────────────────────────────

_SITE_TZ = ZoneInfo('America/New_York')
_CHECK_FIELDS = _EVENT_FIELDS + ['time', 'last_checked', 'last_check_result']


def _is_suppressed(event):
    overlay = event.get('overrides') or {}
    return bool(event.get('hidden') or overlay.get('hidden')
                or event.get('duplicate_of') or overlay.get('duplicate_of'))


@mcp.tool()
def list_events_due_for_check(older_than_days: int = 14, limit: int | None = 40,
                              source: str | None = None) -> dict:
    """
    Upcoming events whose source page has not been re-checked in
    `older_than_days` days (never-checked events are always due), soonest
    event first — the QC agent's re-check queue.

    Hidden and duplicate events are left out (nothing a page says changes
    them), as are events with no url (there is no page to check). `source`
    narrows to 'ical' | 'manual' | 'submitted' | 'recurring'.

    Returns {events, due_total}; due_total counts every due event, not just the
    `limit` returned, so a growing backlog is visible.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)
              ).strftime('%Y-%m-%dT%H:%M:%SZ')
    due = [
        e for e in db.get_all_events()
        if e.get('url') and not _is_suppressed(e)
        and (not source or e.get('source') == source)
        and (e.get('last_checked') or '') < cutoff
    ]
    events = [{k: e.get(k) for k in _CHECK_FIELDS} for e in due]
    if limit is not None:
        events = events[:limit]
    return {'events': events, 'due_total': len(due)}


def _norm_title(title):
    return ' '.join(str(title or '').split()).casefold()


def _page_start(start_date):
    """('YYYY-MM-DD', 'HH:MM' or None) in site time, from a schema.org startDate."""
    if not start_date:
        return None, None
    try:
        dt = datetime.fromisoformat(str(start_date).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        return dt.date().isoformat(), dt.strftime('%H:%M')
    local = dt.astimezone(_SITE_TZ)
    return local.date().isoformat(), local.strftime('%H:%M')


def _source_diffs(raw, shown, http_status, page):
    """What the page says that the listing does not. Deliberately coarse —
    the agent decides what each one means."""
    diffs = []
    if http_status in (404, 410):
        diffs.append({'field': 'page_gone', 'stored': None, 'page': http_status})
    if not page:
        return diffs
    if page['cancelled']:
        diffs.append({'field': 'cancelled', 'stored': False, 'page': True})
    elif page.get('event_status') in ('EventPostponed', 'EventRescheduled', 'EventMovedOnline'):
        diffs.append({'field': 'event_status', 'stored': None, 'page': page['event_status']})
    shown_virtual = shown.get('location_type') == 'virtual'
    # A page with neither a location nor an attendance mode says nothing about
    # format, so it can't contradict the listing either way.
    page_states_format = (page['is_virtual'] or page.get('location')
                          or page.get('attendance_mode'))
    if page_states_format and page['is_virtual'] != shown_virtual:
        diffs.append({'field': 'location_type', 'stored': shown.get('location_type'),
                      'page': 'virtual' if page['is_virtual'] else 'physical'})
    # Title against the *source* title: QC polish overlays retitle on purpose,
    # and those should not read as the page having changed.
    if page.get('title') and _norm_title(page['title']) != _norm_title(raw.get('title')):
        diffs.append({'field': 'title', 'stored': raw.get('title'), 'page': page['title']})
    page_date, page_time = _page_start(page.get('start_date'))
    if page_date and page_date != shown.get('date'):
        diffs.append({'field': 'date', 'stored': shown.get('date'), 'page': page_date})
    if page_time and shown.get('time') and not shown.get('all_day') \
            and page_time != str(shown.get('time'))[:5]:
        diffs.append({'field': 'time', 'stored': shown.get('time'), 'page': page_time})
    return diffs


@mcp.tool()
def check_event_source(guid: str) -> dict:
    """
    Re-read one event's page and report how it differs from the listing.

    Fetches the url server-side and parses its schema.org Event JSON-LD — the
    structured data WebFetch drops when it converts a page to markdown, and the
    same data the iCal aggregator reads. Compares against the event as the site
    shows it (overlay applied), except `title`, which is compared against the
    source title so a deliberate polish overlay is not reported as a change.

    Returns {guid, source, url, http_status, final_url, error, page, stored,
    diffs, cache_refreshed}. `page` is null when the page had no Event JSON-LD
    (common for blocked or interstitial responses). Each diff is
    {field, stored, page}; fields are page_gone, cancelled, event_status,
    location_type, title, date, time.

    For iCal events on Meetup/Luma, a successful read also replaces that url's
    entry in the aggregator's JSON-LD cache (cache_refreshed=true), so the next
    aggregator run — every 4 hours — corrects location_type/title or drops a
    cancelled event at the source. Writes nothing else: no overlay, no stamp.
    """
    raw = _get_or_404(db.get_event_from_config, guid, 'event')
    shown = dict(raw)
    for key, value in (raw.get('overrides') or {}).items():
        if not key.startswith('_') and key not in db.OVERLAY_PROTECTED_FIELDS:
            shown[key] = value
    url = shown.get('url') or raw.get('url')
    result = {
        'guid': guid, 'source': raw.get('source'), 'url': url,
        'http_status': None, 'final_url': None, 'error': None, 'page': None,
        'stored': {k: shown.get(k) for k in
                   ('title', 'date', 'time', 'location', 'location_type')},
        'diffs': [], 'cache_refreshed': False,
    }
    if not url:
        result['error'] = 'event has no url'
        return result
    try:
        resp = safe_get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
        return result
    result['http_status'] = resp.status_code
    result['final_url'] = getattr(resp, 'url', None)
    page = json_ld.parse_event_json_ld(resp.text) if resp.status_code == 200 else None
    result['page'] = page
    result['diffs'] = _source_diffs(raw, shown, resp.status_code, page)

    # Only entries the aggregator actually reads: its own url, on a host it
    # fetches JSON-LD for. Anything else would just grow a 400KB-capped item.
    if (page is not None and raw.get('source') == 'ical'
            and url == raw.get('url') and json_ld.is_json_ld_host(url)):
        url_hash = hashlib.md5(url.encode('utf-8'), usedforsecurity=False).hexdigest()
        db.put_jsonld_cache_entry(url_hash, json_ld.cache_entry(page))
        result['cache_refreshed'] = True
    return result


@mcp.tool()
def mark_events_checked(results: list) -> dict:
    """
    Stamp last_checked on events the re-check has looked at, taking them off
    list_events_due_for_check for the next 14 days. Call once at the end of a
    pass with every event looked at — including fetch failures, or a page that
    always blocks us would sit at the front of the queue forever.

    Each entry is {guid, result, note?}; result is one of:
      ok            page matches the listing
      refreshed     iCal event whose cache check_event_source refreshed; the
                    aggregator will apply the change itself
      fixed         an overlay was written
      flagged       left for a human (resolve_qa_review 'flagged')
      fetch_failed  page could not be read

    Does not trigger a site rebuild. Returns {marked, missing} — missing lists
    guids that no longer exist (pruned since they were listed).
    """
    for entry in results:
        if not isinstance(entry, dict) or not entry.get('guid'):
            raise ValueError(f'each result needs a guid: {entry!r}')
        if entry.get('result') not in db.CHECK_RESULTS:
            raise ValueError(f"result must be one of {list(db.CHECK_RESULTS)}, "
                             f"got: {entry.get('result')!r} for {entry['guid']}")
    marked, missing = [], []
    for entry in results:
        ok = db.mark_event_checked(entry['guid'], entry['result'], entry.get('note'))
        (marked if ok else missing).append(entry['guid'])
    return {'marked': marked, 'missing': missing}


# ─────────────────────────────────────────────────────────────────────────────
# QA run bookkeeping
#
# The weekly QA agent writes overlays directly rather than opening something
# reviewable, so these two tools are what replaces the old workflow's pull
# request: list what a run changed, and undo the whole run if it got it wrong.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_qa_run(run_id: str) -> list:
    """
    List every overlay written by one QA agent run — what it changed, and what
    each field was before. Pair with revert_qa_run to undo the run.
    """
    return db.describe_qa_run(run_id)


@mcp.tool()
def revert_qa_run(run_id: str) -> dict:
    """
    Undo every overlay written by one QA agent run: fields the run introduced
    are removed, fields it overwrote are restored to their previous value.

    Overlay fields written by other runs or by hand are left alone, so this is
    safe on an event that has been edited since — for those fields. A field the
    run wrote and a human has since overwritten is restored to the run's
    snapshot, losing the human's value.
    """
    return db.revert_qa_run(run_id)


# ─────────────────────────────────────────────────────────────────────────────
# Events + rebuild
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_events(
    source: str | None = None, date_from: str | None = None,
    limit: int | None = 500,
) -> list:
    """
    Read active events from DynamoDB (GSI4), optionally filtered by:
      source     'ical' | 'manual' | 'submitted' | 'recurring'
      date_from  'YYYY-MM-DD', inclusive (default: today onward)
      limit      cap on returned events (default 500, None = no cap)

    Returns a compact projection of each event (guid, title, date, url,
    location, location_type, group, group_id, source, categories).
    """
    events = db.get_all_events(include_past=bool(date_from))
    if source:
        events = [e for e in events if e.get('source') == source]
    if date_from:
        events = [e for e in events if e.get('date', '') >= date_from]
    events = [{k: e.get(k) for k in _EVENT_FIELDS} for e in events]
    if limit is not None:
        events = events[:limit]
    return events


@mcp.tool()
def trigger_rebuild() -> dict:
    """Start an on-demand CodeBuild run of the static site generator."""
    if not CODEBUILD_PROJECT_NAME:
        raise ValueError('Site generator not configured yet')
    import boto3
    codebuild = boto3.client('codebuild')
    build = codebuild.start_build(projectName=CODEBUILD_PROJECT_NAME)
    return {'build_id': build['build']['id']}


# ─────────────────────────────────────────────────────────────────────────────
# Submissions
#
# Mirrors the /submit and /api/admin routes so the queue can be filled and
# triaged from an agent instead of the browser. Both paths call the same db.*
# functions, so a draft created here is indistinguishable from one the web form
# created, and approving here is byte-for-byte what approving in the /edit UI
# does.
#
# Unlike the REST routes there is no per-user admin check: this endpoint is
# IAM-authed for trusted callers within the account, which is the same trust
# boundary that already lets add_single_event and set_overlay publish directly.
# ─────────────────────────────────────────────────────────────────────────────

MCP_REVIEWER = 'mcp:agent'


@mcp.tool()
def submit_event(
    title: str, date: str, url: str, submitter_email: str,
    time: str | None = None, end_date: str | None = None,
    location: str | None = None, city: str | None = None,
    state: str | None = None, cost: str | None = None,
    description: str | None = None, all_day: bool = False,
    categories: list | None = None,
) -> dict:
    """
    Queue an event for moderator review, the way the public /submit form does.

    Use this — not add_single_event — when the event came from somewhere you
    have not vetted (a scraped calendar, a third-party listing, a tip). This
    always lands in the pending queue for a human; add_single_event publishes
    immediately. Trusted-submitter auto-approval deliberately does not apply
    here: the submitter address is whoever the event is *credited* to, not
    proof that they asked for it to be listed.

    date/end_date are 'YYYY-MM-DD'; time is 24-hour 'HH:MM' local, omitted for
    all-day events. Returns the draft_id to pass to approve_submission.
    """
    categories = categories or []
    _check_categories(categories)

    draft_data, error = db.build_event_draft_data({
        'title': title,
        'date': date,
        'time': time or '',
        'timing': 'allday' if all_day else 'specific',
        'url': url,
        'end_date': end_date or '',
        'location': location or '',
        'city': city or '',
        'state': state or '',
        'cost': cost or '',
        'description': description or '',
        'categories': categories,
    })
    if error:
        raise ValueError(error)

    email = str(submitter_email or '').strip().lower()
    if '@' not in email:
        raise ValueError(f'{submitter_email!r} is not a valid email address')

    draft_id = db.create_draft('event', draft_data, email)
    return {'draft_id': draft_id, 'status': 'pending', 'title': draft_data['title'],
            'date': draft_data['date'], 'submitter_email': email}


@mcp.tool()
def list_pending_submissions() -> list:
    """List submissions awaiting review, newest first.

    Each entry includes `submitter_trusted` so you can tell at a glance whether
    this person's future events would already publish without review.
    """
    drafts = db.get_drafts_by_status('pending')
    for draft in drafts:
        draft['submitter_trusted'] = db.is_trusted_submitter(
            draft.get('submitter_email', ''))
    return drafts


@mcp.tool()
def get_submission(draft_id: str) -> dict:
    """Full detail for one submission, including the submitter's history.

    `submitter_history` is every other submission from the same address, which
    is the evidence you want before deciding whether to trust someone.
    """
    draft = _get_or_404(db.get_draft, draft_id, 'submission')

    email = draft.get('submitter_email', '')
    draft['submitter_trusted'] = db.is_trusted_submitter(email)

    submitter_id = draft.get('submitter_id') or email
    history = [d for d in db.get_drafts_by_submitter(submitter_id)
               if d.get('id') != draft_id]
    draft['submitter_history'] = [
        {k: d.get(k) for k in ('id', 'title', 'name', 'date', 'status',
                               'created_at')}
        for d in history
    ]
    return draft


@mcp.tool()
def approve_submission(draft_id: str, categories: list | None = None,
                       trust_submitter: bool = False) -> dict:
    """Approve a pending submission and publish it.

    categories: overrides the submitted ones (validated against real slugs).
    trust_submitter: also mark the submitter trusted, so their future *events*
      publish automatically. Group submissions always keep getting reviewed.
    """
    draft = _get_or_404(db.get_draft, draft_id, 'submission')
    if draft.get('status') != 'pending':
        raise ValueError(
            f"Submission {draft_id} is already {draft.get('status')!r}; "
            'only pending submissions can be approved'
        )

    overrides = {} if categories is None else {'categories': categories}
    merged = db.merge_draft_for_approval(draft, overrides)

    draft_type = draft.get('draft_type', 'event')
    published_id = db.promote_draft(draft_id, draft_type, merged)
    db.update_draft_status(draft_id, 'APPROVED', MCP_REVIEWER)

    result = {'draft_id': draft_id, 'draft_type': draft_type,
              'published_id': published_id, 'trusted': None}

    if trust_submitter:
        email = str(draft.get('submitter_email') or '').strip().lower()
        if email:
            db.trust_submitter(email, trusted_by=MCP_REVIEWER)
            result['trusted'] = email
        else:
            result['warning'] = 'Could not trust: draft has no submitter email'

    return result


@mcp.tool()
def reject_submission(draft_id: str, reason: str | None = None) -> dict:
    """Reject a pending submission. It is not published and stays on record."""
    draft = _get_or_404(db.get_draft, draft_id, 'submission')
    if draft.get('status') != 'pending':
        raise ValueError(
            f"Submission {draft_id} is already {draft.get('status')!r}; "
            'only pending submissions can be rejected'
        )

    db.update_draft_status(draft_id, 'REJECTED', MCP_REVIEWER)
    return {'draft_id': draft_id, 'status': 'rejected', 'reason': reason}


@mcp.tool()
def list_trusted_submitters() -> list:
    """Addresses whose event submissions publish without review."""
    return db.list_trusted_submitters()


@mcp.tool()
def trust_submitter(email: str, note: str | None = None) -> dict:
    """Trust an address, so their future events publish without review."""
    email = str(email or '').strip().lower()
    if '@' not in email:
        raise ValueError(f'{email!r} is not a valid email address')
    db.trust_submitter(email, trusted_by=MCP_REVIEWER, note=note)
    return {'trusted': email}


@mcp.tool()
def untrust_submitter(email: str) -> dict:
    """Revoke trust. Their future submissions go back through the queue."""
    email = str(email or '').strip().lower()
    if not db.is_trusted_submitter(email):
        raise ValueError(f'{email!r} is not currently trusted')
    db.untrust_submitter(email)
    return {'untrusted': email}


# ─────────────────────────────────────────────────────────────────────────────
# Public corrections
#
# A correction proposes a change to one existing, already-published event —
# distinct from submit_event, which proposes a whole new one. Approval merges
# the proposed fields into the event's overlay via set_event_overlay, so an
# approved correction is indistinguishable from a hand edit made in /edit.
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_pending_corrections() -> list:
    """Public corrections awaiting moderation, newest first."""
    return db.get_corrections_by_status('pending')


@mcp.tool()
def get_correction(correction_id: str) -> dict:
    """Full detail for one correction, including the target event's guid,
    source, and the fields it proposes to change."""
    return _get_or_404(db.get_correction, correction_id, 'correction')


@mcp.tool()
def approve_correction(correction_id: str) -> dict:
    """Approve a pending correction — against an event, a recurring series'
    definition, or one occurrence of a recurring series (see the
    correction's `target_type`).

    Re-validates the fields against the target's *current* state, not its
    state at submission time — raises if the target was re-sourced, edited,
    or removed in between and the fields are no longer all allowed.
    """
    return db.approve_correction(correction_id, MCP_REVIEWER)


@mcp.tool()
def reject_correction(correction_id: str, reason: str | None = None) -> dict:
    """Reject a pending correction. No overlay write; the event is unchanged."""
    return db.reject_correction(correction_id, MCP_REVIEWER, reason)


# ─────────────────────────────────────────────────────────────────────────────
# Discovery proposals
#
# The discovery agent proposes; it never publishes. These write DRAFT# items
# into the same queue human submissions land in, tagged with a reserved
# submitter address so agent proposals are distinguishable at a glance.
# ─────────────────────────────────────────────────────────────────────────────

DISCOVERY_SUBMITTER = 'discovery-agent@dctech.events'


@mcp.tool()
def propose_group(name: str, website: str, ical: str, categories: list,
                  source_url: str, evidence: str) -> dict:
    """
    Propose a newly-discovered group for review. The iCal feed is verified
    before the proposal is accepted — a group whose feed does not parse is
    worth nothing to the aggregator.

    source_url: the page you found this group on.
    evidence: one or two sentences on why this belongs on dctech.events.
    """
    _check_categories(categories)
    slug = _slugify(name)
    if db.get_group(slug):
        raise ValueError(f'Group already exists: {slug}')

    check = verify_ical_feed(ical)
    if not check['ok']:
        raise ValueError(f"iCal feed did not verify ({check['reason']}): {ical}")

    draft_id = db.create_draft('group', {
        'name': name,
        'website': website,
        'ical_url': ical,
        'categories': categories,
        # Provenance rides in `description` because _draft_item_to_dict
        # whitelists readable fields and would drop a custom key.
        'description': f'Discovered at {source_url}\n\n{evidence}',
    }, submitter_email=DISCOVERY_SUBMITTER)
    return {'draft_id': draft_id, 'proposed_slug': slug}


@mcp.tool()
def propose_event(title: str, date: str, url: str, location: str,
                  source_url: str, evidence: str,
                  end_date: str | None = None, cost: str | None = None,
                  categories: list | None = None) -> dict:
    """
    Propose a one-off event (a conference, a summit — something with no group
    feed behind it) for review. date/end_date are 'YYYY-MM-DD'.

    Recurring meetups should be proposed as a *group* instead: one accepted
    feed keeps paying out every month, one accepted event pays out once.
    """
    categories = categories or []
    _check_categories(categories)
    # This tool calls db.create_draft directly rather than going through
    # build_event_draft_data, so it does not inherit that function's checks.
    date = db.validate_event_date(date)
    end_date = db.validate_event_date(end_date, 'end_date')

    from event_utils import calculate_event_hash
    if db.get_event_from_config(calculate_event_hash(date, '', title, url)):
        raise ValueError(f'Event already on the calendar: {title} on {date}')

    data = {'title': title, 'date': date, 'url': url, 'location': location,
            'categories': categories, 'end_date': end_date, 'cost': cost,
            'description': f'Discovered at {source_url}\n\n{evidence}'}
    draft_id = db.create_draft(
        'event', {k: v for k, v in data.items() if v is not None},
        submitter_email=DISCOVERY_SUBMITTER)
    return {'draft_id': draft_id}


@mcp.tool()
def list_discovery_proposals(limit: int | None = 200) -> list:
    """
    Every proposal this agent has made — pending, approved, and rejected.

    This is the agent's memory. A rejected proposal is a human saying "not
    this"; re-proposing it next week is the fastest way to make the whole
    feature annoying enough to switch off.
    """
    drafts = db.get_drafts_by_submitter(DISCOVERY_SUBMITTER)
    return [{k: d.get(k) for k in
             ('id', 'draft_type', 'status', 'title', 'name', 'url',
              'website', 'ical_url', 'created_at')}
            for d in drafts[:limit]]
