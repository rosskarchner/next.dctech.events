#!/usr/bin/env python3
"""Store what this build matched each highlight to, and notice lost events.

Runs in the site-generator CodeBuild job right after `calgen pipeline`, from
the site dir, reading `_data/highlight_matches.json` ({target_key: guid or
null}, written by calgen/highlights.py) and `_data/highlights.json` (the
export's snapshot of each active highlight).

For each highlight it keeps HIGHLIGHT#<key>/RESOLVED up to date, writing only
when something changed:

  matched     resolved_guid = the guid; clear missing_since. The next export
              hands resolved_guid back to calgen, so a highlight keeps
              following an event whose guid has since moved on.
  unmatched   (and its snapshot date has not passed) set missing_since. Once
              it has been missing for GRACE_SECONDS — long enough to ride out
              one failed iCal fetch — email Ross and, for a paid highlight,
              the buyer, once, and record lost_notified_at.

RESOLVED is its own sort key so these writes never reach the build trigger,
which listens to SK=META only. This step must never fail the build: every
error is printed and swallowed, and the exit status is always 0.

Usage: python reconcile_highlights.py --table dctech-events-next
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

GRACE_SECONDS = 24 * 3600
LOCAL_TZ = ZoneInfo('America/New_York')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
BASE_URL = os.environ.get('BASE_URL', 'https://dctech.events')


def _load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _send(ses, to, subject, text):
    try:
        ses.send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[ADMIN_EMAIL],
            Destination={'ToAddresses': [to]},
            Content={'Simple': {'Subject': {'Data': subject[:200]},
                                'Body': {'Text': {'Data': text}}}},
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never fail the build over mail
        print(f'Lost-highlight email to {to} failed: {exc}')
        return False


def _lost_emails(meta):
    """(to, subject, body) for each person told about a lost highlight."""
    title = meta.get('title', '')
    when = meta.get('date', '')
    body = (f'The highlighted event "{title}" ({when}) is no longer on the DC '
            f'Tech Events calendar — it may have been cancelled or moved.\n\n'
            f'Event link we had: {meta.get("url", "")}\n')
    mails = [(ADMIN_EMAIL, f'[dctech.events] Highlighted event is gone: {title}',
              f'{body}\nkind: {meta.get("kind")}\nkey: {meta.get("target_key")}\n'
              f'buyer: {meta.get("contact_email", "")}\n\n'
              f'{BASE_URL}/edit/highlights.html\n')]
    if meta.get('kind') == 'paid' and meta.get('contact_email'):
        mails.append((meta['contact_email'], 'About your DC Tech Events highlight',
                      f'{body}\nIf the event is back on (or moved to another '
                      f'date), reply and we will point the highlight at it. '
                      f'Otherwise we will refund your payment — just reply to '
                      f'this email.\n'))
    return mails


def plan(matches, snapshots, resolved, now, today):
    """Pure decision step: what to write for each highlight.

    Returns a list of (target_key, action, fields) where action is 'set'
    (merge `fields` into RESOLVED, removing any whose value is None) or
    'notify' (send the lost emails, then set `fields`).
    """
    out = []
    for key, guid in sorted(matches.items()):
        state = resolved.get(key) or {}
        if guid:
            if state.get('resolved_guid') != guid or state.get('missing_since'):
                out.append((key, 'set', {'resolved_guid': guid, 'last_seen_at': now,
                                         'missing_since': None}))
            continue
        snap = snapshots.get(key) or {}
        last = max(snap.get('date') or '', snap.get('end_date') or '')
        if last < today:
            continue  # the event is over (multi-day: its last day); nothing to chase
        if state.get('lost_notified_at'):
            continue
        missing_since = state.get('missing_since')
        if not missing_since:
            out.append((key, 'set', {'missing_since': now}))
        elif now - int(missing_since) >= GRACE_SECONDS:
            out.append((key, 'notify', {'lost_notified_at': now}))
    return out


def _update(table, key, fields):
    sets = {k: v for k, v in fields.items() if v is not None}
    removes = [k for k, v in fields.items() if v is None]
    names, values, parts = {}, {}, []
    if sets:
        clauses = []
        for i, (k, v) in enumerate(sets.items()):
            names[f'#s{i}'] = k
            values[f':s{i}'] = v
            clauses.append(f'#s{i} = :s{i}')
        parts.append('SET ' + ', '.join(clauses))
    if removes:
        clauses = []
        for i, k in enumerate(removes):
            names[f'#r{i}'] = k
            clauses.append(f'#r{i}')
        parts.append('REMOVE ' + ', '.join(clauses))
    kwargs = {'Key': {'PK': f'HIGHLIGHT#{key}', 'SK': 'RESOLVED'},
              'UpdateExpression': ' '.join(parts),
              'ExpressionAttributeNames': names}
    if values:
        kwargs['ExpressionAttributeValues'] = values
    table.update_item(**kwargs)


def run(table, ses, data_dir='_data', now=None, today=None):
    matches = _load(os.path.join(data_dir, 'highlight_matches.json'), {})
    if not matches:
        print('Highlights: nothing to reconcile')
        return []
    snapshots = {h.get('target_key'): h for h in
                 _load(os.path.join(data_dir, 'highlights.json'), [])}
    now = int(time.time()) if now is None else now
    today = today or datetime.now(LOCAL_TZ).date().isoformat()

    resolved = {}
    for key in matches:
        item = table.get_item(Key={'PK': f'HIGHLIGHT#{key}', 'SK': 'RESOLVED'}).get('Item')
        resolved[key] = item or {}

    actions = plan(matches, snapshots, resolved, now, today)
    for key, action, fields in actions:
        try:
            if action == 'notify':
                meta = table.get_item(Key={'PK': f'HIGHLIGHT#{key}', 'SK': 'META'}).get('Item') or {}
                if meta.get('status') != 'active':
                    continue  # taken down since the export; nothing to say
                for to, subject, body in _lost_emails(meta):
                    _send(ses, to, subject, body)
            _update(table, key, fields)
        except Exception as exc:  # noqa: BLE001
            print(f'Highlight {key}: {action} failed: {exc}')
    print(f'Highlights: {len(matches)} checked, {len(actions)} updated')
    return actions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--table', default=os.environ.get('TABLE_NAME', 'dctech-events-next'))
    args = parser.parse_args()
    try:
        import boto3
        run(boto3.resource('dynamodb').Table(args.table), boto3.client('sesv2'))
    except Exception as exc:  # noqa: BLE001 - never fail the build
        print(f'WARNING: highlight reconcile failed: {exc}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
