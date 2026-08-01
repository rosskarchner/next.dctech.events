"""
Admin routes (requires authentication + admins group).

Forked from dctech.events/backend/routes/admin.py with the github_commit
hop removed: approval promotes DRAFT# → EVENT# (or GROUP#) directly in
DynamoDB — the Static Site Generator reads DynamoDB, so there is no
git-committed YAML to update.
"""

import json
import os
import re

import boto3

from auth import get_user_from_event, require_admin
from db import (
    get_drafts_by_status, get_draft as db_get_draft, update_draft_status,
    promote_draft_to_event, put_group,
    get_all_categories,
)

CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')
NEWSLETTER_TOPIC = os.environ.get('NEWSLETTER_TOPIC', 'dctech-next')
CODEBUILD_PROJECT_NAME = os.environ.get('CODEBUILD_PROJECT_NAME', '')


def _slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')


def _admin_check(event):
    """Validate auth + admin group. Returns (claims, error_response)."""
    claims, err = get_user_from_event(event)
    if err:
        return None, err

    admin_err = require_admin(claims)
    if admin_err:
        return None, admin_err

    return claims, None


def _html(status_code, body, event=None):
    """Return HTML response."""
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'text/html'},
        'body': body,
    }


def _json(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body),
    }


def _parse_body(event):
    """Parse application/x-www-form-urlencoded body."""
    from urllib.parse import parse_qs
    body = event.get('body', '')
    parsed = parse_qs(body)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def _promote_approved_draft(draft_id, draft_type, merged):
    """Promote an approved draft directly in DynamoDB (no git-commit hop)."""
    if draft_type == 'group':
        slug = _slugify(merged.get('name', draft_id))
        group_data = {
            'name': merged.get('name', ''),
            'website': merged.get('website', ''),
            'active': True,
        }
        if merged.get('ical_url') or merged.get('ical'):
            group_data['ical'] = merged.get('ical') or merged.get('ical_url')
        if merged.get('fallback_url'):
            group_data['fallback_url'] = merged['fallback_url']
        if merged.get('categories'):
            group_data['categories'] = merged['categories']
        put_group(slug, group_data)
        return slug
    merged = dict(merged)
    merged.setdefault('id', draft_id)
    return promote_draft_to_event(merged)


def dashboard(event, jinja_env):
    """GET /admin/dashboard — Main admin view."""
    claims, err = _admin_check(event)
    if err:
        return err

    template = jinja_env.get_template('admin/dashboard.html')
    html = template.render(claims=claims)
    return _html(200, html, event)


# ─── Draft Queue ──────────────────────────────────────────────────

def get_queue(event, jinja_env):
    """GET /admin/queue — Draft review queue."""
    claims, err = _admin_check(event)
    if err:
        return err

    drafts = get_drafts_by_status('pending')
    template = jinja_env.get_template('partials/draft_queue.html')
    html = template.render(drafts=drafts)
    return _html(200, html, event)


def get_draft(event, jinja_env, draft_id):
    """GET /admin/draft/{id} — Draft detail."""
    claims, err = _admin_check(event)
    if err:
        return err

    draft = db_get_draft(draft_id)
    if not draft:
        return {'statusCode': 404, 'body': 'Draft not found'}

    template = jinja_env.get_template('partials/draft_detail.html')
    html = template.render(draft=draft)
    return _html(200, html, event)


def get_approve_form(event, jinja_env, draft_id):
    """GET /admin/draft/{id}/approve-form — Approval form."""
    claims, err = _admin_check(event)
    if err:
        return err

    draft = db_get_draft(draft_id)
    if not draft:
        return {'statusCode': 404, 'body': 'Draft not found'}

    categories = get_all_categories()
    template = jinja_env.get_template('partials/draft_approve_form.html')
    html = template.render(draft=draft, categories=categories)
    return _html(200, html, event)


def get_draft_row(event, jinja_env, draft_id):
    """GET /admin/draft/{id}/row — Return just the queue row."""
    draft = db_get_draft(draft_id)
    template = jinja_env.get_template('partials/draft_row.html')
    html = template.render(draft=draft)
    return _html(200, html)


def approve_draft(event, jinja_env, draft_id):
    """POST /admin/draft/{id}/approve — approve event or group draft."""
    claims, err = _admin_check(event)
    if err:
        return err

    data = _parse_body(event)

    # Normalize categories
    cats = data.get('categories', [])
    if isinstance(cats, str):
        cats = [cats] if cats else []
    data['categories'] = cats

    draft = db_get_draft(draft_id)
    if not draft:
        return {'statusCode': 404, 'body': 'Draft not found'}

    draft_type = draft.get('draft_type', 'event')
    merged = {k: v for k, v in draft.items() if v is not None}
    merged.update({k: v for k, v in data.items() if v is not None})

    _promote_approved_draft(draft_id, draft_type, merged)
    update_draft_status(draft_id, 'APPROVED', claims.get('email', ''))

    label = 'group' if draft_type == 'group' else 'event'
    return _html(200, f"Approved and published {label}.")


def reject_draft(event, jinja_env, draft_id):
    """POST /admin/draft/{id}/reject."""
    claims, err = _admin_check(event)
    if err:
        return err

    update_draft_status(draft_id, 'REJECTED')
    return _html(200, "Rejected.")


def get_queue_json(event, jinja_env):
    """GET /api/admin/queue — JSON draft review queue."""
    claims, err = _admin_check(event)
    if err:
        return err

    drafts = get_drafts_by_status('pending')
    return _json(200, {'drafts': drafts})


def get_draft_json(event, jinja_env, draft_id):
    """GET /api/admin/drafts/{id} — JSON draft detail."""
    claims, err = _admin_check(event)
    if err:
        return err

    draft = db_get_draft(draft_id)
    if not draft:
        return _json(404, {'error': 'Draft not found'})

    return _json(200, {'draft': draft})


def approve_draft_json(event, jinja_env, draft_id):
    """POST /api/admin/drafts/{id}/approve — approve a draft and return JSON."""
    claims, err = _admin_check(event)
    if err:
        return err

    data = _parse_body(event)
    cats = data.get('categories', [])
    if isinstance(cats, str):
        cats = [cats] if cats else []
    data['categories'] = cats

    draft = db_get_draft(draft_id)
    if not draft:
        return _json(404, {'error': 'Draft not found'})

    draft_type = draft.get('draft_type', 'event')
    merged = {k: v for k, v in draft.items() if v is not None}
    merged.update({k: v for k, v in data.items() if v is not None})

    promoted_id = _promote_approved_draft(draft_id, draft_type, merged)
    update_draft_status(draft_id, 'APPROVED', claims.get('email', ''))
    label = 'group' if draft_type == 'group' else 'event'
    return _json(200, {'message': f'Approved and published {label}.', 'id': promoted_id})


def reject_draft_json(event, jinja_env, draft_id):
    """POST /api/admin/drafts/{id}/reject — reject a draft and return JSON."""
    claims, err = _admin_check(event)
    if err:
        return err

    draft = db_get_draft(draft_id)
    if not draft:
        return _json(404, {'error': 'Draft not found'})

    update_draft_status(draft_id, 'REJECTED')
    return _json(200, {'message': 'Rejected.'})


# ─── Site rebuild ─────────────────────────────────────────────────

def trigger_rebuild_json(event, jinja_env):
    """POST /api/admin/rebuild — start a CodeBuild site build on demand."""
    claims, err = _admin_check(event)
    if err:
        return err

    if not CODEBUILD_PROJECT_NAME:
        return _json(503, {'error': 'Site generator not configured yet'})

    codebuild = boto3.client('codebuild')
    build = codebuild.start_build(projectName=CODEBUILD_PROJECT_NAME)
    return _json(202, {'message': 'Build started',
                       'build_id': build['build']['id']})


# ─── Newsletter Subscribers ──────────────────────────────────────────

def get_subscribers(event, jinja_env):
    """GET /admin/subscribers — Newsletter subscribers page."""
    claims, err = _admin_check(event)
    if err:
        return err

    template = jinja_env.get_template('admin/subscribers.html')
    html = template.render(claims=claims)
    return _html(200, html, event)


def get_subscribers_json(event, jinja_env):
    """GET /api/admin/subscribers — Return subscribers as JSON."""
    claims, err = _admin_check(event)
    if err:
        # Ensure error response has proper headers
        if 'headers' not in err:
            err['headers'] = {'Content-Type': 'application/json'}
        return err

    try:
        sesv2 = boto3.client('sesv2', region_name='us-east-1')
        contacts = []
        # The contact list is the account-wide shared one; filter to this
        # stack's own topic so only next.dctech.events subscribers show.
        kwargs = {
            'ContactListName': CONTACT_LIST_NAME,
            'Filter': {
                'FilteredStatus': 'OPT_IN',
                'TopicFilter': {
                    'TopicName': NEWSLETTER_TOPIC,
                    'UseDefaultIfPreferenceUnavailable': False,
                },
            },
        }
        while True:
            response = sesv2.list_contacts(**kwargs)
            contacts.extend(response.get('Contacts', []))
            next_token = response.get('NextToken')
            if not next_token:
                break
            kwargs['NextToken'] = next_token

        # Sort by LastUpdatedTimestamp (newest first)
        contacts.sort(key=lambda x: x.get('LastUpdatedTimestamp', ''), reverse=True)

        subscribers = [{
            'email': c['EmailAddress'],
            'subscribed_at': c.get('LastUpdatedTimestamp', '').isoformat() if c.get('LastUpdatedTimestamp') else '',
            'unsubscribe_all': c.get('UnsubscribeAll', False),
        } for c in contacts]

        return _json(200, {
            'subscribers': subscribers,
            'count': len(subscribers)
        })
    except Exception as e:
        print(f"Error fetching subscribers: {e}")
        return _json(500, {'error': str(e)})
