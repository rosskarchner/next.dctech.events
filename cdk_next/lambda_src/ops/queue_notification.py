"""Email a daily summary of the moderation queue.

Ported from the old TypeScript app's queue_notification Lambda, which ran
daily until dctech-events-api was deleted on 2026-08-02. That version
counted pending drafts in the old `dctech-events` table and emailed a bare
count; this one reads the current table and lists what is actually waiting,
so the mail is useful without opening the queue.

Sends nothing when the queue is empty — a daily "0 pending" mail trains you
to ignore it.
"""
import os

import boto3

import db

ses = boto3.client('ses')

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'noreply@dctech.events')
QUEUE_URL = os.environ.get('QUEUE_URL', 'https://dctech.events/edit/queue.html')


def _format(drafts):
    lines = []
    for d in drafts:
        kind = d.get('draft_type', 'event')
        label = d.get('title') or d.get('name') or '(untitled)'
        when = f" on {d['date']}" if d.get('date') else ''
        who = d.get('submitter_email') or 'unknown'
        lines.append(f"  - [{kind}] {label}{when}\n      from {who}, submitted {d.get('created_at', '?')}")
    return '\n'.join(lines)


def lambda_handler(event, context):
    event = event or {}
    drafts = db.get_drafts_by_status('pending')
    count = len(drafts)

    if not count:
        print('No pending submissions; no mail sent.')
        return {'sent': False, 'count': 0}

    noun = 'submission' if count == 1 else 'submissions'
    subject = f'{count} pending {noun} on DC Tech Events'
    body = (
        f'{count} {noun} awaiting review:\n\n'
        f'{_format(drafts)}\n\n'
        f'Review them at {QUEUE_URL}\n'
    )

    if event.get('dry_run'):
        print(f'[dry-run] subject: {subject}\n{body}')
        return {'sent': False, 'count': count, 'dry_run': True,
                'subject': subject, 'body': body}

    ses.send_email(
        Source=SENDER_EMAIL,
        Destination={'ToAddresses': [ADMIN_EMAIL]},
        Message={'Subject': {'Data': subject},
                 'Body': {'Text': {'Data': body}}},
    )
    print(f'Sent queue notification to {ADMIN_EMAIL} ({count} pending)')
    return {'sent': True, 'count': count}
