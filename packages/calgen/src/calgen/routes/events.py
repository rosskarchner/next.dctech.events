"""Single-event pages: the event detail page and its per-event iCal download."""
from datetime import datetime
from urllib.parse import urlparse

from flask import render_template

from calgen.location_utils import extract_location_info
from calgen.site_config import get_config
from calgen.routes.common import get_categories, get_event_by_slug, _generate_ical_feed


def _format_event_date(event):
    try:
        return datetime.strptime(event['date'], '%Y-%m-%d').date().strftime('%A, %B %-d, %Y')
    except (KeyError, ValueError, TypeError):
        return event.get('date', '')


def _format_event_time(event):
    raw = event.get('time', '')
    if isinstance(raw, dict):
        raw = raw.get(event.get('date', ''), '')
    if not (raw and isinstance(raw, str) and ':' in raw):
        return ''
    try:
        return datetime.strptime(raw.strip(), '%H:%M').time().strftime('%-I:%M %p').lower()
    except ValueError:
        return ''


def _event_end_iso(event):
    """schema.org endDate, only when the data actually supports one.

    Google wants endDate on Event, but inventing one (start + 2h, say) would
    put a fact in the markup that no feed asserted. Emitted only for events
    carrying an explicit end_date; otherwise the property is omitted.
    """
    end_date = event.get('end_date')
    if not end_date:
        return ''
    end_time = event.get('end_time')
    if end_time and isinstance(end_time, str) and ':' in end_time:
        return f"{end_date}T{end_time}"
    return str(end_date)


def register_routes(app):
    @app.route("/events/<slug>/")
    def event_page(slug):
        event = get_event_by_slug(slug)
        if not event:
            return "Event not found", 404
        cfg = get_config()
        city, state = extract_location_info(event.get('location', '') or '')
        return render_template('event_page.html',
                               event=event,
                               slug=slug,
                               city=city,
                               state=state,
                               source_host=urlparse(event.get('url', '') or '').netloc,
                               formatted_date=_format_event_date(event),
                               formatted_time=_format_event_time(event),
                               end_datetime=_event_end_iso(event),
                               categories=get_categories(),
                               base_url=cfg.get('base_url', ''))

    @app.route("/events/<slug>/event.ics")
    def event_ical(slug):
        event = get_event_by_slug(slug)
        if not event:
            return "Event not found", 404
        return _generate_ical_feed([event], event.get('title', 'Event'),
                                   get_config().get('site_name', ''))
