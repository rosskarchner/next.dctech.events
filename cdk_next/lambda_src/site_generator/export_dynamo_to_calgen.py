#!/usr/bin/env python3
"""Export the dctech-events-next DynamoDB table into calgen's on-disk layout.

Writes (into the current working directory, which must be the calgen site
dir): _groups/, _categories/, _single_events/, _recurring_events/, _overlay/,
and reconstructs _cache/ical/{group_id}.json + .meta from ICAL#{group_id}
items — so `calgen pipeline` + `calgen build` run unmodified with DynamoDB as
the sole data source. `calgen refresh` is deliberately skipped downstream:
the iCal Aggregator Lambda owns fetching.

Usage: python export_dynamo_to_calgen.py [--table dctech-events-next]
"""
import argparse
import json
import os
import shutil
import sys
from decimal import Decimal

import boto3
import yaml
from boto3.dynamodb.conditions import Attr, Key


def _to_plain(val):
    if isinstance(val, Decimal):
        return int(val) if val == int(val) else float(val)
    if isinstance(val, list):
        return [_to_plain(v) for v in val]
    if isinstance(val, dict):
        return {k: _to_plain(v) for k, v in val.items()}
    return val


def _title_key(date_str, title):
    """Fallback identity for events whose guid differs between table and site.

    Must stay byte-identical to updates_publisher's _title_key and calgen's
    reader, or the two halves of the join silently stop matching.
    """
    return f"{date_str}|{' '.join(str(title or '').split()).casefold()}"


def _clean(item, drop=()):
    """Strip DynamoDB key/index attributes and internal fields."""
    out = {}
    for k, v in item.items():
        if k in ('PK', 'SK') or k.startswith('GSI'):
            continue
        if k in drop:
            continue
        out[k] = _to_plain(v)
    return out


def _scan_all(table, **kwargs):
    resp = table.scan(**kwargs)
    items = resp.get('Items', [])
    while 'LastEvaluatedKey' in resp:
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
    return items


def _write_yaml(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)


def _reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--table',
                        default=os.environ.get('DYNAMODB_TABLE_NAME',
                                               'dctech-events-next'))
    args = parser.parse_args()

    table = boto3.resource('dynamodb').Table(args.table)
    # Everything real lives at SK='META', except a recurring series' own
    # per-occurrence corrections (RECURRING#{slug} / SK=OVERRIDE#{date}) —
    # without the second clause those rows are invisible to this scan
    # entirely, not just miscategorized once collected.
    items = _scan_all(table, FilterExpression=(
        Attr('SK').eq('META') | Attr('SK').begins_with('OVERRIDE#')))

    by_prefix = {}
    for item in items:
        prefix = str(item['PK']).split('#', 1)[0]
        by_prefix.setdefault(prefix, []).append(item)

    counts = {}

    # ── _groups/ ────────────────────────────────────────────────────
    _reset_dir('_groups')
    for item in by_prefix.get('GROUP', []):
        slug = item['PK'].split('#', 1)[1]
        data = _clean(item, drop=('id', 'status_reason'))
        _write_yaml(os.path.join('_groups', f'{slug}.yaml'), data)
    counts['groups'] = len(by_prefix.get('GROUP', []))

    # ── _categories/ ────────────────────────────────────────────────
    _reset_dir('_categories')
    for item in by_prefix.get('CATEGORY', []):
        slug = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_categories', f'{slug}.yaml'), _clean(item))
    counts['categories'] = len(by_prefix.get('CATEGORY', []))

    # ── _single_events/ + _overlay/ ─────────────────────────────────
    # Manual + submitted events become _single_events YAML; ical-sourced
    # events are represented via the _cache export below (writing them here
    # too would duplicate them in the pipeline). Overrides on any event
    # become _overlay/{guid}.yaml.
    _reset_dir('_single_events')
    _reset_dir('_overlay')
    n_single = n_overlay = 0
    internal = ('slug', 'source', 'status', 'review_status', 'createdAt',
                'overrides', 'submitted_by', 'group_id', 'end_time',
                'description', 'site')
    for item in by_prefix.get('EVENT', []):
        guid = item['PK'].split('#', 1)[1]
        source = item.get('source')

        # Underscore-prefixed overlay keys are private bookkeeping (_comment,
        # _qa_run) — calgen merges overlay keys straight onto the event, so
        # anything left here would show up as a junk event attribute.
        overrides = {k: v for k, v in _to_plain(item.get('overrides') or {}).items()
                     if not k.startswith('_')}
        if overrides:
            _write_yaml(os.path.join('_overlay', f'{guid}.yaml'), overrides)
            n_overlay += 1

        if source not in ('manual', 'submitted', None):
            continue
        # Hidden events are written out and dropped at render instead of being
        # withheld here. The skip used to be load-bearing for a different
        # reason than it looked: calgen deduped over hidden events too, so a
        # hidden event could absorb its visible twin and lose both listings.
        # pipeline.process_events now dedups over visible events only, and
        # app.get_events does the filtering — one place decides visibility.
        data = _clean(item, drop=internal)
        # keep description for manual events — calgen renders it
        if item.get('description'):
            data['description'] = _to_plain(item['description'])
        # DynamoDB owns event identity. Without this, load_single_events
        # recomputes a 32-char content hash over an 8-hex EVENT# key and an
        # overlay on a submitted event can never match (next_dctech_events-p8o).
        data['guid'] = guid
        slug = str(item.get('slug') or guid)
        _write_yaml(os.path.join('_single_events', f'{slug}.yaml'), data)
        n_single += 1
    counts['single_events'] = n_single
    counts['overlays'] = n_overlay

    # ── _recurring_events/ ──────────────────────────────────────────
    # by_prefix['RECURRING'] now also holds each series' OVERRIDE# rows
    # (see the scan filter above) — skip anything that isn't the series'
    # own definition.
    _reset_dir('_recurring_events')
    n_recurring = 0
    for item in by_prefix.get('RECURRING', []):
        if item.get('SK') != 'META':
            continue
        slug = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_recurring_events', f'{slug}.yaml'),
                    _clean(item))
        n_recurring += 1
    counts['recurring'] = n_recurring

    # ── _recurring_overlay/ ─────────────────────────────────────────
    # Approved per-occurrence corrections. One file per series (not per
    # date) — a series can accumulate many dated overrides, and
    # _overlay/{guid}.yaml's one-file-per-target convention doesn't fit an
    # entity with no single guid.
    _reset_dir('_recurring_overlay')
    overrides_by_slug = {}
    for item in by_prefix.get('RECURRING', []):
        sk = item.get('SK', '')
        if not sk.startswith('OVERRIDE#'):
            continue
        slug = item['PK'].split('#', 1)[1]
        override_date = sk.split('#', 1)[1]
        fields = {k: v for k, v in _clean(item).items() if not k.startswith('_')}
        if fields:
            overrides_by_slug.setdefault(slug, {})[override_date] = fields
    for slug, by_date in overrides_by_slug.items():
        _write_yaml(os.path.join('_recurring_overlay', f'{slug}.yaml'), by_date)
    counts['recurring_overrides'] = sum(len(v) for v in overrides_by_slug.values())

    # ── _updates/ ───────────────────────────────────────────────────
    # Published weekly posts. These are frozen snapshots written by the
    # updates publisher — exported verbatim, since the events they list are
    # in the past by the time anyone reads the archive and can no longer be
    # reconstructed from the pipeline.
    _reset_dir('_updates')
    for item in by_prefix.get('UPDATE', []):
        week_id = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_updates', f'{week_id}.yaml'), _clean(item))
    counts['updates'] = len(by_prefix.get('UPDATE', []))

    # ── _data/added_at.json ─────────────────────────────────────────
    # When each event was first recorded. `createdAt` lives only on the
    # EVENT# rows and is dropped from the per-event YAML above (it is
    # bookkeeping, not content), but /just-added/ is entirely about it, so it
    # is exported once as a lookup instead of being smeared across every file.
    #
    # Two keys into the same answer, mirroring updates_publisher's
    # _added_at_index: iCal events share one guid with the site's events.json,
    # but submitted events are keyed EVENT#<8 hex> while calgen recomputes a
    # 32-char content hash for them (next_dctech_events-p8o), so those only
    # match on (date, title). Where both hit, the guid wins.
    by_guid, by_title = {}, {}
    for item in by_prefix.get('EVENT', []):
        created = item.get('createdAt')
        if not created:
            continue
        created = str(created)
        by_guid[item['PK'].split('#', 1)[1]] = created
        title, event_date = item.get('title'), item.get('date')
        if title and event_date:
            key = _title_key(str(event_date), str(title))
            # Earliest wins: a duplicate row for the same listing must not
            # make an old event look newly added.
            if created < by_title.get(key, created + 'z'):
                by_title[key] = created
    os.makedirs('_data', exist_ok=True)
    with open(os.path.join('_data', 'added_at.json'), 'w', encoding='utf-8') as f:
        json.dump({'by_guid': by_guid, 'by_title': by_title}, f, indent=1)
    counts['added_at'] = len(by_guid)

    # ── _archive/ ───────────────────────────────────────────────────
    # One frozen listing per ISO week, captured by the updates publisher the
    # Wednesday before that week starts. This is the only record of what the
    # calendar showed for a week: the pipeline drops events dated before
    # today, and organizers' feeds drop them too, so once a week is over its
    # events cannot be reconstructed from anything else.
    _reset_dir('_archive')
    for item in by_prefix.get('ARCHIVE', []):
        week_id = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_archive', f'{week_id}.yaml'), _clean(item))
    counts['archive'] = len(by_prefix.get('ARCHIVE', []))

    # ── _posts/ ─────────────────────────────────────────────────────
    # Free-form posts authored in /edit. Drafts are exported too — calgen is
    # the single place that decides what renders, and it skips anything not
    # marked published.
    _reset_dir('_posts')
    for item in by_prefix.get('POST', []):
        slug = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_posts', f'{slug}.yaml'), _clean(item))
    counts['posts'] = len(by_prefix.get('POST', []))

    # ── _cache/ical/ ────────────────────────────────────────────────
    _reset_dir(os.path.join('_cache', 'ical'))
    for item in by_prefix.get('ICAL', []):
        group_id = item['PK'].split('#', 1)[1]
        events = _to_plain(item.get('events', []))
        meta = _to_plain(item.get('meta', {}))
        with open(os.path.join('_cache', 'ical', f'{group_id}.json'), 'w') as f:
            json.dump(events, f)
        with open(os.path.join('_cache', 'ical', f'{group_id}.meta'), 'w') as f:
            json.dump(meta, f)
    counts['ical_caches'] = len(by_prefix.get('ICAL', []))

    print(f'Exported from {args.table}: {counts}')


if __name__ == '__main__':
    sys.exit(main())
