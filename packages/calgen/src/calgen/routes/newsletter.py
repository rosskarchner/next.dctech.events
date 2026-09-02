"""Newsletter HTML/text — same event window and stats as _newsletter_context
builds, just a different template and content-type per route."""
from datetime import datetime, timedelta

from flask import render_template, request

from calgen.site_config import get_config
from calgen.routes.common import (
    local_tz, UPCOMING_WINDOW_DAYS, get_events, get_stats, get_upcoming_months,
    get_categories_with_event_counts, filter_events_to_upcoming_days,
    prepare_events_by_day, is_virtual_event,
)

# Rendered literally into the newsletter's output, then string-replaced
# post-render with each subscriber's own signed link — the sender sends the
# same rendered bytes to a whole group of subscribers who share a
# (categories, regions) preference signature, but the manage-preferences
# link is unique per subscriber, so it can't be baked in at render time the
# way everything else in the template is.
PREFERENCES_LINK_PLACEHOLDER = '__PREFERENCES_LINK__'


def prepare_newsletter_titles(days):
    for day in days:
        for time_slot in day['time_slots']:
            for event in time_slot['events']:
                event['newsletter_title'] = event.get('display_title', event.get('title', 'Untitled Event'))
    return days


def _filter_events(events, category_slugs, region_slugs):
    """category_slugs/region_slugs: None or empty means unfiltered on that
    axis."""
    if not category_slugs and not region_slugs:
        return events

    def _matches(event):
        if category_slugs and not any(
                c in (event.get('categories') or []) for c in category_slugs):
            return False
        if region_slugs and event.get('region') not in region_slugs:
            return False
        return True

    return [e for e in events if _matches(e)]


def _newsletter_context(category_slugs=None, region_slugs=None):
    """Shared setup for newsletter_html/newsletter_text — same event
    window, same stats, only the rendered template and content-type
    differ. Split apart once already (the homepage/newsletter 21-day
    window bug) by one of the two copies getting fixed and the other
    forgotten; a shared helper closes that drift off for good.

    category_slugs/region_slugs: optional per-subscriber filters (see
    _filter_events) — omitted, this is the exact unfiltered (aside from
    virtual events, always dropped — see below) behavior the live public
    /newsletter.html page has always had.
    """
    events = get_events()
    # Virtual events are excluded from the newsletter unconditionally, not
    # as a subscriber preference — email is a poor fit for "join from
    # anywhere" listings the way region/category actually narrow real
    # differences in what a reader wants to hear about.
    events = [e for e in events if not is_virtual_event(e)]
    events = _filter_events(events, category_slugs, region_slugs)
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
        'preferences_link_placeholder': PREFERENCES_LINK_PLACEHOLDER,
    }


def _slugs_from_query(param):
    raw = request.args.get(param, '')
    return [s for s in raw.split(',') if s] or None


def register_routes(app):
    @app.route("/newsletter.html")
    def newsletter_html():
        ctx = _newsletter_context(
            _slugs_from_query('categories'), _slugs_from_query('regions'))
        return render_template('newsletter.html', **ctx)

    @app.route("/newsletter.txt")
    def newsletter_text():
        ctx = _newsletter_context(
            _slugs_from_query('categories'), _slugs_from_query('regions'))
        response = render_template('newsletter.txt', **ctx)
        return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}
