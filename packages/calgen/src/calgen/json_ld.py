"""schema.org Event JSON-LD parsing, shared by calgen and the MCP server.

Kept to bs4 + stdlib on purpose: build_lambdas.sh copies this file into the MCP
Lambda (whose check_event_source re-reads event pages for the QC agent's
two-week re-check) without installing the rest of calgen. Both sides must agree
on what a page says — the re-check writes its result into the same JSON-LD cache
the aggregator reads — so the rules live here once.
"""
import json
from datetime import datetime, timezone

# Hosts whose event pages fetch_json_ld_data reads. Anything else gets no page
# fetch in the aggregator, so a cache entry for it would never be read.
JSON_LD_HOSTS = ('meetup.com', 'luma.com', 'lu.ma')

# Meetup emits a more specific schema.org subtype instead of plain "Event"
# depending on how the organizer categorized it (e.g. "The Art of the Chart",
# a workshop, came back as ScreeningEvent — next_dctech_events online-flag
# miss). These are schema.org's documented subtypes of Event.
SCHEMA_ORG_EVENT_TYPES = frozenset({
    'Event',
    'BusinessEvent',
    'ChildrensEvent',
    'ComedyEvent',
    'CourseInstance',
    'DanceEvent',
    'DeliveryEvent',
    'EducationEvent',
    'ExhibitionEvent',
    'Festival',
    'FoodEvent',
    'Hackathon',
    'LiteraryEvent',
    'MusicEvent',
    'PublicationEvent',
    'SaleEvent',
    'ScreeningEvent',
    'SocialEvent',
    'SportsEvent',
    'TheaterEvent',
    'VisualArtsEvent',
})


def is_json_ld_host(url):
    return any(host in (url or '') for host in JSON_LD_HOSTS)


def utc_now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_event_json_ld(html):
    """Return what the page's first schema.org Event says, or None if it has none.

    Keys: title, is_virtual, location, cancelled (the four the aggregator has
    always cached) plus event_status, attendance_mode, start_date, end_date,
    which only the re-check reads.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            ld_data = json.loads(script.string or '')
        except json.JSONDecodeError:
            continue
        items = ld_data if isinstance(ld_data, list) else [ld_data]
        for item in items:
            if not isinstance(item, dict) or item.get('@type') not in SCHEMA_ORG_EVENT_TYPES:
                continue
            event_status = item.get('eventStatus', '') or ''
            attendance_mode = item.get('eventAttendanceMode', '') or ''
            result = {
                'title': item.get('name'),
                'is_virtual': False,
                'location': None,
                'cancelled': 'EventCancelled' in event_status,
                'event_status': event_status.rsplit('/', 1)[-1] or None,
                'attendance_mode': attendance_mode.rsplit('/', 1)[-1] or None,
                'start_date': item.get('startDate'),
                'end_date': item.get('endDate'),
            }
            location = item.get('location')
            # Virtual only on the page's say-so: an online attendance mode, or
            # a VirtualLocation and nothing else. A missing location says
            # nothing — AFCEA's chapter pages carry name-only Event blocks for
            # in-person summits (next_dctech_events-ahp).
            places = location if isinstance(location, list) else [location]
            place_types = {p.get('@type') for p in places if isinstance(p, dict)}
            if 'Online' in attendance_mode or place_types == {'VirtualLocation'}:
                result['is_virtual'] = True
            if isinstance(location, list):
                location = next((p for p in location if isinstance(p, dict)
                                 and p.get('@type') != 'VirtualLocation'), None)
            if isinstance(location, dict):
                addr = location.get('address', {})
                if isinstance(addr, dict):
                    parts = [
                        location.get('name', ''),
                        addr.get('streetAddress', ''),
                        addr.get('addressLocality', ''),
                        addr.get('addressRegion', ''),
                    ]
                    result['location'] = ', '.join(p for p in parts if p) or None
                elif isinstance(addr, str) and addr:
                    result['location'] = addr
            return result
    return None


def cache_entry(parsed):
    """The JSON-LD cache's shape for a parse result (None = page had no Event)."""
    parsed = parsed or {}
    return {
        'title': parsed.get('title'),
        'is_virtual': parsed.get('is_virtual', False),
        'location': parsed.get('location'),
        'cancelled': parsed.get('cancelled', False),
        'fetched_at': utc_now_iso(),
    }
