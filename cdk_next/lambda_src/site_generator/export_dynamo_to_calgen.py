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
    items = _scan_all(table, FilterExpression=Attr('SK').eq('META'))

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
        if item.get('hidden'):
            continue
        data = _clean(item, drop=internal)
        # keep description for manual events — calgen renders it
        if item.get('description'):
            data['description'] = _to_plain(item['description'])
        slug = str(item.get('slug') or guid)
        _write_yaml(os.path.join('_single_events', f'{slug}.yaml'), data)
        n_single += 1
    counts['single_events'] = n_single
    counts['overlays'] = n_overlay

    # ── _recurring_events/ ──────────────────────────────────────────
    _reset_dir('_recurring_events')
    for item in by_prefix.get('RECURRING', []):
        slug = item['PK'].split('#', 1)[1]
        _write_yaml(os.path.join('_recurring_events', f'{slug}.yaml'),
                    _clean(item))
    counts['recurring'] = len(by_prefix.get('RECURRING', []))

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
