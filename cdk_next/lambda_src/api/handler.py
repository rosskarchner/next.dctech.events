"""
Lambda handler for the next.dctech.events API.

Lightweight router that dispatches to route modules.
Returns HTML fragments for HTMX endpoints, JSON for public API.
Forked from dctech.events/backend/handler.py; adds /api/admin/rebuild.
"""

import json
import os
import traceback

from jinja2 import Environment, FileSystemLoader

from routes import public, submit, admin, events, corrections, preferences

# Set up Jinja2 template environment
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')
jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
)


# List of allowed origins for CORS
ALLOWED_ORIGINS = [
    'https://dctech.events',
    'https://www.dctech.events',
    'http://localhost:5000',
]


def get_cors_origin(event):
    """Get the CORS origin header value for this request, or None to omit it.

    Non-allowlisted origins get no Access-Control-Allow-Origin header at all
    (default-deny), not a wildcard — a wildcard here would let any site's JS
    read every response, including /api/admin/* JSON, from a browser holding
    a leaked bearer token.
    """
    request_origin = event.get('headers', {}).get('Origin', '') or event.get('headers', {}).get('origin', '')
    if request_origin in ALLOWED_ORIGINS:
        return request_origin
    return None


def html_response(status_code, body, headers=None, allow_all_origins=False):
    """Build an HTML response."""
    resp_headers = {
        'Content-Type': 'text/html; charset=utf-8',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
    }
    if headers:
        resp_headers.update(headers)
    return {
        'statusCode': status_code,
        'headers': resp_headers,
        'body': body,
    }


def json_response(status_code, body, allow_all_origins=False):
    """Build a JSON response."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
        },
        'body': json.dumps(body) if isinstance(body, (dict, list)) else body,
    }


def _dispatch_id_tail(path, prefix, routes, event, jinja_env, http_method):
    """Match {prefix}{id}[/{tail}] against `routes`.

    `routes` maps (tail, method) -> handler(event, jinja_env, id); tail is
    '' for the bare-id case (no trailing segment). Shared by every admin
    resource shaped like /api/admin/{thing}/{id}[/action] — corrections,
    events, and qa-runs all parsed this identically by hand before.

    Returns None (falls through to the 404 at the end of lambda_handler) if
    the path doesn't match the prefix, has no id, or no route matches this
    tail/method combination.
    """
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):].split('/')
    id_, tail = rest[0], (rest[1] if len(rest) > 1 else '')
    if not id_:
        return None
    handler = routes.get((tail, http_method))
    if handler is None:
        return None
    return handler(event, jinja_env, id_)


def lambda_handler(event, context):
    """Main Lambda entry point."""
    http_method = event.get('httpMethod', 'GET')
    path = event.get('path', '/')
    resource = event.get('resource', path)

    print(f"REQUEST: {http_method} {path} resource={resource}")

    # Determine CORS origin based on request
    cors_origin = get_cors_origin(event)

    # Handle CORS preflight
    if http_method == 'OPTIONS':
        preflight_headers = {
            'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
        }
        if cors_origin:
            preflight_headers['Access-Control-Allow-Origin'] = cors_origin
        return {
            'statusCode': 200,
            'headers': preflight_headers,
            'body': json.dumps({'message': 'ok'}),
        }

    # Add CORS origin to all responses, if this origin is allowlisted
    def add_cors(response):
        if cors_origin:
            if 'headers' not in response:
                response['headers'] = {}
            response['headers']['Access-Control-Allow-Origin'] = cors_origin
        return response

    try:
        # Public routes
        if path == '/health':
            return add_cors(public.health(event, jinja_env))

        if path == '/api/events':
            return add_cors(public.get_events(event, jinja_env))

        if path == '/api/categories':
            return add_cors(public.get_categories(event, jinja_env))

        # Magic-link submission: both are public at the gateway and do their
        # own token checking, so a submitter never needs a Cognito account.
        if path == '/api/submit-link' and http_method == 'POST':
            return add_cors(submit.request_link_json(event, jinja_env))

        if path == '/api/submissions' and http_method == 'POST':
            return add_cors(submit.submit_event_json(event, jinja_env))

        if path == '/api/my-submissions' and http_method == 'GET':
            return add_cors(submit.my_submissions_json(event, jinja_env))

        # Subscriber category/region preferences: reached via the same
        # account-free magic-link pattern (purpose='prefs'), public at the
        # gateway for the same reason /api/submissions is.
        if path == '/api/preferences' and http_method == 'GET':
            return add_cors(preferences.get_preferences_json(event, jinja_env))

        if path == '/api/preferences' and http_method == 'POST':
            return add_cors(preferences.update_preferences_json(event, jinja_env))

        # Corrections: public POST is unauthenticated at the gateway for the
        # same reason /api/submissions is — the Lambda verifies the magic
        # link itself. The public read is a separate, narrower endpoint from
        # the admin event detail route, on purpose (see get_public_event_json).
        if path == '/api/corrections' and http_method == 'POST':
            return add_cors(corrections.submit_correction_json(event, jinja_env))

        if path.startswith('/api/public/events/') and http_method == 'GET':
            guid = path[len('/api/public/events/'):].split('/')[0]
            if guid:
                return add_cors(
                    corrections.get_public_event_json(event, jinja_env, guid))

        if path.startswith('/api/public/recurring/') and http_method == 'GET':
            slug = path[len('/api/public/recurring/'):].split('/')[0]
            if slug:
                return add_cors(
                    corrections.get_public_recurring_json(event, jinja_env, slug))

        if path == '/api/admin/corrections' and http_method == 'GET':
            return add_cors(corrections.list_corrections_json(event, jinja_env))

        if path.startswith('/api/admin/corrections/'):
            result = _dispatch_id_tail(path, '/api/admin/corrections/', {
                ('approve', 'POST'): corrections.approve_correction_json,
                ('reject', 'POST'): corrections.reject_correction_json,
                ('', 'GET'): corrections.get_correction_json,
            }, event, jinja_env, http_method)
            if result is not None:
                return add_cors(result)

        if path == '/api/admin/queue' and http_method == 'GET':
            return add_cors(admin.get_queue_json(event, jinja_env))

        if path == '/api/admin/subscribers' and http_method == 'GET':
            return add_cors(admin.get_subscribers_json(event, jinja_env))

        if path == '/api/admin/rebuild' and http_method == 'POST':
            return add_cors(admin.trigger_rebuild_json(event, jinja_env))

        # ── Events and overlays (the human QA surface) ──────────────
        # /bulk is matched before the {guid} block below, or it parses as a
        # guid — the same trap the drafts routes navigate with /approve and
        # /reject.
        if path == '/api/admin/events' and http_method == 'GET':
            return add_cors(events.list_events_json(event, jinja_env))

        if path == '/api/admin/events/bulk' and http_method == 'POST':
            return add_cors(events.bulk_json(event, jinja_env))

        if path.startswith('/api/admin/events/'):
            result = _dispatch_id_tail(path, '/api/admin/events/', {
                ('overlay', 'PUT'): events.put_overlay_json,
                ('overlay', 'DELETE'): events.delete_overlay_json,
                ('review-status', 'PUT'): events.put_review_status_json,
                ('', 'GET'): events.get_event_json,
            }, event, jinja_env, http_method)
            if result is not None:
                return add_cors(result)

        if path.startswith('/api/admin/qa-runs/'):
            result = _dispatch_id_tail(path, '/api/admin/qa-runs/', {
                ('revert', 'POST'): events.revert_qa_run_json,
                ('', 'GET'): events.get_qa_run_json,
            }, event, jinja_env, http_method)
            if result is not None:
                return add_cors(result)

        if path.startswith('/api/admin/drafts/') and path.endswith('/approve') and http_method == 'POST':
            draft_id = path.split('/')[4]
            return add_cors(admin.approve_draft_json(event, jinja_env, draft_id))

        if path.startswith('/api/admin/drafts/') and path.endswith('/reject') and http_method == 'POST':
            draft_id = path.split('/')[4]
            return add_cors(admin.reject_draft_json(event, jinja_env, draft_id))

        if path.startswith('/api/admin/drafts/') and http_method == 'GET':
            draft_id = path.split('/')[4]
            return add_cors(admin.get_draft_json(event, jinja_env, draft_id))

        # Trusted submitters (skip the moderation queue)
        if path == '/api/admin/trusted' and http_method == 'GET':
            return add_cors(admin.list_trusted_json(event, jinja_env))

        if path == '/api/admin/trusted' and http_method == 'POST':
            return add_cors(admin.trust_submitter_json(event, jinja_env))

        if path.startswith('/api/admin/trusted/') and http_method == 'DELETE':
            # Emails contain no slashes, but they are URL-encoded by the
            # client, so take the remainder of the path rather than one segment.
            trusted_email = path[len('/api/admin/trusted/'):]
            return add_cors(
                admin.untrust_submitter_json(event, jinja_env, trusted_email))

        # Free-form /updates posts
        if path == '/api/admin/posts' and http_method == 'GET':
            return add_cors(admin.list_posts_json(event, jinja_env))

        if path == '/api/admin/posts' and http_method == 'POST':
            return add_cors(admin.create_post_json(event, jinja_env))

        if path.startswith('/api/admin/posts/'):
            slug = path.split('/')[4]
            if http_method == 'GET':
                return add_cors(admin.get_post_json(event, jinja_env, slug))
            if http_method == 'PUT':
                return add_cors(admin.update_post_json(event, jinja_env, slug))
            if http_method == 'DELETE':
                return add_cors(admin.delete_post_json(event, jinja_env, slug))

        # Submission routes (authenticated)
        if path == '/submit' and http_method == 'GET':
            return add_cors(submit.submit_form(event, jinja_env))
        if path == '/submit' and http_method == 'POST':
            return add_cors(submit.submit_event(event, jinja_env))

        if path == '/my-submissions' and http_method == 'GET':
            return add_cors(submit.my_submissions(event, jinja_env))

        # Admin routes (authenticated + admin group)
        # No /admin or /admin/subscribers: both rendered templates that do not
        # exist in this fork (admin/dashboard.html, admin/subscribers.html) and
        # returned 500. The live UI is the static one under /edit/.
        if path == '/admin/queue' and http_method == 'GET':
            return add_cors(admin.get_queue(event, jinja_env))

        if path == '/admin/rebuild' and http_method == 'POST':
            return add_cors(admin.trigger_rebuild_json(event, jinja_env))

        if path.startswith('/admin/draft/') and path.endswith('/approve') and http_method == 'POST':
            draft_id = path.split('/')[3]
            return add_cors(admin.approve_draft(event, jinja_env, draft_id))

        if path.startswith('/admin/draft/') and path.endswith('/approve-form') and http_method == 'GET':
            draft_id = path.split('/')[3]
            return add_cors(admin.get_approve_form(event, jinja_env, draft_id))

        if path.startswith('/admin/draft/') and path.endswith('/row') and http_method == 'GET':
            draft_id = path.split('/')[3]
            return add_cors(admin.get_draft_row(event, jinja_env, draft_id))

        if path.startswith('/admin/draft/') and path.endswith('/reject') and http_method == 'POST':
            draft_id = path.split('/')[3]
            return add_cors(admin.reject_draft(event, jinja_env, draft_id))

        if path.startswith('/admin/draft/') and http_method == 'GET':
            draft_id = path.split('/')[3]
            return add_cors(admin.get_draft(event, jinja_env, draft_id))

        return add_cors(json_response(404, {'error': 'Not found'}))

    except Exception as e:
        print(f"ERROR handling {http_method} {path}: {traceback.format_exc()}")
        return add_cors(json_response(500, {'error': 'Internal server error'}))
