"""calgen's Flask app factory.

Every resource-specific view function has moved into calgen.routes.* — see
that package's common.py for why the shared data/filter/format layer lives
there instead of being duplicated per resource module. What's left here is
genuinely app-wide: the factory itself, the two routes with no real
"resource" of their own (robots.txt, the 404 page), and the context
processor every template depends on.

The import block below re-exports exactly the names 5 of this package's 16
test files reach into via `from calgen.app import NAME` or
`monkeypatch.setattr('calgen.app.NAME', ...)` — moving one of these without
keeping it resolvable as calgen.app.NAME wouldn't fail those tests loudly
(setattr succeeds on any attribute); it would just silently stop them
testing anything real. Nothing else needs to be re-exported here — anything
else a caller needs (freeze.py included) imports directly from
calgen.routes.common or the specific resource module.
"""
from flask import Flask, render_template, send_from_directory
from jinja2 import FileSystemLoader
import json
import os

from calgen.site_config import get_config
from calgen.regions import load_region_plugin  # noqa: E402

from calgen.routes.common import (
    get_events, get_event_by_slug, get_categories,
    filter_events_to_upcoming_days, prepare_events_by_day, local_tz,
)
from calgen.routes.listings import filter_in_person_events, get_recently_added_count

from calgen.routes import groups as _groups_routes
from calgen.routes import events as _events_routes
from calgen.routes import locations as _locations_routes
from calgen.routes import categories as _categories_routes
from calgen.routes import feeds as _feeds_routes
from calgen.routes import posts as _posts_routes
from calgen.routes import newsletter as _newsletter_routes
from calgen.routes import listings as _listings_routes


def create_app(site_dir=None):
    """
    Create and return a configured Flask application.

    site_dir: path to the site directory (defaults to CWD). Templates and
    static files are read from {site_dir}.
    """
    if site_dir is None:
        site_dir = os.getcwd()
    site_dir = os.path.abspath(site_dir)

    static_dir = os.path.join(site_dir, 'static')
    app = Flask(
        __name__,
        static_folder=static_dir if os.path.exists(static_dir) else None,
    )
    # Templates live in the site directory only. calgen used to ship a
    # fallback set, but every one of them was shadowed by site/templates,
    # which made edits to the packaged copies silently do nothing.
    app.jinja_loader = FileSystemLoader(os.path.join(site_dir, 'templates'))
    app.region_plugin = load_region_plugin(site_dir)

    _register_routes(app)
    _groups_routes.register_routes(app)
    _events_routes.register_routes(app)
    _locations_routes.register_routes(app)
    _categories_routes.register_routes(app)
    _feeds_routes.register_routes(app)
    _posts_routes.register_routes(app)
    _newsletter_routes.register_routes(app)
    _listings_routes.register_routes(app)
    return app


def _register_routes(app):
    """Attach this module's own routes — everything else is a resource
    module's register_routes(app), called from create_app() above."""

    @app.context_processor
    def inject_config():
        cfg = get_config()
        plugin = app.region_plugin
        nav_locations = (
            [{'state': r['slug'], 'name': r['name']} for r in plugin.list_regions()]
            if plugin else []
        )
        return {
            'site_name': cfg.get('site_name', 'Tech Events'),
            'tagline': cfg.get('tagline', 'Technology conferences and meetups'),
            'base_url': cfg.get('base_url', ''),
            'add_events_link': cfg.get('add_events_link', ''),
            'newsletter_signup_link': cfg.get('newsletter_signup_link', ''),
            'sponsors': _load_sponsors(),
            'categories': get_categories(),
            'nav_locations': nav_locations,
        }

    @app.route("/robots.txt")
    def robots_txt():
        return send_from_directory(app.static_folder, 'robots.txt')

    @app.route('/404.html')
    def not_found_page():
        return render_template('404.html')


def _load_sponsors():
    sponsors_file = os.path.join('_data', 'sponsors.json')
    if not os.path.exists(sponsors_file):
        return []
    try:
        with open(sponsors_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading sponsors: {e}")
        return []
