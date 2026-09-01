"""
Submission routes.

Submitting no longer requires a Cognito account: a submitter can prove control
of an email address by clicking an emailed magic link instead. Cognito claims
still work, so admins and already-signed-in users are unaffected — see
`_resolve_submitter`, which accepts either.
"""

import json
import os
import re
from urllib.parse import parse_qs, urlparse

import magic_link
from auth import get_user_from_event
from db import (
    build_event_draft_data as _build_event_draft_data,
    create_draft, get_all_categories, get_drafts_by_submitter,
    check_and_record_link_request, subscribe_to_newsletter,
    is_trusted_submitter, promote_draft_to_event, update_draft_status,
)
from routes.responses import html as _html, json as _json_response

CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')
NEWSLETTER_TOPIC = os.environ.get('NEWSLETTER_TOPIC', 'dctech')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')


def _parse_body(event):
    """Parse request body (JSON or form-encoded)."""
    body = event.get('body', '')
    if not body:
        return {}

    content_type = event.get('headers', {}).get('Content-Type', '')
    if not content_type:
        content_type = event.get('headers', {}).get('content-type', '')

    if 'application/json' in content_type:
        return json.loads(body)

    # Form-encoded
    from urllib.parse import parse_qs
    parsed = parse_qs(body)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def _json(status_code, body, event=None):
    return _json_response(status_code, body, event)


def _error_payload(message):
    return {'error': message}


def _site_from_origin(event):
    """Extract site slug from Origin header, e.g. https://dctech.events -> dctech."""
    headers = event.get('headers', {})
    origin = headers.get('origin') or headers.get('Origin') or ''
    if not origin:
        return None
    host = origin.split('//')[-1].split('/')[0]
    return host.split('.')[0] or None


def _build_group_draft_data(data):
    name = data.get('name', '').strip()
    website = data.get('website', '').strip()
    if not name or not website:
        return None, 'Group name and website are required.'

    return {
        'name': name,
        'website': website,
        'ical_url': data.get('ical_url', ''),
        'fallback_url': data.get('fallback_url', ''),
    }, None


def submit_form(event, jinja_env):
    """GET /submit — returns the submission form HTML."""
    claims, err = get_user_from_event(event)
    if err:
        return err

    from datetime import datetime, timedelta
    tomorrow = datetime.now() + timedelta(days=1)
    default_date = tomorrow.strftime('%Y-%m-%d')

    categories = get_all_categories()
    template = jinja_env.get_template('partials/submit_form.html')
    html = template.render(
        categories=categories,
        user_email=claims.get('email', ''),
        default_date=default_date,
        default_hour='6',
        default_minute='30',
        default_ampm='PM',
    )

    return _html(200, html, event)


def submit_event(event, jinja_env):
    """POST /submit — create a draft event or group submission."""
    claims, err = get_user_from_event(event)
    if err:
        return err

    data = _parse_body(event)
    submitter = claims.get('email')
    submitter_id = claims.get('sub')
    submission_type = data.get('type', 'event')

    if submission_type == 'group':
        return _submit_group(event, data, submitter, jinja_env, submitter_id)
    return _submit_event(event, data, submitter, jinja_env, submitter_id)


def _submit_event(event, data, submitter, jinja_env, submitter_id=None):
    """Handle event submission."""
    draft_data, error = _build_event_draft_data(data)
    if error:
        template = jinja_env.get_template('partials/submit_error.html')
        html = template.render(error=error)
        return _html(400, html, event)

    draft_id = create_draft('event', draft_data, submitter, submitter_id)
    _notify_admin(draft_id, 'event', draft_data, submitter, False)

    template = jinja_env.get_template('partials/submit_confirmation.html')
    html = template.render(draft_id=draft_id, draft_type='event')

    return _html(201, html, event)


def _submit_group(event, data, submitter, jinja_env, submitter_id=None):
    """Handle group submission."""
    draft_data, error = _build_group_draft_data(data)
    if error:
        template = jinja_env.get_template('partials/submit_error.html')
        html = template.render(error=error)
        return _html(400, html, event)

    draft_id = create_draft('group', draft_data, submitter, submitter_id)
    _notify_admin(draft_id, 'group', draft_data, submitter, False)

    template = jinja_env.get_template('partials/submit_confirmation.html')
    html = template.render(draft_id=draft_id, draft_type='group')

    return _html(201, html, event)


def my_submissions(event, jinja_env):
    """GET /my-submissions — list user's own submissions."""
    claims, err = get_user_from_event(event)
    if err:
        return err

    submitter = claims.get('email', '')
    user_id = claims.get('sub') or submitter
    print(f"Querying submissions for user_id: {user_id} (email: {submitter})")
    drafts = get_drafts_by_submitter(user_id)
    print(f"Found {len(drafts)} drafts for {user_id}")

    template = jinja_env.get_template('partials/my_submissions.html')
    html = template.render(submissions=drafts)

    return _html(200, html, event)


def submit_event_json(event, jinja_env):
    """POST /api/submissions — create a draft event or group submission."""
    data = _parse_body(event)
    submitter, submitter_id, err = _resolve_submitter(event, data)
    if err:
        return err

    submission_type = data.get('type', 'event')

    if submission_type == 'group':
        draft_data, error = _build_group_draft_data(data)
        if error:
            return _json(400, _error_payload(error), event)
        draft_id = create_draft('group', draft_data, submitter, submitter_id)
        subscribed = _maybe_subscribe(data, submitter)
        _notify_admin(draft_id, 'group', draft_data, submitter, False)
        return _json(201, {'draft_id': draft_id, 'draft_type': 'group',
                           'subscribed': subscribed}, event)

    draft_data, error = _build_event_draft_data(data)
    if error:
        return _json(400, _error_payload(error), event)
    site = _site_from_origin(event)
    if site:
        draft_data['site'] = site
    draft_id = create_draft('event', draft_data, submitter, submitter_id)

    # Trusted submitters skip the queue. Only events: a group submission adds
    # a whole recurring feed to the site, which deserves a human look even
    # from someone whose one-off events are trusted.
    published = False
    if is_trusted_submitter(submitter):
        published = _auto_approve_event(draft_id, draft_data, submitter)

    # After the draft is safely stored, so a newsletter hiccup cannot cost
    # the user the submission they just filled in.
    subscribed = _maybe_subscribe(data, submitter)
    _notify_admin(draft_id, 'event', draft_data, submitter, published)
    return _json(201, {'draft_id': draft_id, 'draft_type': 'event',
                       'subscribed': subscribed, 'published': published}, event)


def my_submissions_json(event, jinja_env):
    """GET /api/my-submissions — return JSON list of user's submissions."""
    claims, err = get_user_from_event(event)
    if err:
        return err

    submitter = claims.get('email', '')
    user_id = claims.get('sub') or submitter
    drafts = get_drafts_by_submitter(user_id)
    return _json(200, {'submissions': drafts}, event)


# ─── Magic-link submission ────────────────────────────────────────

def _resolve_submitter(event, data):
    """Identify the submitter from a magic link or Cognito claims.

    Returns (submitter_email, submitter_id, error_response). The magic link is
    checked first so a signed-out submitter never trips the Cognito path; the
    id falls back to the email because drafts are keyed by submitter id and a
    link-authenticated user has no Cognito `sub`.
    """
    email, timestamp, signature = magic_link.token_from_request(data)
    if email or timestamp or signature:
        ok, reason = magic_link.verify_token(email, timestamp, signature)
        if not ok:
            return None, None, _json(401, _error_payload(reason), event)
        return email, f'magiclink:{email}', None

    claims, err = get_user_from_event(event)
    if err:
        return None, None, _json(
            401,
            _error_payload(
                'Please request a submission link, or sign in, to submit.'),
            event,
        )
    return claims.get('email'), claims.get('sub'), None


def _maybe_subscribe(data, email):
    """Honor the newsletter opt-in checkbox.

    Never raises: a newsletter problem must not lose an event submission the
    user already filled in. Worst case they subscribe again from /newsletter.
    """
    raw = data.get('newsletter_optin') or data.get('newsletter')
    opted_in = str(raw).lower() in ('1', 'true', 'on', 'yes')
    if not opted_in or not email:
        return False

    try:
        # The address is already proven — by the magic link or by Cognito —
        # so this skips the double opt-in the public signup form requires.
        subscribe_to_newsletter(email, CONTACT_LIST_NAME, NEWSLETTER_TOPIC)
        return True
    except Exception as exc:
        print(f'Newsletter opt-in failed for {email}: {exc}')
        return False


# Paths a magic link is allowed to point at — an explicit allowlist so this
# endpoint can never become an open redirect via a client-supplied path, and
# so a malformed or unrecognized value costs nothing worse than falling back
# to the default rather than erroring out the whole request.
_LINK_REDIRECT_PATHS = ('/edit/submit-event.html', '/edit/correct-event.html')
_GUID_RE = re.compile(r'^[0-9a-f]{1,64}$')


def _sanitize_redirect_path(raw):
    """Only ever returns one of _LINK_REDIRECT_PATHS, optionally with a
    validated `guid` query param carried through for the correction form.
    """
    parsed = urlparse(str(raw or ''))
    if parsed.scheme or parsed.netloc or parsed.path not in _LINK_REDIRECT_PATHS:
        return _LINK_REDIRECT_PATHS[0]
    if parsed.path == '/edit/correct-event.html':
        guid = (parse_qs(parsed.query).get('guid') or [''])[0]
        if guid and _GUID_RE.match(guid):
            return f'/edit/correct-event.html?guid={guid}'
        return '/edit/correct-event.html'
    return parsed.path


def request_link_json(event, jinja_env):
    """POST /api/submit-link — email a magic link to submit an event or,
    with a `redirect_path`, to correct one (see _sanitize_redirect_path)."""
    import boto3

    data = _parse_body(event)
    email = magic_link.normalize_email(data.get('email'))

    if not magic_link.is_valid_email(email):
        return _json(400, _error_payload('Please enter a valid email address.'),
                     event)

    allowed, retry_after = check_and_record_link_request(email)
    if not allowed:
        minutes = max(1, round(retry_after / 60))
        return _json(429, _error_payload(
            f'A link was just sent to that address. Please check your inbox, '
            f'or try again in {minutes} minute{"s" if minutes != 1 else ""}.'
        ), event)

    redirect_path = _sanitize_redirect_path(data.get('redirect_path'))
    is_correction = redirect_path.startswith('/edit/correct-event.html')

    try:
        timestamp, signature = magic_link.generate_token(email)
        link = magic_link.build_link(email, timestamp, signature, path=redirect_path)
        hours = magic_link.TOKEN_TTL_SECONDS // 3600

        if is_correction:
            subject = 'Your DC Tech Events correction link'
            action_verb = 'suggest a correction to an event'
            action_label = 'Suggest a correction'
            not_requested = 'nothing was changed'
        else:
            subject = 'Your DC Tech Events submission link'
            action_verb = 'submit your event to'
            action_label = 'Submit an event'
            not_requested = 'nothing was submitted'

        ses = boto3.client('sesv2')
        ses.send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[REPLY_TO_EMAIL],
            Destination={'ToAddresses': [email]},
            Content={'Simple': {
                'Subject': {'Data': subject},
                'Body': {
                    'Html': {'Data': (
                        f'<p>Use this link to {action_verb} '
                        'DC Tech Events:</p>'
                        f'<p><a href="{link}">{action_label}</a></p>'
                        f'<p>The link works for the next {hours} hours. '
                        'If you did not request it, you can ignore this '
                        f'email — {not_requested}.</p>'
                    )},
                    'Text': {'Data': (
                        f'Use this link to {action_verb} '
                        f'DC Tech Events:\n\n{link}\n\n'
                        f'The link works for the next {hours} hours. If you '
                        'did not request it, you can ignore this email — '
                        f'{not_requested}.\n'
                    )},
                },
            }},
        )
    except Exception as exc:
        print(f'Failed to send magic link to {email}: {exc}')
        return _json(500, _error_payload(
            'We could not send that email. Please try again shortly.'), event)

    return _json(200, {
        'message': ('Check your email — we sent you a link to submit your '
                    'event. It may take a minute to arrive.'),
    }, event)


# ─── Trusted-submitter auto-approval ──────────────────────────────

# Recorded as the reviewer so the queue history distinguishes an automatic
# publish from one a human actually looked at.
AUTO_REVIEWER = 'auto:trusted-submitter'


def _auto_approve_event(draft_id, draft_data, submitter):
    """Publish a trusted submitter's event immediately.

    The DRAFT record is still written first and then marked APPROVED rather
    than skipped: it keeps the audit trail and /my-submissions complete, and
    leaves a normal-looking history entry showing who published and when.

    Returns True if the event was published. A failure here is deliberately
    not fatal — the draft simply stays pending and an admin approves it by
    hand, which is the pre-existing behaviour and strictly safer than losing
    the submission.
    """
    try:
        merged = dict(draft_data)
        merged['id'] = draft_id
        merged.setdefault('submitter_email', submitter)
        promote_draft_to_event(merged)
        update_draft_status(draft_id, 'APPROVED', AUTO_REVIEWER)
        return True
    except Exception as exc:
        print(f'Auto-approval failed for draft {draft_id} ({submitter}): {exc}')
        return False


# ─── Admin notification ───────────────────────────────────────────

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'ross@karchner.com')
QUEUE_URL = os.environ.get('QUEUE_URL', 'https://dctech.events/edit/queue.html')


def _notify_admin(draft_id, draft_type, draft_data, submitter, published):
    """Email the admin about a new submission.

    Complements the daily queue digest in NextOpsStack with an immediate
    heads-up. Auto-published submissions are included too — those never reach
    the queue, so this is the only notice they generate, and it is the one
    worth seeing if a trusted submitter posts something wrong.

    Never raises: a mail failure must not fail the submission.
    """
    try:
        import boto3

        label = 'Group' if draft_type == 'group' else 'Event'
        title = (draft_data.get('title') or draft_data.get('name')
                 or '(untitled)')
        when = draft_data.get('date') or ''
        if draft_data.get('time'):
            when = f"{when} {draft_data['time']}".strip()

        if published:
            state = 'Published automatically (trusted submitter)'
            subject = f'[dctech.events] {label} auto-published: {title}'
        else:
            state = 'Waiting for review'
            subject = f'[dctech.events] New {label.lower()} submission: {title}'

        rows = [
            ('Title', title),
            ('When', when or '—'),
            ('Location', draft_data.get('location') or '—'),
            ('URL', draft_data.get('url') or draft_data.get('website') or '—'),
            ('Submitter', submitter or 'unknown'),
            ('Status', state),
            ('Draft ID', draft_id),
        ]
        html_rows = ''.join(
            f'<tr><td style="padding:2px 12px 2px 0;color:#666">{k}</td>'
            f'<td style="padding:2px 0">{v}</td></tr>'
            for k, v in rows
        )
        text_rows = '\n'.join(f'{k}: {v}' for k, v in rows)

        boto3.client('sesv2').send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[submitter] if submitter else [REPLY_TO_EMAIL],
            Destination={'ToAddresses': [ADMIN_EMAIL]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200]},
                'Body': {
                    'Html': {'Data': (
                        f'<p><strong>{state}</strong></p>'
                        f'<table>{html_rows}</table>'
                        f'<p><a href="{QUEUE_URL}">Open the moderation queue</a></p>'
                    )},
                    'Text': {'Data': f'{state}\n\n{text_rows}\n\n{QUEUE_URL}\n'},
                },
            }},
        )
    except Exception as exc:
        print(f'Admin notification failed for draft {draft_id}: {exc}')
