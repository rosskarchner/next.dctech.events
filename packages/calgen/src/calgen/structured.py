"""Machine-readable views of an event: schema.org values and the public JSON shape.

Three consumers read events without a human in the loop: search engines (the
JSON-LD on each page), agents and scripts (the /…/events.json slices), and
the sitemap's change detection. Each needs the same few facts said precisely,
so the conversions live here once instead of in three templates.
"""
import re
from datetime import datetime

import pytz

from calgen.event_utils import event_slug, has_specific_time
from calgen.location_utils import extract_location_info
from calgen.site_config import get_config


def _site_tz():
    return pytz.timezone(get_config().get('timezone', 'US/Eastern'))


# config.yaml uses pytz's legacy alias; published data should name the
# canonical IANA zone, which every client library recognises.
_LEGACY_TZ = {'US/Eastern': 'America/New_York', 'US/Central': 'America/Chicago',
              'US/Mountain': 'America/Denver', 'US/Pacific': 'America/Los_Angeles'}


def iana_timezone():
    name = get_config().get('timezone', 'US/Eastern')
    return _LEGACY_TZ.get(name, name)


def _time_for(event_time, date_str):
    """A recurring event can carry a per-date dict of times; pick this date's."""
    if isinstance(event_time, dict):
        return event_time.get(date_str, '') or ''
    return event_time or ''


def iso_local(date_str, time_str=''):
    """schema.org/ISO-8601 start or end: a bare date, or a datetime with offset.

    Every time in the data is wall-clock time in the site's timezone with no
    offset attached. Emitting '2026-12-01T17:30' leaves a reader to guess the
    zone (Google flags it; an agent may assume UTC), so a timed value gets the
    offset that applies on that date, DST included. An all-day or untimed
    event stays a bare date, which schema.org accepts and which claims nothing
    the feed did not say.
    """
    date_str = str(date_str or '')
    time_str = _time_for(time_str, date_str)
    if not date_str or not has_specific_time(time_str):
        return date_str
    try:
        naive = datetime.strptime(f'{date_str} {time_str.strip()}', '%Y-%m-%d %H:%M')
    except ValueError:
        return date_str
    return _site_tz().localize(naive).isoformat(timespec='seconds')


def event_start_iso(event):
    return iso_local(event.get('date'), event.get('time'))


def event_end_iso(event):
    """Only when the data states an end; never invented from the start."""
    end_date = event.get('end_date')
    if not end_date:
        return ''
    end_time = event.get('end_time')
    if isinstance(end_time, str) and ':' in end_time:
        return iso_local(end_date, end_time)
    return str(end_date)


# A street address starts with a house number: "1340 U St NW", "3100 Clarendon
# Blvd. #800", "42604 Trade West Drive". A leading number followed by a word,
# not a bare zip or a year.
_STREET_RE = re.compile(r'^\d+[A-Za-z]?(?:-\d+)?\s+[A-Za-z0-9]')
# Pieces that continue a street line rather than start a new field.
_UNIT_RE = re.compile(
    r'^(?:suite|ste\.?|unit|#|floor|fl\.?|room|rm\.?|\d+(?:st|nd|rd|th)\s+floor)\b',
    re.I)
_POSTAL_RE = re.compile(r'\b(\d{5})(?:-\d{4})?\b')


def split_place(location):
    """Split a free-text location into venue name, street line and postal code.

    Feeds hand us one string ("Sudhouse DC, 1340 U St NW, Washington, DC
    20009, USA"), and the JSON-LD used to put all of it in streetAddress. The
    venue goes in Place.name and only the street line in streetAddress, which
    is what map and local results key on. Anything that doesn't parse falls
    back to the whole string, as before: a wrong split is worse than no split.

    Returns {'name', 'street', 'postal_code'}; name/street may be None.
    """
    text = str(location or '').strip()
    if not text:
        return {'name': None, 'street': None, 'postal_code': None}
    parts, seen = [], set()
    for raw in text.split(','):
        part = raw.strip()
        # Meetup repeats the address and the "City, ST" tail; keep the first.
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            parts.append(part)

    street_idx = next((i for i, p in enumerate(parts) if _STREET_RE.match(p)), None)
    if street_idx is None:
        return {'name': text, 'street': None, 'postal_code': _postal(parts)}

    street_parts = [parts[street_idx]]
    for part in parts[street_idx + 1:]:
        if not _UNIT_RE.match(part):
            break
        street_parts.append(part)
    name = ', '.join(parts[:street_idx]) or None
    return {
        'name': name,
        'street': ', '.join(street_parts),
        'postal_code': _postal(parts[street_idx + len(street_parts):]),
    }


def _postal(parts):
    for part in parts:
        match = _POSTAL_RE.search(part)
        if match:
            return match.group(1)
    return None


# ── Public JSON ───────────────────────────────────────────────────────────────
#
# The shape of every /…/events.json slice. It is a published interface, so add
# fields freely but never rename or drop one. The site-wide /events.json keeps
# its older, internal shape for the tools already reading it.

PUBLIC_SCHEMA_VERSION = 1


def public_event(event, base_url=''):
    date_str = event.get('date') or ''
    time_str = _time_for(event.get('time'), date_str)
    city, state = extract_location_info(event.get('location', '') or '')
    is_virtual = event.get('location_type') == 'virtual'
    return {
        'title': event.get('title') or '',
        'start': event_start_iso(event),
        'end': event_end_iso(event) or None,
        'all_day': not has_specific_time(time_str),
        'url': event.get('url') or None,
        'page': f"{base_url}/events/{event_slug(event)}/",
        'group': event.get('group') or None,
        'group_website': event.get('group_website') or None,
        'location': event.get('location') or None,
        'location_type': 'virtual' if is_virtual else (event.get('location_type') or 'physical'),
        'city': city or None,
        'state': state or None,
        'region': event.get('region') or None,
        'categories': list(event.get('categories') or []),
        'cost': event.get('cost') or None,
        'recurring': bool(event.get('is_recurring') or event.get('recurring_slug')),
    }


def _sort_key(event):
    return (event.get('date') or '', _time_for(event.get('time'), event.get('date') or ''),
            event.get('title') or '')


def public_events_document(events, *, title, page, base_url):
    """One slice: the events plus enough context to use them unattended."""
    return {
        'schema_version': PUBLIC_SCHEMA_VERSION,
        'title': title,
        'page': f"{base_url}{page}",
        'timezone': iana_timezone(),
        'about': f"{base_url}/llms.txt",
        'count': len(events),
        'events': [public_event(e, base_url) for e in sorted(events, key=_sort_key)],
    }
