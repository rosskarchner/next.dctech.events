#!/usr/bin/env python3
"""One-time migration/seeding of the dctech-events-next table.

Consolidates three sources:
  1. Git-committed YAML in the production dctech.events checkout
     (_groups/, _categories/, _single_events/, _recurring_events/, _overlay/)
     — authoritative; loaded via calgen's own pipeline loaders so guids stay
     stable (calgen.event_utils.calculate_event_hash, same formula).
  2. The current `dctech-events` DynamoDB table — drafts copy verbatim;
     EVENT# items import only where their guid doesn't collide with a
     YAML-derived event (YAML wins).
  3. The legacy `DcTechEvents` table — audited/counted only, not imported.

Also performs a live iCal fetch (via calgen.calendars, honoring its 4-hour
cache throttle) to seed ICAL#{group_id} cache items and materialized
EVENT# items with source='ical', so the new site has real content
immediately.

Dry-run by default; pass --apply to write. Not meant to be rerun as an
ongoing sync.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date as date_type, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, '..', 'lambda_src', 'api'))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true',
                   help='Actually write to the target table (default: dry run)')
    p.add_argument('--site-dir', default=os.path.expanduser('~/projects/dctech.events'),
                   help='Production dctech.events checkout (YAML source of truth)')
    p.add_argument('--source-table', default='dctech-events')
    p.add_argument('--legacy-table', default='DcTechEvents')
    p.add_argument('--target-table', default='dctech-events-next')
    p.add_argument('--skip-ical-fetch', action='store_true',
                   help='Skip the live iCal fetch/seed step')
    return p.parse_args()


def load_yaml_sources(site_dir):
    os.environ['CALGEN_SITE_DIR'] = site_dir
    os.chdir(site_dir)
    from calgen import pipeline
    groups = pipeline.get_groups()
    categories = pipeline.get_categories()
    single_events = pipeline.load_single_events()
    recurring_events = pipeline.load_recurring_events()
    overlays = pipeline.load_overlays()
    return groups, categories, single_events, recurring_events, overlays


def scan_source_table(table_name):
    import boto3
    table = boto3.resource('dynamodb').Table(table_name)
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return items


def audit_legacy_table(table_name):
    import boto3
    try:
        client = boto3.client('dynamodb')
        desc = client.describe_table(TableName=table_name)
        return desc['Table'].get('ItemCount', 0)
    except Exception as e:
        return f'unavailable ({e})'


def fetch_ical_caches(groups, site_dir):
    """Run calgen's fetch for each active group; return {group_id: (events, meta)}."""
    from calgen.calendars import fetch_ical_and_extract_events
    ical_cache_dir = os.path.join(site_dir, '_cache', 'ical')
    results = {}
    for group in groups:
        if not group.get('active', True) or not group.get('ical'):
            continue
        gid = group['id']
        events = fetch_ical_and_extract_events(group['ical'], gid, group)
        meta = {}
        meta_file = os.path.join(ical_cache_dir, f'{gid}.meta')
        if os.path.exists(meta_file):
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
            except Exception:
                pass
        if events is None:
            # Fetch failed with no cache — nothing to seed
            cache_file = os.path.join(ical_cache_dir, f'{gid}.json')
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    events = json.load(f)
            else:
                continue
        results[gid] = (events, meta)
    return results


def ical_events_to_items(groups_by_id, ical_caches):
    """Materialize EVENT#-shaped dicts from fetched ical events (aggregator logic)."""
    from calgen.event_utils import calculate_event_hash
    today = date_type.today().isoformat()
    out = {}
    for gid, (events, _meta) in ical_caches.items():
        group = groups_by_id.get(gid, {})
        suppress = group.get('suppress_urls') or []
        if isinstance(suppress, bool):
            suppress = []
        for ev in events:
            if not ev.get('date') or ev['date'] < today:
                continue
            if ev.get('url', '') in suppress:
                continue
            guid = calculate_event_hash(ev.get('date', ''), ev.get('time', ''),
                                        ev.get('title', ''), ev.get('url'))
            data = dict(ev)
            data['group'] = group.get('name', gid)
            data['group_id'] = gid
            data['group_website'] = group.get('website', '')
            data['categories'] = list(group.get('categories', []))
            out[guid] = data
    return out


def main():
    args = parse_args()
    site_dir = os.path.abspath(os.path.expanduser(args.site_dir))
    mode = 'APPLY' if args.apply else 'DRY RUN'
    print(f'=== Migration to {args.target_table} — {mode} ===\n')

    os.environ['DYNAMODB_TABLE_NAME'] = args.target_table
    import db  # cdk_next/lambda_src/api/db.py

    # 1. YAML (authoritative)
    groups, categories, single_events, recurring_events, overlays = \
        load_yaml_sources(site_dir)
    groups_by_id = {g['id']: g for g in groups}
    print(f'YAML: {len(groups)} groups, {len(categories)} categories, '
          f'{len(single_events)} single events, {len(recurring_events)} recurring, '
          f'{len(overlays)} overlays')

    # 2. Current dctech-events table
    source_items = scan_source_table(args.source_table)
    drafts = [i for i in source_items if str(i.get('PK', '')).startswith('DRAFT#')]
    src_events = [i for i in source_items if str(i.get('PK', '')).startswith('EVENT#')]
    other = len(source_items) - len(drafts) - len(src_events)
    print(f'{args.source_table}: {len(source_items)} items '
          f'({len(drafts)} drafts, {len(src_events)} events, {other} other)')

    # 3. Legacy table — audit only
    print(f'{args.legacy_table} (audit only, not imported): '
          f'~{audit_legacy_table(args.legacy_table)} items\n')

    # 4. Live iCal fetch
    ical_caches = {}
    ical_event_items = {}
    if not args.skip_ical_fetch:
        print('Fetching iCal feeds (honors calgen 4-hour cache throttle)...')
        ical_caches = fetch_ical_caches(groups, site_dir)
        ical_event_items = ical_events_to_items(groups_by_id, ical_caches)
        print(f'iCal: {len(ical_caches)} group caches, '
              f'{len(ical_event_items)} future events materialized\n')

    # Reconciliation: YAML wins on guid collision
    yaml_guids = {e['guid'] for e in single_events}
    src_event_conflicts = [
        i for i in src_events if i['PK'].split('#', 1)[1] in yaml_guids]
    src_events_to_copy = [
        i for i in src_events if i['PK'].split('#', 1)[1] not in yaml_guids
        and i['PK'].split('#', 1)[1] not in ical_event_items]
    print(f'Reconciliation: {len(src_event_conflicts)} source-table events '
          f'superseded by YAML (YAML wins), {len(src_events_to_copy)} copied verbatim')

    # Overlay targets
    all_new_guids = yaml_guids | set(ical_event_items) | {
        i['PK'].split('#', 1)[1] for i in src_events_to_copy}
    overlays_applied = {g: o for g, o in overlays.items() if g in all_new_guids}
    overlays_orphaned = sorted(set(overlays) - set(overlays_applied))
    print(f'Overlays: {len(overlays_applied)} applied, '
          f'{len(overlays_orphaned)} orphaned (target guid not present)')
    if overlays_orphaned:
        for g in overlays_orphaned:
            print(f'  orphaned overlay: {g}')
    print()

    if not args.apply:
        print('Dry run complete — re-run with --apply to write.')
        return

    # ── Writes ────────────────────────────────────────────────────
    import boto3
    target = boto3.resource('dynamodb').Table(args.target_table)

    print('Writing groups...')
    for g in groups:
        data = {k: v for k, v in g.items() if k != 'id'}
        db.put_group(g['id'], data)

    print('Writing categories...')
    for slug, cat in categories.items():
        db.put_category(slug, {k: v for k, v in cat.items() if k != 'slug'})

    print('Writing single events...')
    for e in single_events:
        data = {k: v for k, v in e.items() if k not in ('id', 'guid', 'source')}
        data['slug'] = e['id']
        if e['guid'] in overlays_applied:
            data['overrides'] = overlays_applied[e['guid']]
        db.put_event(e['guid'], data, source='manual', review_status='approved')

    print('Writing recurring events...')
    for e in recurring_events:
        data = {k: v for k, v in e.items() if k not in ('id',)}
        db.put_recurring_event(e['id'], data)

    print('Writing iCal caches + materialized ical events...')
    for gid, (events, meta) in ical_caches.items():
        db.put_ical_cache(gid, events, meta)
    for guid, data in ical_event_items.items():
        if guid in overlays_applied:
            data = dict(data)
            data['overrides'] = overlays_applied[guid]
        db.put_event(guid, data, source='ical', review_status='approved')

    print('Copying drafts verbatim...')
    with target.batch_writer() as batch:
        for item in drafts:
            batch.put_item(Item=item)

    print('Copying non-conflicting source-table events (with GSI5 wiring added)...')
    with target.batch_writer() as batch:
        for item in src_events:
            guid = item['PK'].split('#', 1)[1]
            if guid in yaml_guids or guid in ical_event_items:
                continue
            item = dict(item)
            rs = item.get('review_status', 'approved')
            date_val = str(item.get('date', '') or '')
            time_val = str(item.get('time', '') or '00:00')
            item['review_status'] = rs
            item['GSI5PK'] = f'REVIEW#{rs}'
            item['GSI5SK'] = f'{date_val}#{time_val}'
            if guid in overlays_applied:
                item['overrides'] = overlays_applied[guid]
            batch.put_item(Item=item)

    print('\nMigration applied. Summary:')
    print(f'  groups={len(groups)} categories={len(categories)} '
          f'single={len(single_events)} recurring={len(recurring_events)}')
    print(f'  ical_caches={len(ical_caches)} ical_events={len(ical_event_items)}')
    print(f'  drafts={len(drafts)} copied_events={len(src_events_to_copy)}')
    print(f'  overlays applied={len(overlays_applied)} orphaned={len(overlays_orphaned)}')


if __name__ == '__main__':
    main()
