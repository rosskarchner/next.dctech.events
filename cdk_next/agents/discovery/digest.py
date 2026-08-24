"""Emails the admin a digest of discovery proposals."""
import html
import os

FROM_EMAIL = os.environ.get('FROM_EMAIL', 'newsletter@dctech.events')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')


def _rows(results):
    groups = results.get('groups') or []
    events = results.get('events') or []
    skipped = results.get('skipped') or []
    failed = results.get('sources_failed') or []
    proposed = results.get('proposed') or []
    return groups, events, skipped, failed, proposed


def render(run_id, results):
    groups, events, skipped, failed, proposed = _rows(results)
    dry = bool(results.get('dry_run'))
    proposed_count = len(proposed) if proposed else (len(groups) + len(events))
    mode = 'DRY RUN ' if dry else ''
    subject = f'[dctech.events] {mode}Discovery: {proposed_count} proposals'

    text = [f'Run ID: {run_id}', f'Dry run: {dry}', '']
    if groups:
        text.append(f'Groups proposed ({len(groups)})')
        text.extend(f"  - {g.get('name', '?')} [{g.get('draft_id', '?')}]" for g in groups)
        text.append('')
    if events:
        text.append(f'Events proposed ({len(events)})')
        text.extend(
            f"  - {e.get('title', '?')} ({e.get('date', '?')}) [{e.get('draft_id', '?')}]"
            for e in events
        )
        text.append('')
    if skipped:
        text.append(f'Skipped ({len(skipped)})')
        text.extend(f"  - {s.get('name', '?')}: {s.get('reason', '')}" for s in skipped)
        text.append('')
    if failed:
        text.append(f'Source failures ({len(failed)})')
        text.extend(f"  - {f.get('url', '?')}: {f.get('reason', '')}" for f in failed)
        text.append('')
    if not (groups or events or skipped or failed):
        text.append('No discoveries reported.')

    html_parts = [
        f'<p>Run ID: <code>{html.escape(run_id)}</code><br>'
        f'Dry run: <strong>{str(dry)}</strong></p>'
    ]
    if groups:
        html_parts.append(f'<h3>Groups proposed ({len(groups)})</h3><ul>')
        html_parts.extend(
            f"<li>{html.escape(str(g.get('name', '?')))} "
            f"[{html.escape(str(g.get('draft_id', '?')))}]</li>"
            for g in groups
        )
        html_parts.append('</ul>')
    if events:
        html_parts.append(f'<h3>Events proposed ({len(events)})</h3><ul>')
        html_parts.extend(
            f"<li>{html.escape(str(e.get('title', '?')))} "
            f"({html.escape(str(e.get('date', '?')) )}) "
            f"[{html.escape(str(e.get('draft_id', '?')))}]</li>"
            for e in events
        )
        html_parts.append('</ul>')
    if failed:
        html_parts.append(f'<h3>Source failures ({len(failed)})</h3><ul>')
        html_parts.extend(
            f"<li>{html.escape(str(f.get('url', '?')))}: "
            f"{html.escape(str(f.get('reason', '')))}</li>"
            for f in failed
        )
        html_parts.append('</ul>')
    if not (groups or events or skipped or failed):
        html_parts.append('<p>No discoveries reported.</p>')

    return subject, '\n'.join(text), ''.join(html_parts)


def send(run_id, results):
    """Email the digest. Never raises."""
    subject, text, body_html = render(run_id, results)
    try:
        import boto3

        boto3.client('sesv2').send_email(
            FromEmailAddress=FROM_EMAIL,
            Destination={'ToAddresses': [ADMIN_EMAIL]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200]},
                'Body': {'Html': {'Data': body_html}, 'Text': {'Data': text}},
            }},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f'digest email failed: {exc}')
        return False
