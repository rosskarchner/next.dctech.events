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

from routes import public, submit, admin

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
    """Get the appropriate CORS origin header based on request origin."""
    request_origin = event.get('headers', {}).get('Origin', '') or event.get('headers', {}).get('origin', '')
    if request_origin in ALLOWED_ORIGINS:
        return request_origin
    # Fallback to wildcard for public endpoints (will be restricted below)
    return '*'


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
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': cors_origin,
                'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Trigger,HX-Trigger-Name,HX-Target,HX-Current-URL',
                'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
            },
            'body': json.dumps({'message': 'ok'}),
        }

    # Add CORS origin to all responses
    def add_cors(response):
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

        if path == '/api/admin/queue' and http_method == 'GET':
            return add_cors(admin.get_queue_json(event, jinja_env))

        if path == '/api/admin/subscribers' and http_method == 'GET':
            return add_cors(admin.get_subscribers_json(event, jinja_env))

        if path == '/api/admin/rebuild' and http_method == 'POST':
            return add_cors(admin.trigger_rebuild_json(event, jinja_env))

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
        if path == '/admin' and http_method == 'GET':
            return add_cors(admin.dashboard(event, jinja_env))

        if path == '/admin/queue' and http_method == 'GET':
            return add_cors(admin.get_queue(event, jinja_env))

        if path == '/admin/subscribers' and http_method == 'GET':
            return add_cors(admin.get_subscribers(event, jinja_env))

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
