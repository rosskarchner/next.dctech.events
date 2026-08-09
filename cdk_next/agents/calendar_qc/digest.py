"""Emails the admin a digest of what a QC run changed.

This is what replaces the old workflow's pull request. It cannot gate the
changes the way a PR did — they are already live by the time this sends — so
it has one job the PR body didn't: make the run reversible in practice. Every
digest carries its run id and the exact command to undo it.

Mirrors the SES pattern in api/routes/submit.py (_notify_admin): sesv2,
ADMIN_EMAIL, and never raising — a mail failure must not turn a good QC run
into a failed one.
"""
import html
import os

FROM_EMAIL = os.environ.get('FROM_EMAIL', 'newsletter@dctech.events')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')

# (results key, heading, formatter) — one section per kind of fix, matching the
# groupings the old workflow used in its PR body.
_SECTIONS = (
    ('duplicates', 'Duplicates hidden',
     lambda r: f"{r.get('title', '?')} ({r.get('guid', '?')}) "
               f"→ canonical {r.get('canonical', '?')}: {r.get('reason', '')}"),
    ('hidden', 'Hidden (out of area)',
     lambda r: f"{r.get('title', '?')} ({r.get('guid', '?')}): {r.get('reason', '')}"),
    ('other', 'Other overlay fields written',
     lambda r: f"{r.get('title', '?')} — {r.get('fields')} ({r.get('reason', '')})"),
    ('flagged', 'Flagged for you',
     lambda r: f"{r.get('title', '?')} ({r.get('guid', '?')}): {r.get('reason', '')}"),
    ('fetch_failures', 'Pages that would not load',
     lambda r: f"{r.get('title', r.get('guid', '?'))} — {r.get('url', '')}"
               f": {r.get('reason', '')}"),
)


def _rows(results):
    """(heading, [lines]) for each non-empty section."""
    sections = []
    for key, heading, fmt in _SECTIONS:
        entries = results.get(key) or []
        if entries:
            sections.append((heading, [fmt(e) for e in entries]))
    return sections


def render(run_id, results, reviewed=0):
    """Build (subject, text, html) for one run's digest."""
    sections = _rows(results)
    total = sum(len(lines) for _, lines in sections)

    if total:
        subject = f'[dctech.events] Calendar QC: {total} fixes across {reviewed} events'
    else:
        subject = f'[dctech.events] Calendar QC: {reviewed} events reviewed, no changes'

    undo = f'revert_qa_run("{run_id}")'

    text = [f'Reviewed {reviewed} events.', '']
    for heading, lines in sections:
        text.append(f'{heading} ({len(lines)})')
        text.extend(f'  - {line}' for line in lines)
        text.append('')
    if not sections:
        text.append('No quality issues found.')
        text.append('')
    text += [f'Run ID: {run_id}',
             f'To undo everything above, call the MCP tool: {undo}']

    body_html = [f'<p>Reviewed <strong>{reviewed}</strong> events.</p>']
    for heading, lines in sections:
        items = ''.join(f'<li>{html.escape(line)}</li>' for line in lines)
        body_html.append(
            f'<h3 style="margin:16px 0 4px">{html.escape(heading)} '
            f'({len(lines)})</h3><ul style="margin:0">{items}</ul>'
        )
    if not sections:
        body_html.append('<p>No quality issues found.</p>')
    body_html.append(
        f'<p style="margin-top:20px;color:#666">Run ID: <code>{html.escape(run_id)}</code>'
        f'<br>To undo everything above, call the MCP tool '
        f'<code>{html.escape(undo)}</code></p>'
    )

    return subject, '\n'.join(text), ''.join(body_html)


def send(run_id, results, reviewed=0):
    """Email the digest. Never raises."""
    subject, text, body_html = render(run_id, results, reviewed)
    try:
        import boto3

        boto3.client('sesv2').send_email(
            FromEmailAddress=FROM_EMAIL,
            Destination={'ToAddresses': [ADMIN_EMAIL]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200]},
                'Body': {
                    'Html': {'Data': body_html},
                    'Text': {'Data': text},
                },
            }},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a mail failure is not a run failure
        print(f'digest email failed: {exc}')
        return False
