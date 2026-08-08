"""
Submission routes.

Submitting no longer requires a Cognito account: a submitter can prove control
of an email address by clicking an emailed magic link instead. Cognito claims
still work, so admins and already-signed-in users are unaffected — see
`_resolve_submitter`, which accepts either.
"""

import json
import os

import magic_link
from auth import get_user_from_event
from db import (
    create_draft, get_all_categories, get_drafts_by_submitter,
    check_and_record_link_request, subscribe_to_newsletter,
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


def _normalize_categories(data):
    categories = data.get('categories', [])
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(',') if c.strip()]
    return categories


def _build_event_draft_data(data):
    title = (data.get('title') or data.get('name') or '').strip()
    date_val = data.get('date', '').strip()
    time_str = data.get('time', '').strip() or None
    timing = data.get('timing', 'specific')

    start_dt = data.get('start_datetime', '').strip()
    if start_dt and not date_val:
        if 'T' in start_dt:
            date_val, time_str = start_dt.split('T')
        else:
            date_val = start_dt

    if not title or not date_val:
        return None, 'Event title and date are required.'

    if timing == 'specific' and not time_str:
        hour = data.get('time_hour', '')
        minute = data.get('time_minute', '00')
        ampm = data.get('time_ampm', 'PM')
        if hour:
            h = int(hour)
            if ampm == 'PM' and h != 12:
                h += 12
            elif ampm == 'AM' and h == 12:
                h = 0
            time_str = f'{h:02d}:{minute}'

    draft_data = {
        'title': title,
        'date': date_val,
        'time': time_str,
        'url': data.get('url', ''),
        'city': data.get('city', ''),
        'state': data.get('state', ''),
        'cost': data.get('cost', ''),
        'end_date': data.get('end_date', ''),
        'all_day': timing == 'allday',
        'description': data.get('description', ''),
        'location': data.get('location', ''),
        'categories': _normalize_categories(data),
    }
    return draft_data, None


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
        return _json(201, {'draft_id': draft_id, 'draft_type': 'group',
                           'subscribed': subscribed}, event)

    draft_data, error = _build_event_draft_data(data)
    if error:
        return _json(400, _error_payload(error), event)
    site = _site_from_origin(event)
    if site:
        draft_data['site'] = site
    draft_id = create_draft('event', draft_data, submitter, submitter_id)
    # After the draft is safely stored, so a newsletter hiccup cannot cost
    # the user the submission they just filled in.
    subscribed = _maybe_subscribe(data, submitter)
    return _json(201, {'draft_id': draft_id, 'draft_type': 'event',
                       'subscribed': subscribed}, event)


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


def request_link_json(event, jinja_env):
    """POST /api/submit-link — email a magic link to submit an event."""
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

    try:
        timestamp, signature = magic_link.generate_token(email)
        link = magic_link.build_link(email, timestamp, signature)
        hours = magic_link.TOKEN_TTL_SECONDS // 3600

        ses = boto3.client('sesv2')
        ses.send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[REPLY_TO_EMAIL],
            Destination={'ToAddresses': [email]},
            Content={'Simple': {
                'Subject': {'Data': 'Your DC Tech Events submission link'},
                'Body': {
                    'Html': {'Data': (
                        '<p>Use this link to submit your event to '
                        'DC Tech Events:</p>'
                        f'<p><a href="{link}">Submit an event</a></p>'
                        f'<p>The link works for the next {hours} hours. '
                        'If you did not request it, you can ignore this '
                        'email — nothing was submitted.</p>'
                    )},
                    'Text': {'Data': (
                        'Use this link to submit your event to '
                        f'DC Tech Events:\n\n{link}\n\n'
                        f'The link works for the next {hours} hours. If you '
                        'did not request it, you can ignore this email — '
                        'nothing was submitted.\n'
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
