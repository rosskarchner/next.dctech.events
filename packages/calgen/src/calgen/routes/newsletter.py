"""Newsletter HTML/text — same event window and stats as _newsletter_context
builds, just a different template and content-type per route."""
from datetime import datetime, timedelta

from flask import render_template

from calgen.site_config import get_config
from calgen.routes.common import (
    local_tz, UPCOMING_WINDOW_DAYS, get_events, get_stats, get_upcoming_months,
    get_categories_with_event_counts, filter_events_to_upcoming_days,
    prepare_events_by_day, is_virtual_event,
)


def prepare_newsletter_titles(days):
    for day in days:
        for time_slot in day['time_slots']:
            for event in time_slot['events']:
                base_title = event.get('display_title', event.get('title', 'Untitled Event'))
                event['newsletter_title'] = f"Virtual: {base_title}" if is_virtual_event(event) else base_title
    return days


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


def register_routes(app):
    @app.route("/newsletter.html")
    def newsletter_html():
        return render_template('newsletter.html', **_newsletter_context())

    @app.route("/newsletter.txt")
    def newsletter_text():
        response = render_template('newsletter.txt', **_newsletter_context())
        return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}
