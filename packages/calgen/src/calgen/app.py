from flask import Flask, render_template, Response, send_from_directory
from jinja2 import FileSystemLoader
from datetime import date, datetime, timedelta, time
import os
import yaml
import pytz
import calendar
import json
from urllib.parse import urlparse
from icalendar import Calendar, Event as ICalEvent
import hashlib
import xml.etree.ElementTree as ET  # nosec B405
from email.utils import formatdate
import io

from calgen.site_config import get_config
from calgen.location_utils import extract_location_info, get_region_name
from calgen.event_utils import calculate_event_hash
from calgen.updates import (
    get_all_posts, get_free_post, get_free_posts, get_update_post,
    get_update_posts, summarize,
)

# Valid US state/territory codes for location route validation
VALID_US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
    'DC', 'PR', 'VI', 'GU', 'MP', 'AS',
}

# Module-level singletons — initialized lazily on first call, reset via site_config.reset_config()
config = get_config()
timezone_name = config.get('timezone', 'US/Eastern')
local_tz = pytz.timezone(timezone_name)


from calgen.regions import load_region_plugin  # noqa: E402


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
    return app


def _register_routes(app):
    """Attach all URL rules to the app."""

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

    @app.route("/")
    def homepage():
        all_events = get_events()
        events = filter_in_person_events(all_events)
        two_week_events = filter_events_to_next_two_weeks(events)
        days = prepare_events_by_day(two_week_events, add_week_links=True)

        cfg = get_config()
        base_url = cfg.get('base_url', '')
        stats = get_stats().copy()
        stats['upcoming_events'] = len(two_week_events)
        stats['total_upcoming_events'] = len(events)

        return render_template('homepage.html',
                               days=days,
                               stats=stats,
                               base_url=base_url,
                               upcoming_months=get_upcoming_months(),
                               categories_with_counts=get_categories_with_event_counts(),
                               sidebar=get_sidebar_data())

    @app.route("/virtual/")
    def virtual_events_page():
        all_events = get_events()
        events = filter_virtual_events(all_events)
        days = prepare_events_by_day(events, add_week_links=False)
        cfg = get_config()
        base_url = cfg.get('base_url', '')
        stats = {'upcoming_events': len(events)}
        return render_template('virtual_events.html',
                               days=days,
                               stats=stats,
                               base_url=base_url)

    @app.route("/week/<week_id>/")
    def week_page(week_id):
        try:
            year, week_num = parse_week_identifier(week_id)
            week_start, week_end = get_iso_week_dates(year, week_num)
        except Exception:
            return "Invalid week identifier", 404

        events = get_events()
        week_events = filter_events_by_week(events, week_start, week_end)
        days = prepare_events_by_day(week_events)
        week_start_formatted = week_start.strftime('%B %-d, %Y')
        stats = {'upcoming_events': len(week_events)}
        base_url = get_config().get('base_url', '')
        return render_template('week_page.html',
                               days=days,
                               stats=stats,
                               week_id=week_id,
                               week_start=week_start,
                               week_end=week_end,
                               week_start_formatted=week_start_formatted,
                               base_url=base_url)

    @app.route("/<int:year>/<int:month>/")
    def month_page(year, month):
        try:
            if month < 1 or month > 12:
                return "Invalid month", 404
            first_day = date(year, month, 1)
            if month == 12:
                last_day = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = date(year, month + 1, 1) - timedelta(days=1)
        except ValueError:
            return "Invalid date", 404

        events = get_events()
        month_events = filter_events_by_month(events, first_day, last_day)
        days = prepare_events_by_day(month_events)
        month_name = calendar.month_name[month]
        stats = {'upcoming_events': len(month_events)}

        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1

        return render_template('month_page.html',
                               days=days,
                               stats=stats,
                               month_name=month_name,
                               year=year,
                               month=month,
                               prev_month=prev_month,
                               prev_year=prev_year,
                               next_month=next_month,
                               next_year=next_year,
                               sidebar=get_sidebar_data(active_month=(year, month)))

    @app.route("/locations/")
    def locations_index():
        plugin = app.region_plugin
        if not plugin:
            return "Not found", 404
        events = get_events()
        counts = {}
        for event in events:
            slug = event.get('region')
            if slug:
                counts[slug] = counts.get(slug, 0) + 1
        locations = [
            {'state': r['slug'], 'name': r['name'], 'count': counts.get(r['slug'], 0)}
            for r in plugin.list_regions()
            if counts.get(r['slug'], 0) > 0
        ]
        return render_template('locations_index.html', locations=locations)

    @app.route("/groups/")
    def approved_groups_list():
        groups = get_approved_groups()
        return render_template('approved_groups_list.html',
                               groups=groups,
                               next_key=None,
                               has_next=False)

    @app.route("/newsletter.html")
    def newsletter_html():
        events = get_events()
        two_week_events = filter_events_to_next_two_weeks(events)
        days = prepare_events_by_day(two_week_events)
        prepare_newsletter_titles(days)
        stats = get_stats().copy()
        stats['upcoming_events'] = len(two_week_events)
        return render_template('newsletter.html',
                               days=days,
                               stats=stats,
                               base_url=get_config().get('base_url', ''),
                               upcoming_months=get_upcoming_months(),
                               categories_with_counts=get_categories_with_event_counts())

    @app.route("/newsletter.txt")
    def newsletter_text():
        events = get_events()
        two_week_events = filter_events_to_next_two_weeks(events)
        days = prepare_events_by_day(two_week_events)
        prepare_newsletter_titles(days)
        stats = get_stats().copy()
        stats['upcoming_events'] = len(two_week_events)
        response = render_template('newsletter.txt',
                                   days=days,
                                   stats=stats,
                                   base_url=get_config().get('base_url', ''),
                                   upcoming_months=get_upcoming_months(),
                                   categories_with_counts=get_categories_with_event_counts())
        return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

    @app.route("/locations/<slug>/")
    def region_page(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        days = prepare_events_by_day(filtered_events)
        stats = {'upcoming_events': len(filtered_events)}
        return render_template('location_page.html',
                               days=days,
                               stats=stats,
                               location_name=region['name'],
                               location_type='region')

    @app.route("/categories/")
    def categories_index():
        categories = get_categories()
        events = get_events()
        categories_with_counts = []
        for slug, category in sorted(categories.items(), key=lambda x: x[1]['name']):
            count = len([e for e in events if slug in e.get('categories', [])])
            categories_with_counts.append({
                'slug': slug,
                'name': category['name'],
                'description': category.get('description', ''),
                'count': count
            })
        return render_template('categories_index.html', categories=categories_with_counts)

    @app.route("/categories/<slug>/")
    def category_page(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        days = prepare_events_by_day(filtered_events)
        category = categories[slug]
        stats = {'upcoming_events': len(filtered_events)}
        return render_template('category_page.html',
                               days=days,
                               stats=stats,
                               category_name=category['name'],
                               category_description=category.get('description', ''),
                               category_slug=slug,
                               sidebar=get_sidebar_data(active_category=slug))

    @app.route("/categories/<slug>/<int:year>/<int:month>/")
    def category_month_page(slug, year, month):
        """Show events for a single category within a single month."""
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        try:
            if month < 1 or month > 12:
                return "Invalid month", 404
            first_day = date(year, month, 1)
            if month == 12:
                last_day = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = date(year, month + 1, 1) - timedelta(days=1)
        except ValueError:
            return "Invalid date", 404

        events = get_events()
        category_events = [e for e in events if slug in e.get('categories', [])]
        month_events = filter_events_by_month(category_events, first_day, last_day)
        days = prepare_events_by_day(month_events)
        category = categories[slug]
        stats = {'upcoming_events': len(month_events)}
        return render_template('category_month_page.html',
                               days=days,
                               stats=stats,
                               category_name=category['name'],
                               category_description=category.get('description', ''),
                               category_slug=slug,
                               month_name=calendar.month_name[month],
                               year=year,
                               month=month,
                               sidebar=get_sidebar_data(active_category=slug,
                                                        active_month=(year, month)))

    @app.route("/feeds/")
    def feeds_page():
        cfg = get_config()
        only_states = cfg.get('only_states', [])
        categories = get_categories()
        events = get_events()

        categories_with_counts = []
        for slug, category in sorted(categories.items(), key=lambda x: x[1]['name']):
            count = len([e for e in events if slug in e.get('categories', [])])
            if count > 0:
                categories_with_counts.append({'slug': slug, 'name': category['name'], 'count': count})

        locations = [
            {'state': s.lower(), 'name': get_region_name(s.upper())}
            for s in only_states
        ]

        return render_template('feeds.html',
                               categories=categories_with_counts,
                               locations=locations)

    @app.route("/sitemap.xml")
    def sitemap():
        cfg = get_config()
        base_url = cfg.get('base_url', '')
        now = datetime.now().strftime('%Y-%m-%d')

        urls = [
            {'loc': f"{base_url}/", 'lastmod': now},
            {'loc': f"{base_url}/virtual/", 'lastmod': now},
            {'loc': f"{base_url}/categories/", 'lastmod': now},
            {'loc': f"{base_url}/groups/", 'lastmod': now},
        ]

        plugin = app.region_plugin
        if plugin:
            urls.append({'loc': f"{base_url}/locations/", 'lastmod': now})
            for region in plugin.list_regions():
                urls.append({'loc': f"{base_url}/locations/{region['slug']}/", 'lastmod': now})

        for slug in get_categories().keys():
            urls.append({'loc': f"{base_url}/categories/{slug}/", 'lastmod': now, 'changefreq': 'daily'})

        for week_id in get_upcoming_weeks(12):
            urls.append({'loc': f"{base_url}/week/{week_id}/", 'lastmod': now, 'changefreq': 'daily'})

        urls.append({'loc': f"{base_url}/updates/", 'lastmod': now, 'changefreq': 'weekly'})
        for post in get_all_posts():
            # Weekly roundups are frozen snapshots and never change again;
            # free-form posts can be edited after publication.
            urls.append({'loc': f"{base_url}{post['url']}",
                         'lastmod': post['published_on'].strftime('%Y-%m-%d'),
                         'changefreq': 'never' if post['kind'] == 'weekly' else 'monthly'})

        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for url in urls:
            xml.append('  <url>')
            xml.append(f'    <loc>{url["loc"]}</loc>')
            xml.append(f'    <lastmod>{url["lastmod"]}</lastmod>')
            xml.append(f'    <changefreq>{url.get("changefreq", "daily")}</changefreq>')
            xml.append('  </url>')
        xml.append('</urlset>')
        return Response('\n'.join(xml), mimetype='application/xml')

    @app.route("/events.json")
    def events_json():
        events_json_file = os.path.join('_data', 'events.json')
        if os.path.exists(events_json_file):
            with open(events_json_file, 'r', encoding='utf-8') as f:
                return Response(f.read(), mimetype='application/json')
        events = get_events()
        return Response(json.dumps(events, indent=2), mimetype='application/json')

    @app.route("/categories.json")
    def categories_json():
        categories = get_categories()
        formatted = {
            slug: {'slug': slug, 'name': cat.get('name', slug), 'description': cat.get('description', '')}
            for slug, cat in categories.items()
        }
        return Response(json.dumps(formatted, indent=2), mimetype='application/json')

    @app.route("/events.ics")
    def ical_feed():
        events = get_events()
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        tagline = cfg.get('tagline', '')
        return _generate_ical_feed(events, site_name, tagline)

    @app.route("/categories/<slug>/feed.ics")
    def category_ical_feed(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        category = categories[slug]
        return _generate_ical_feed(
            filtered_events,
            f"{category['name']} Events - {site_name}",
            f"{category['name']} events"
        )

    @app.route("/locations/<slug>/feed.ics")
    def location_ical_feed(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        return _generate_ical_feed(
            filtered_events,
            f"{region['name']} Events - {site_name}",
            f"Technology events in {region['name']}"
        )

    @app.route("/events-feed.xml")
    def events_rss_feed():
        events = get_events()
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_rss_feed(events, f"{site_name}", f"Upcoming events", f"{base_url}/")

    @app.route("/categories/<slug>/feed.xml")
    def category_rss_feed(slug):
        categories = get_categories()
        if slug not in categories:
            return "Category not found", 404
        events = get_events()
        filtered_events = [e for e in events if slug in e.get('categories', [])]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        category = categories[slug]
        return _generate_rss_feed(
            filtered_events,
            f"{category['name']} Events - {site_name}",
            f"Upcoming {category['name']} events",
            f"{base_url}/categories/{slug}/"
        )

    @app.route("/locations/<slug>/feed.xml")
    def location_rss_feed(slug):
        plugin = app.region_plugin
        if not plugin:
            return "Region not found", 404
        regions = plugin.list_regions()
        region = next((r for r in regions if r['slug'] == slug), None)
        if not region:
            return "Region not found", 404
        events = get_events()
        filtered_events = [e for e in events if e.get('region') == slug]
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_rss_feed(
            filtered_events,
            f"{region['name']} Events - {site_name}",
            f"Upcoming technology events in {region['name']}",
            f"{base_url}/locations/{slug}/"
        )

    # ── /updates — the weekly blog (replaces updates.dctech.events) ────
    @app.route("/updates/")
    def updates_index():
        posts = get_all_posts()
        for post in posts:
            post['summary'] = summarize(post)
        cfg = get_config()
        return render_template('updates_index.html',
                               posts=posts,
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/<slug>/")
    def free_post(slug):
        post = get_free_post(slug)
        if not post:
            return "Update not found", 404
        cfg = get_config()
        return render_template('free_post.html',
                               post=post,
                               summary=summarize(post),
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/<int:year>/<int:month>/<int:day>/")
    def update_post(year, month, day):
        post = get_update_post(year, month, day)
        if not post:
            return "Update not found", 404
        days = prepare_events_by_day(post.get('events', []))
        cfg = get_config()
        return render_template('update_post.html',
                               post=post,
                               days=days,
                               summary=summarize(post),
                               base_url=cfg.get('base_url', ''))

    @app.route("/updates/feed.xml")
    def updates_rss_feed():
        cfg = get_config()
        site_name = cfg.get('site_name', 'Tech Events')
        base_url = cfg.get('base_url', '')
        return _generate_updates_rss(get_all_posts(), site_name, base_url)

    @app.route('/404.html')
    def not_found_page():
        return render_template('404.html')


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

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


def get_events(include_hidden=False):
    events_file = os.path.join('_data', 'all_events.json')
    try:
        with open(events_file, 'r') as f:
            events = json.load(f)
        if not include_hidden:
            events = [e for e in events if not e.get('hidden', False) and not e.get('duplicate_of')]
        today = datetime.now(local_tz).date().strftime('%Y-%m-%d')
        events = [e for e in events if e.get('date', '') >= today]

        groups = get_approved_groups()
        group_map = {g.get('name'): g for g in groups if g.get('name')}
        for event in events:
            if not event.get('group_website') and event.get('group'):
                group = group_map.get(event['group'])
                if group and group.get('website'):
                    event['group_website'] = group['website']
        return events
    except Exception as e:
        print(f"Error loading events from {events_file}: {e}")
        return []


def get_approved_groups():
    groups = []
    groups_dir = '_groups'
    if not os.path.exists(groups_dir):
        return groups
    for filename in os.listdir(groups_dir):
        if filename.endswith('.yaml'):
            slug = filename[:-5]
            try:
                with open(os.path.join(groups_dir, filename), 'r') as f:
                    group = yaml.safe_load(f)
                    group['id'] = slug
                    groups.append(group)
            except Exception as e:
                print(f"Error loading group {filename}: {e}")
    groups.sort(key=lambda x: x.get('name', '').lower())
    return groups


def get_categories():
    categories = {}
    categories_dir = '_categories'
    if not os.path.exists(categories_dir):
        return categories
    for filename in os.listdir(categories_dir):
        if filename.endswith('.yaml'):
            slug = filename[:-5]
            try:
                with open(os.path.join(categories_dir, filename), 'r') as f:
                    cat = yaml.safe_load(f)
                    cat['slug'] = slug
                    categories[slug] = cat
            except Exception as e:
                print(f"Error loading category {filename}: {e}")
    return categories


def get_upcoming_months():
    events = get_events()
    months = {}
    for event in events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
            month_key = (event_date.year, event_date.month)
            if month_key not in months:
                month_name = calendar.month_name[event_date.month]
                months[month_key] = {
                    'year': event_date.year,
                    'month': event_date.month,
                    'name': f"{month_name} {event_date.year}",
                    'count': 0,
                    'url': f"/{event_date.year}/{event_date.month}/"
                }
            months[month_key]['count'] += 1
        except ValueError:
            continue
    return [months[k] for k in sorted(months.keys())]


def get_categories_with_event_counts():
    categories = get_categories()
    events = get_events()
    result = []
    for slug, category in categories.items():
        count = len([e for e in events if slug in e.get('categories', [])])
        if count > 0:
            result.append({
                'slug': slug,
                'name': category['name'],
                'count': count,
                'url': f"/categories/{slug}/"
            })
    result.sort(key=lambda x: x['count'], reverse=True)
    return result


def _month_bounds(year, month):
    """Return (first_day, last_day) date objects for the given month."""
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    return first_day, last_day


def _category_url(slug, active_month=None):
    """URL for a category, optionally pinned to an active month."""
    if active_month:
        year, month = active_month
        return f"/categories/{slug}/{year}/{month}/"
    return f"/categories/{slug}/"


def _month_url(year, month, active_category=None):
    """URL for a month, optionally pinned to an active category."""
    if active_category:
        return f"/categories/{active_category}/{year}/{month}/"
    return f"/{year}/{month}/"


def get_category_month_combos():
    """Return sorted (slug, year, month) tuples that have at least one event.

    Used to enumerate the category+month pages worth freezing.
    """
    events = get_events()
    categories = get_categories()
    combos = set()
    for event in events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
        except ValueError:
            continue
        for slug in event.get('categories', []):
            if slug in categories:
                combos.add((slug, event_date.year, event_date.month))
    return sorted(combos)


def get_sidebar_data(active_category=None, active_month=None):
    """Build data for the cross-filterable browse sidebar.

    The category list reflects counts within the active month (if any) and the
    month list reflects counts within the active category (if any), so the two
    axes stay consistent with each other. Each link carries the other axis's
    active selection so clicking narrows rather than resets.
    """
    events = get_events()
    categories = get_categories()

    # Category list — counts constrained to the active month, if set.
    cat_events = events
    if active_month:
        first_day, last_day = _month_bounds(*active_month)
        cat_events = filter_events_by_month(events, first_day, last_day)
    category_list = []
    for slug, category in categories.items():
        count = len([e for e in cat_events if slug in e.get('categories', [])])
        if count == 0 and slug != active_category:
            continue
        category_list.append({
            'slug': slug,
            'name': category['name'],
            'count': count,
            'url': _category_url(slug, active_month),
            'active': slug == active_category,
        })
    category_list.sort(key=lambda x: x['name'].lower())

    # Month list — counts constrained to the active category, if set.
    month_events = events
    if active_category:
        month_events = [e for e in events if active_category in e.get('categories', [])]
    month_counts = {}
    for event in month_events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
        except ValueError:
            continue
        key = (event_date.year, event_date.month)
        month_counts[key] = month_counts.get(key, 0) + 1
    if active_month and active_month not in month_counts:
        month_counts[active_month] = 0
    month_list = []
    for (year, month) in sorted(month_counts.keys()):
        month_list.append({
            'year': year,
            'month': month,
            'name': f"{calendar.month_name[month]} {year}",
            'count': month_counts[(year, month)],
            'url': _month_url(year, month, active_category),
            'active': active_month == (year, month),
        })

    return {
        'categories': category_list,
        'months': month_list,
        'active_category': active_category,
        'active_month': active_month,
    }


def get_stats():
    stats_file = os.path.join('_data', 'stats.yaml')
    if not os.path.exists(stats_file):
        return {}
    try:
        with open(stats_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading stats: {e}")
        return {}


# ---------------------------------------------------------------------------
# Event filtering helpers
# ---------------------------------------------------------------------------

def filter_events_to_next_two_weeks(events):
    today = datetime.now(local_tz).date()
    two_weeks_from_now = today + timedelta(days=14)
    return [
        e for e in events
        if e.get('date') and today <= datetime.strptime(e['date'], '%Y-%m-%d').date() <= two_weeks_from_now
    ]


def filter_events_by_location(events, city=None, state=None):
    filtered = []
    for event in events:
        event_city, event_state = extract_location_info(event.get('location', ''))
        if city and state:
            if event_city and event_state and event_city.lower() == city.lower() and event_state == state:
                filtered.append(event)
        elif state:
            if event_state == state:
                filtered.append(event)
        elif city:
            if event_city and event_city.lower() == city.lower():
                filtered.append(event)
    return filtered


def is_virtual_event(event):
    return event.get('location_type') == 'virtual'


def filter_virtual_events(events):
    return [e for e in events if is_virtual_event(e)]


def filter_in_person_events(events):
    return [e for e in events if not is_virtual_event(e)]


def filter_events_by_week(events, week_start, week_end):
    filtered = []
    for event in events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
            end_date = event_date
            if 'end_date' in event:
                try:
                    end_date = datetime.strptime(event['end_date'], '%Y-%m-%d').date()
                except ValueError:
                    pass
            if event_date <= week_end and end_date >= week_start:
                filtered.append(event)
        except ValueError:
            continue
    return filtered


def filter_events_by_month(events, month_start, month_end):
    filtered = []
    for event in events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
            end_date = event_date
            if 'end_date' in event:
                try:
                    end_date = datetime.strptime(event['end_date'], '%Y-%m-%d').date()
                except ValueError:
                    pass
            if event_date <= month_end and end_date >= month_start:
                filtered.append(event)
        except ValueError:
            continue
    return filtered


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------

def get_iso_week_dates(year, week):
    jan4 = date(year, 1, 4)
    week_1_monday = jan4 - timedelta(days=jan4.weekday())
    week_start = week_1_monday + timedelta(weeks=week - 1)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def get_week_identifier(target_date):
    iso_year, iso_week, _ = target_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def parse_week_identifier(week_id):
    parts = week_id.split('-W')
    return int(parts[0]), int(parts[1])


def get_upcoming_weeks(num_weeks=12):
    weeks = set()
    current_date = date.today()
    for i in range(num_weeks):
        weeks.add(get_week_identifier(current_date + timedelta(weeks=i)))
    for event in get_events():
        event_date_str = event.get('date')
        if event_date_str:
            try:
                event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
                weeks.add(get_week_identifier(event_date))
                end_date_str = event.get('end_date')
                if end_date_str:
                    try:
                        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                        current = event_date
                        while current <= end_date:
                            weeks.add(get_week_identifier(current))
                            current += timedelta(days=1)
                    except (ValueError, TypeError):
                        pass
            except (ValueError, TypeError):
                pass
    return sorted(list(weeks))


# ---------------------------------------------------------------------------
# Event display helpers
# ---------------------------------------------------------------------------

def prepare_events_by_day(events, add_week_links=False):
    events_by_day = {}
    for event in events:
        day_key = event.get('date')
        if not day_key:
            continue
        start_date = datetime.strptime(day_key, '%Y-%m-%d').date()
        end_date = None
        if 'end_date' in event:
            try:
                end_date = datetime.strptime(event['end_date'], '%Y-%m-%d').date()
            except ValueError:
                pass
        event_dates = []
        if end_date and end_date > start_date:
            current_date = start_date
            while current_date <= end_date:
                event_dates.append(current_date)
                current_date += timedelta(days=1)
        else:
            event_dates = [start_date]

        for i, event_date in enumerate(event_dates):
            day_key = event_date.strftime('%Y-%m-%d')
            short_date = event_date.strftime('%a %-m/%-d')
            if day_key not in events_by_day:
                week_url = None
                if add_week_links:
                    week_url = f"/week/{get_week_identifier(event_date)}/#{day_key}"
                events_by_day[day_key] = {
                    'date': day_key,
                    'short_date': short_date,
                    'week_url': week_url,
                    'time_slots': {}
                }

            time_key = 'All Day'
            formatted_time = 'All Day'
            original_time = event.get('time', '')
            if isinstance(original_time, dict):
                original_time = original_time.get(day_key, '')
            if original_time and isinstance(original_time, str) and ':' in original_time:
                try:
                    time_obj = datetime.strptime(original_time.strip(), '%H:%M').time()
                    time_key = time_obj.strftime('%H:%M')
                    formatted_time = time_obj.strftime('%-I:%M %p').lower()
                except ValueError:
                    pass

            event_copy = event.copy()
            event_copy['time'] = time_key if time_key != 'All Day' else ''
            event_copy['formatted_time'] = formatted_time
            event_copy['display_title'] = f"{event['title']} (continuing)" if i > 0 else event['title']

            if time_key not in events_by_day[day_key]['time_slots']:
                events_by_day[day_key]['time_slots'][time_key] = []
            events_by_day[day_key]['time_slots'][time_key].append(event_copy)

    days_data = []
    for day in sorted(events_by_day.keys()):
        day_data = events_by_day[day]

        def time_sort_key(ts):
            if ts == 'All Day':
                return (-1, 0)
            try:
                h, m = map(int, ts.split(':'))
                return (h, m)
            except Exception:
                return (0, 0)

        time_slots = []
        for t in sorted(day_data['time_slots'].keys(), key=time_sort_key):
            time_slots.append({
                'time': t,
                'events': sorted(day_data['time_slots'][t], key=lambda x: x.get('title', ''))
            })
        day_data['time_slots'] = time_slots
        day_data['has_events'] = bool(time_slots)
        days_data.append(day_data)
    return days_data


def prepare_newsletter_titles(days):
    for day in days:
        for time_slot in day['time_slots']:
            for event in time_slot['events']:
                base_title = event.get('display_title', event.get('title', 'Untitled Event'))
                event['newsletter_title'] = f"Virtual: {base_title}" if is_virtual_event(event) else base_title
    return days


# ---------------------------------------------------------------------------
# Feed generators
# ---------------------------------------------------------------------------

def _generate_ical_feed(filtered_events, calendar_name, calendar_description):
    cfg = get_config()
    site_name = cfg.get('site_name', 'Tech Events')
    base_url = cfg.get('base_url', 'https://example.com')
    domain = urlparse(base_url).netloc or 'example.com'

    groups = get_approved_groups()
    group_websites = {g.get('name'): g.get('website') for g in groups if g.get('website')}

    cal = Calendar()
    cal.add('prodid', f'-//{site_name}//')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', calendar_name)
    cal.add('x-wr-caldesc', calendar_description)

    for event in filtered_events:
        event_date_str = event.get('date')
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            continue

        event_time_str = event.get('time', '')
        is_all_day = not (event_time_str and isinstance(event_time_str, str) and ':' in event_time_str)

        if is_all_day:
            event_datetime = event_date
            end_date_str = event.get('end_date')
            if end_date_str:
                try:
                    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                    end_datetime = end_date + timedelta(days=1)
                except (ValueError, TypeError):
                    end_datetime = event_date + timedelta(days=1)
            else:
                end_datetime = event_date + timedelta(days=1)
        else:
            try:
                event_time = datetime.strptime(event_time_str.strip(), '%H:%M').time()
            except ValueError:
                event_datetime = event_date
                end_datetime = event_date + timedelta(days=1)
                is_all_day = True

            if not is_all_day:
                event_datetime_local = datetime.combine(event_date, event_time)
                event_datetime_local = local_tz.localize(event_datetime_local)
                event_datetime = event_datetime_local.astimezone(pytz.UTC)
                end_date_str = event.get('end_date')
                if end_date_str:
                    try:
                        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                        end_time = datetime.strptime('23:59', '%H:%M').time()
                        end_datetime_local = datetime.combine(end_date, end_time)
                        end_datetime_local = local_tz.localize(end_datetime_local)
                        end_datetime = end_datetime_local.astimezone(pytz.UTC)
                    except (ValueError, TypeError):
                        end_datetime = event_datetime + timedelta(hours=1)
                else:
                    end_datetime = event_datetime + timedelta(hours=1)

        ical_event = ICalEvent()
        ical_event.add('summary', event.get('title', 'Untitled Event'))
        ical_event.add('dtstart', event_datetime)
        ical_event.add('dtend', end_datetime)
        if event.get('location'):
            ical_event.add('location', event['location'])

        desc_parts = []
        if event.get('url'):
            desc_parts.append(f"Event URL: {event['url']}")
        if event.get('group'):
            desc_parts.append(f"Organized by: {event['group']}")
        if event.get('cost'):
            desc_parts.append(f"Cost: {event['cost']}")
        if desc_parts:
            ical_event.add('description', '\n'.join(desc_parts))
        if event.get('url'):
            ical_event.add('url', event['url'])
        if event.get('group'):
            group_name = event['group']
            group_website = group_websites.get(group_name)
            if group_website:
                ical_event.add('organizer', group_website, parameters={'CN': group_name})

        uid_hash = calculate_event_hash(event_date_str, event_time_str, event.get('title', 'event'), event.get('url'))
        ical_event.add('uid', f"{uid_hash}@{domain}")
        ical_event.add('dtstamp', datetime.now(pytz.UTC))
        cal.add_component(ical_event)

    return Response(cal.to_ical(), mimetype='text/calendar')


def _generate_updates_rss(posts, site_name, base_url):
    """RSS for the weekly posts.

    Deliberately separate from _generate_rss_feed: that one emits one item per
    *event* keyed by a content hash, while a blog feed needs one item per
    *post* with a stable permalink guid, so subscribers migrating from
    updates.dctech.events see one entry per week rather than per event.
    """
    rss = ET.Element('rss', version='2.0')
    rss.set('xmlns:atom', 'http://www.w3.org/2005/Atom')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = f"{site_name} Updates"
    ET.SubElement(channel, 'link').text = f"{base_url}/updates/"
    ET.SubElement(channel, 'description').text = (
        "Weekly roundups of technology events in and around Washington, DC"
    )
    ET.SubElement(channel, 'language').text = 'en-us'
    ET.SubElement(channel, 'lastBuildDate').text = formatdate(
        timeval=datetime.now(pytz.UTC).timestamp(), usegmt=True)
    atom_link = ET.SubElement(channel, 'atom:link')
    atom_link.set('href', f"{base_url}/updates/feed.xml")
    atom_link.set('rel', 'self')
    atom_link.set('type', 'application/rss+xml')

    for post in posts[:50]:
        item = ET.SubElement(channel, 'item')
        link = f"{base_url}{post['url']}"
        ET.SubElement(item, 'title').text = post['title']
        ET.SubElement(item, 'link').text = link

        if post.get('kind') == 'post':
            # Free-form posts carry their own rendered prose; the Markdown is
            # already HTML by this point, so ship it as the whole item body.
            body = [post.get('body_html', '')]
        else:
            body = [f"<p>{summarize(post)}</p>"]
            body.append(
                f'<p><a href="{base_url}{post["week_url"]}">'
                f'See the full week on {site_name}</a></p>'
            )
            listing = []
            for event in post.get('events', []):
                title = event.get('title')
                if not title:
                    continue
                event_url = event.get('url')
                label = f'<a href="{event_url}">{title}</a>' if event_url else title
                when = event.get('date', '')
                listing.append(f"<li>{when} — {label}</li>")
            if listing:
                body.append("<ul>" + ''.join(listing) + "</ul>")
        ET.SubElement(item, 'description').text = ''.join(body)

        guid = ET.SubElement(item, 'guid')
        guid.set('isPermaLink', 'true')
        guid.text = link

        # Using the post's own date keeps pubDate stable across rebuilds
        # instead of drifting to "now" on every site regeneration.
        pub_dt = local_tz.localize(
            datetime.combine(post['published_on'], time(hour=11)))
        ET.SubElement(item, 'pubDate').text = formatdate(
            timeval=pub_dt.astimezone(pytz.UTC).timestamp(), usegmt=True)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space='  ')
    output = io.BytesIO()
    tree.write(output, encoding='utf-8', xml_declaration=True)
    return Response(output.getvalue(), mimetype='application/rss+xml')


def _generate_rss_feed(events, feed_title, feed_description, feed_link):
    cfg = get_config()
    base_url = cfg.get('base_url', '')

    rss = ET.Element('rss', version='2.0')
    rss.set('xmlns:atom', 'http://www.w3.org/2005/Atom')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = feed_title
    ET.SubElement(channel, 'link').text = feed_link
    ET.SubElement(channel, 'description').text = feed_description
    ET.SubElement(channel, 'language').text = 'en-us'
    now_rfc822 = formatdate(timeval=datetime.now(pytz.UTC).timestamp(), usegmt=True)
    ET.SubElement(channel, 'lastBuildDate').text = now_rfc822

    for event in events[:50]:
        item = ET.SubElement(channel, 'item')
        title = event.get('title', 'Untitled Event')
        event_date = event.get('date', '')
        event_time = event.get('time', '')
        url = event.get('url', '')

        if event_date:
            try:
                date_obj = datetime.strptime(event_date, '%Y-%m-%d').date()
                full_title = f"{title} ({date_obj.strftime('%B %-d, %Y')})"
            except (ValueError, AttributeError):
                full_title = title
        else:
            full_title = title

        ET.SubElement(item, 'title').text = full_title
        ET.SubElement(item, 'link').text = url if url else base_url

        desc_parts = []
        if event_date:
            try:
                date_obj = datetime.strptime(event_date, '%Y-%m-%d').date()
                date_str = date_obj.strftime('%B %-d, %Y')
                desc_parts.append(f"Event Date: {date_str}" + (f" at {event_time}" if event_time else ""))
            except (ValueError, AttributeError):
                pass
        if url:
            desc_parts.append(f'<a href="{url}">Event Details</a>')
        ET.SubElement(item, 'description').text = '<br/>'.join(desc_parts) if desc_parts else title

        guid_hash = hashlib.md5(f"{event_date}-{event_time}-{title}-{url}".encode('utf-8'), usedforsecurity=False).hexdigest()
        guid = ET.SubElement(item, 'guid')
        guid.set('isPermaLink', 'false')
        guid.text = guid_hash

        if event_date:
            try:
                date_obj = datetime.strptime(event_date, '%Y-%m-%d')
                local_dt = local_tz.localize(date_obj)
                ET.SubElement(item, 'pubDate').text = formatdate(timeval=local_dt.astimezone(pytz.UTC).timestamp(), usegmt=True)
            except (ValueError, AttributeError):
                pass

    tree = ET.ElementTree(rss)
    ET.indent(tree, space='  ')
    output = io.BytesIO()
    tree.write(output, encoding='utf-8', xml_declaration=True)
    return Response(output.getvalue(), mimetype='application/rss+xml')
