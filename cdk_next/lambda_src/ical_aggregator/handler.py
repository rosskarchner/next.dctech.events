"""iCal Aggregator Lambda.

Imports calgen.calendars.fetch_ical_and_extract_events directly (no fork)
via a /tmp-scratch adapter: seeds the cache/meta files calgen expects from
DynamoDB at invocation start, lets calgen's function do its normal file I/O
(including its 4-hour fetch throttle and ETag handling), then persists
results back to ICAL#{group_id}/META cache items and EVENT#{guid}
(source='ical') items. Reads the group list from DynamoDB, not _groups/*.yaml.

Also persists the JSON-LD page-metadata cache (one CACHE#jsonld blob item)
so Meetup pages aren't refetched on every 4-hour run.
"""
import json
import os
import shutil
from datetime import date as date_type

SITE_DIR = '/tmp/calgen-site'  # nosec - Lambda scratch space

# calgen reads config.yaml at import time relative to CALGEN_SITE_DIR, so the
# scratch site dir must exist before any calgen import.
os.makedirs(SITE_DIR, exist_ok=True)
if not os.path.exists(os.path.join(SITE_DIR, 'config.yaml')):
    with open(os.path.join(SITE_DIR, 'config.yaml'), 'w') as f:
        f.write('site_name: DC Tech Events\n'
                'base_url: https://dctech.events\n'
                'timezone: US/Eastern\n')
os.environ['CALGEN_SITE_DIR'] = SITE_DIR
os.chdir(SITE_DIR)

from calgen.calendars import fetch_ical_and_extract_events  # noqa: E402
from calgen.event_utils import calculate_event_hash  # noqa: E402

import db  # noqa: E402

ICAL_CACHE_DIR = os.path.join(SITE_DIR, '_cache', 'ical')
JSON_LD_CACHE_DIR = os.path.join(SITE_DIR, '_cache', 'json-ld')

JSONLD_CACHE_KEY = {'PK': 'CACHE#jsonld', 'SK': 'META'}


def _seed_caches(groups):
    """Materialize calgen's expected cache files from DynamoDB items."""
    os.makedirs(ICAL_CACHE_DIR, exist_ok=True)
    os.makedirs(JSON_LD_CACHE_DIR, exist_ok=True)

    for group in groups:
        cache = db.get_ical_cache(group['id'])
        if not cache:
            continue
        with open(os.path.join(ICAL_CACHE_DIR, f"{group['id']}.json"), 'w') as f:
            json.dump(cache['events'], f)
        with open(os.path.join(ICAL_CACHE_DIR, f"{group['id']}.meta"), 'w') as f:
            json.dump(cache['meta'], f)

    table = db._get_table()
    item = table.get_item(Key=JSONLD_CACHE_KEY).get('Item')
    if item:
        for url_hash, data in db._to_plain(item.get('entries', {})).items():
            with open(os.path.join(JSON_LD_CACHE_DIR, f'{url_hash}.json'), 'w') as f:
                json.dump(data, f)


def _persist_jsonld_cache():
    entries = {}
    for filename in os.listdir(JSON_LD_CACHE_DIR):
        if filename.endswith('.json'):
            try:
                with open(os.path.join(JSON_LD_CACHE_DIR, filename)) as f:
                    entries[filename[:-5]] = json.load(f)
            except Exception:
                continue
    import time as _time
    table = db._get_table()
    current = table.get_item(Key=JSONLD_CACHE_KEY).get('Item')
    if current and db._to_plain(current.get('entries', {})) == entries:
        return  # unchanged — skip the (large) rewrite
    table.put_item(Item={
        **JSONLD_CACHE_KEY,
        'entries': db._from_plain(entries),
        'ttl': int(_time.time()) + 7 * 86400,
    })


def _persist_group(group, events):
    """Write the group's fresh cache + materialized EVENT# items; prune stale ones."""
    gid = group['id']
    meta = {}
    meta_file = os.path.join(ICAL_CACHE_DIR, f'{gid}.meta')
    if os.path.exists(meta_file):
        with open(meta_file) as f:
            meta = json.load(f)

    # Compare before writing: every DynamoDB put emits a stream record even
    # when the item is unchanged, and the site-build trigger listens for
    # ICAL#/EVENT# records — so a quiet feed must produce zero writes or
    # every aggregator run forces a no-op rebuild. Skipping the cache write
    # also skips persisting fresh fetch meta (etag/last_fetch), which only
    # means the next run re-fetches — the run cadence already matches
    # calgen's 4-hour throttle, so nothing is lost.
    writes = 0
    existing_cache = db.get_ical_cache(gid)
    if existing_cache is None or existing_cache.get('events') != events:
        db.put_ical_cache(gid, events, meta)
        writes += 1

    today = date_type.today().isoformat()
    suppress = group.get('suppress_urls') or []
    if isinstance(suppress, bool):
        suppress = []

    fresh_guids = set()
    for ev in events:
        if not ev.get('date') or ev['date'] < today:
            continue
        if ev.get('url', '') in suppress:
            continue
        guid = calculate_event_hash(ev.get('date', ''), ev.get('time', ''),
                                    ev.get('title', ''), ev.get('url'))
        fresh_guids.add(guid)
        existing = db.get_event_from_config(guid)
        data = dict(ev)
        data['group'] = group.get('name', gid)
        data['group_id'] = gid
        data['group_website'] = group.get('website', '')
        data['categories'] = list(group.get('categories', []))
        if existing:
            # Preserve moderation/overlay state on refresh
            for keep in ('overrides', 'hidden', 'duplicate_of', 'review_status'):
                if existing.get(keep) is not None:
                    data.setdefault(keep, existing[keep])
            if (existing.get('source') == 'ical'
                    and all(existing.get(k) == v for k, v in data.items())):
                continue  # identical item — don't rewrite, don't wake the trigger
        # New iCal events land in pending_qa — the QA agent's work queue (GSI5).
        # Refreshed events keep whatever status they already had, via the
        # setdefault above. This does not gate publication: get_all_events
        # queries GSI4 and never looks at review_status, so the event is live
        # on the site immediately and QC is a cleanup pass over it.
        db.put_event(guid, data, source='ical',
                     review_status=data.get('review_status', 'pending_qa'),
                     created_at=(existing or {}).get('createdAt'))
        writes += 1
    return fresh_guids, writes


def _prune_stale_ical_events(processed_group_ids, fresh_guids):
    """Delete future source='ical' events for processed groups that vanished
    from their feeds (cancelled/rescheduled)."""
    removed = 0
    for ev in db.get_all_events():
        if ev.get('source') != 'ical':
            continue
        if ev.get('group_id') not in processed_group_ids:
            continue
        if ev['guid'] not in fresh_guids:
            db.delete_event(ev['guid'])
            removed += 1
    return removed


def lambda_handler(event, context):
    only_groups = set((event or {}).get('groups', []))

    groups = [g for g in db.get_all_groups()
              if g.get('active', True) and g.get('ical')]
    if only_groups:
        groups = [g for g in groups if g['id'] in only_groups]

    _seed_caches(groups)

    processed = set()
    all_fresh = set()
    total_writes = 0
    errors = []
    for group in groups:
        if context and context.get_remaining_time_in_millis() < 60_000:
            errors.append('time budget exhausted; remaining groups skipped')
            break
        try:
            events = fetch_ical_and_extract_events(group['ical'], group['id'], group)
            if events is None:
                continue  # fetch failed with no cache — nothing to persist
            fresh, writes = _persist_group(group, events)
            all_fresh |= fresh
            total_writes += writes
            processed.add(group['id'])
        except Exception as e:
            errors.append(f"{group['id']}: {e}")

    pruned = 0
    if processed and not only_groups:
        pruned = _prune_stale_ical_events(processed, all_fresh)

    _persist_jsonld_cache()
    # Keep /tmp tidy across warm invocations (caches re-seed from DynamoDB)
    shutil.rmtree(os.path.join(SITE_DIR, '_cache'), ignore_errors=True)

    result = {
        'groups_processed': len(processed),
        'events_fresh': len(all_fresh),
        'items_written': total_writes,
        'stale_pruned': pruned,
        'errors': errors[:20],
    }
    print(json.dumps(result))
    return result
