"""Single-event pages: the event detail page and its per-event iCal download."""
from datetime import datetime
from urllib.parse import urlparse

from flask import render_template

from calgen.location_utils import extract_location_info
from calgen.structured import event_end_iso, event_start_iso, split_place
from calgen.site_config import get_config
from calgen.event_utils import has_specific_time
from calgen.routes.common import (get_categories, get_event_by_slug, _generate_ical_feed,
                                  is_virtual_event, local_today)


def _format_event_date(event):
    try:
        return datetime.strptime(event['date'], '%Y-%m-%d').date().strftime('%A, %B %-d, %Y')
    except (KeyError, ValueError, TypeError):
        return event.get('date', '')


def _format_event_time(event):
    raw = event.get('time', '')
    if isinstance(raw, dict):
        raw = raw.get(event.get('date', ''), '')
    if not has_specific_time(raw):
        return ''
    try:
        return datetime.strptime(raw.strip(), '%H:%M').time().strftime('%-I:%M %p').lower()
    except ValueError:
        return ''


def register_routes(app):
    @app.route("/events/<slug>/")
    def event_page(slug):
        event = get_event_by_slug(slug)
        if not event:
            return "Event not found", 404
        cfg = get_config()
        city, state = extract_location_info(event.get('location', '') or '')
        # "Highlight this event" is offered only where the checkout would
        # accept it: in person, not yet happened, not already highlighted.
        highlight_price = str(cfg.get('highlight_price_usd', '') or '').strip()
        can_highlight = (highlight_price not in ('', '0')
                         and not event.get('highlight')
                         and not is_virtual_event(event)
                         and (event.get('date') or '') >= local_today().isoformat())
        return render_template('event_page.html',
                               event=event,
                               can_highlight=can_highlight,
                               highlight_price=highlight_price,
                               slug=slug,
                               city=city,
                               state=state,
                               source_host=urlparse(event.get('url', '') or '').netloc,
                               formatted_date=_format_event_date(event),
                               formatted_time=_format_event_time(event),
                               # schema.org values with the site's UTC offset; see
                               # calgen.structured for why bare times are not enough.
                               start_datetime=event_start_iso(event),
                               end_datetime=event_end_iso(event),
                               place=split_place(event.get('location', '')),
                               categories=get_categories(),
                               base_url=cfg.get('base_url', ''))

    @app.route("/events/<slug>/event.ics")
    def event_ical(slug):
        event = get_event_by_slug(slug)
        if not event:
            return "Event not found", 404
        return _generate_ical_feed([event], event.get('title', 'Event'),
                                   get_config().get('site_name', ''))
