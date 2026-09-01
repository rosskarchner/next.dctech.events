from flask import Flask, render_template, Response, send_from_directory
from jinja2 import FileSystemLoader
from datetime import date, datetime, timedelta, time
import os
import json
import calendar
import pytz
import xml.etree.ElementTree as ET  # nosec B405
from email.utils import formatdate
import io

from calgen.site_config import get_config
from calgen.updates import (
    get_all_posts, get_free_post, get_free_posts, get_update_post,
    get_update_posts, summarize,
)
from calgen.archive import (
    get_archived_events, get_archived_week, get_archived_weeks, merge_events,
)
from calgen.added_at import get_added_at_index, added_on, group_by_added_date

from calgen.routes.common import (
    config, local_tz, THIN_FACET_MIN_EVENTS, JUST_ADDED_DAYS,
    UPCOMING_WINDOW_DAYS,
    get_events, get_categories, get_upcoming_months,
    get_categories_with_event_counts, get_all_week_ids, get_all_months,
    get_events_by_slug, get_event_by_slug, get_recently_added,
    get_category_month_counts, get_category_month_combos, get_sidebar_data,
    get_stats, filter_events_to_upcoming_days, filter_events_by_month,
    is_virtual_event, get_week_identifier, prepare_events_by_day,
    _generate_ical_feed, _generate_rss_feed,
)


from calgen.regions import load_region_plugin  # noqa: E402
from calgen.routes import groups as _groups_routes
from calgen.routes import events as _events_routes
from calgen.routes import locations as _locations_routes
from calgen.routes import categories as _categories_routes
from calgen.routes import feeds as _feeds_routes


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
        window_end = datetime.now(local_tz).date() + timedelta(days=UPCOMING_WINDOW_DAYS)
        upcoming_events = filter_events_to_upcoming_days(events, window_end)
        days = prepare_events_by_day(upcoming_events, add_week_links=True, window_end=window_end)

        cfg = get_config()
        base_url = cfg.get('base_url', '')
        stats = get_stats().copy()
        stats['upcoming_events'] = len(upcoming_events)
        stats['total_upcoming_events'] = len(events)

        return render_template('homepage.html',
                               days=days,
                               stats=stats,
                               base_url=base_url,
                               recently_added_count=get_recently_added_count(),
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

        # Live events for the part of the week still ahead, the frozen
        # capture for the part already gone. For a wholly future week the
        # archive adds nothing; for a wholly past one it is all there is.
        archived = get_archived_week(week_id)
        week_events = merge_events(
            filter_events_by_week(get_events(), week_start, week_end),
            filter_events_by_week(archived['events'], week_start, week_end)
            if archived else [],
        )
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
                               is_past=week_end < datetime.now(local_tz).date(),
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

        month_events = merge_events(
            filter_events_by_month(get_events(), first_day, last_day),
            filter_events_by_month(get_archived_events(), first_day, last_day),
        )
        days = prepare_events_by_day(month_events)
        month_name = calendar.month_name[month]
        stats = {'upcoming_events': len(month_events)}

        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        built_months = set(get_all_months())

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
                               # Only months that are actually built get a nav
                               # link; the ends of the range used to point at
                               # pages that were never frozen.
                               has_prev=(prev_year, prev_month) in built_months,
                               has_next=(next_year, next_month) in built_months,
                               is_past=last_day < datetime.now(local_tz).date(),
                               sidebar=get_sidebar_data(active_month=(year, month)))

    @app.route("/just-added/")
    def just_added_page():
        """Events by the day they appeared on the site, newest first.

        A freshness page, and the only view keyed on when a listing was
        recorded rather than when its event happens.
        """
        days = group_by_added_date(get_events(), limit_days=JUST_ADDED_DAYS)
        for day in days:
            day['date_display'] = _format_added_day(day['date'])
        return render_template('just_added.html',
                               days_with_events=days,
                               error_message=None if days else
                               "Nothing has been recorded as newly added yet.")

    def _newsletter_context():
        """Shared setup for newsletter_html/newsletter_text — same event
        window, same stats, only the rendered template and content-type
        differ. Split apart once already (the homepage/newsletter 21-day
        window bug) by one of the two copies getting fixed and the other
        forgotten; a shared helper closes that drift off for good.
        """
        events = get_events()
        window_end = datetime.now(local_tz).date() + timedelta(days=UPCOMING_WINDOW_DAYS)
        upcoming_events = filter_events_to_upcoming_days(events, window_end)
        days = prepare_events_by_day(upcoming_events, window_end=window_end)
        prepare_newsletter_titles(days)
        stats = get_stats().copy()
        stats['upcoming_events'] = len(upcoming_events)
        return {
            'days': days,
            'stats': stats,
            'base_url': get_config().get('base_url', ''),
            'upcoming_months': get_upcoming_months(),
            'categories_with_counts': get_categories_with_event_counts(),
        }

    @app.route("/newsletter.html")
    def newsletter_html():
        return render_template('newsletter.html', **_newsletter_context())

    @app.route("/newsletter.txt")
    def newsletter_text():
        response = render_template('newsletter.txt', **_newsletter_context())
        return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

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


def _format_added_day(day):
    try:
        return datetime.strptime(day, '%Y-%m-%d').date().strftime('%B %-d, %Y')
    except (ValueError, TypeError):
        return day


RECENTLY_ADDED_WINDOW_DAYS = 7


def get_recently_added_count(window_days=RECENTLY_ADDED_WINDOW_DAYS):
    """How many events were added in the last N days — the homepage's one-
    line freshness signal."""
    cutoff = (datetime.now(local_tz).date() - timedelta(days=window_days - 1)).isoformat()
    count = 0
    for day in group_by_added_date(get_events()):
        if day['date'] < cutoff:
            break
        count += day['count']
    return count


# ---------------------------------------------------------------------------
# Event filtering helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------

def get_iso_week_dates(year, week):
    jan4 = date(year, 1, 4)
    week_1_monday = jan4 - timedelta(days=jan4.weekday())
    week_start = week_1_monday + timedelta(weeks=week - 1)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def parse_week_identifier(week_id):
    parts = week_id.split('-W')
    return int(parts[0]), int(parts[1])


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
        elif post.get('kind') == 'link':
            # The item's own link is already the week page, so a "see the full
            # week" link underneath it would point at where the reader is
            # going anyway.
            body = [f"<p>{summarize(post)}</p>"]
        else:
            body = [f"<p>{summarize(post)}</p>"]
            # A roundup of what was *added* spans months, so pointing at one
            # week of the calendar would be a link to the wrong thing.
            if post.get('added_since'):
                body.append(
                    f'<p><a href="{base_url}/">'
                    f'See everything coming up on {site_name}</a></p>'
                )
            else:
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
