"""
Public API routes (no authentication required).
"""

from db import get_all_events, get_all_categories
from routes.responses import json as _json


def health(event, jinja_env):
    """GET /health"""
    return _json(200, {'status': 'ok'}, event)


def get_events(event, jinja_env):
    """GET /api/events — returns JSON list of events."""
    params = event.get('queryStringParameters') or {}
    date_prefix = params.get('date')

    all_events = get_all_events(date_prefix)

    # Filter out hidden and duplicate events
    events = [e for e in all_events if not e.get('hidden') and not e.get('duplicate_of')]

    return _json(200, events, event)


def get_categories(event, jinja_env):
    """GET /api/categories — returns JSON map of categories."""
    return _json(200, get_all_categories(), event)
