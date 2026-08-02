#!/usr/bin/env python3
"""Fetch a group's iCal feed and extract its events.

The iCal Aggregator Lambda calls fetch_ical_and_extract_events directly,
seeding the cache files below from DynamoDB first and persisting the results
back afterwards. There is no site-wide refresh entry point here — the
aggregator owns iteration over groups.
"""
import os
import icalendar
import requests
import json
import pytz
import hashlib
from datetime import datetime, timedelta, timezone
import re
import recurring_ical_events

from calgen.site_config import get_config
from calgen.event_utils import _normalize_title

config = get_config()
timezone_name = config.get('timezone', 'US/Eastern')
local_tz = pytz.timezone(timezone_name)

_site_name = config.get('site_name', 'calgen')
_base_url = config.get('base_url', 'https://example.com')
USER_AGENT = config.get('user_agent', f'{_site_name}/1.0 (+{_base_url})')

DATA_DIR = '_data'
CACHE_DIR = '_cache'
ICAL_CACHE_DIR = os.path.join(CACHE_DIR, 'ical')
JSON_LD_CACHE_DIR = os.path.join(CACHE_DIR, 'json-ld')

# How far ahead to keep events. Feeds routinely publish recurring series
# years out; without a bound the site listed events into 2029.
HORIZON_DAYS = 180


def _ensure_dirs():
    os.makedirs(ICAL_CACHE_DIR, exist_ok=True)
    os.makedirs(JSON_LD_CACHE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def should_fetch(meta_data, group_id):
    last_fetch = meta_data.get('last_fetch')
    if last_fetch:
        last_fetch_time = datetime.fromisoformat(last_fetch)
        if datetime.now(timezone.utc) - last_fetch_time < timedelta(hours=4):
            print(f"Data for {group_id} was fetched less than 4 hours ago, using cached version")
            return False
    return True


def fetch_json_ld_data(url):
    """Fetch JSON-LD metadata from a URL (Meetup-aware)."""
    if "meetup.com" not in url:
        return {'title': None, 'is_virtual': False, 'location': None}

    url_hash = hashlib.md5(url.encode('utf-8'), usedforsecurity=False).hexdigest()
    cache_file = os.path.join(JSON_LD_CACHE_DIR, f"{url_hash}.json")

    if os.path.exists(cache_file):
        cached = json.load(open(cache_file))
        cached.setdefault('location', None)
        if 'cancelled' in cached:
            return cached

    result = {'title': None, 'is_virtual': False, 'location': None, 'cancelled': False}
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    ld_data = json.loads(script.string)
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    for item in items:
                        if item.get('@type') == 'Event':
                            if 'name' in item:
                                result['title'] = item['name']
                            if 'EventCancelled' in item.get('eventStatus', ''):
                                result['cancelled'] = True
                            attendance_mode = item.get('eventAttendanceMode', '')
                            if 'OnlineEventAttendanceMode' in attendance_mode or 'Online' in attendance_mode:
                                result['is_virtual'] = True
                            location = item.get('location')
                            if not location:
                                result['is_virtual'] = True
                            elif isinstance(location, dict):
                                addr = location.get('address', {})
                                if isinstance(addr, dict):
                                    parts = [
                                        location.get('name', ''),
                                        addr.get('streetAddress', ''),
                                        addr.get('addressLocality', ''),
                                        addr.get('addressRegion', ''),
                                    ]
                                    result['location'] = ', '.join(p for p in parts if p)
                                elif isinstance(addr, str) and addr:
                                    result['location'] = addr
                            with open(cache_file, 'w') as f:
                                json.dump(result, f)
                            return result
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Error fetching JSON-LD for {url}: {e}")

    with open(cache_file, 'w') as f:
        json.dump(result, f)
    return result


def fetch_ical_and_extract_events(url, group_id, group=None):
    try:
        print(f"Fetching iCal feed from {url}")
        _ensure_dirs()
        cache_file = os.path.join(ICAL_CACHE_DIR, f"{group_id}.json")
        cache_meta_file = os.path.join(ICAL_CACHE_DIR, f"{group_id}.meta")

        headers = {'User-Agent': USER_AGENT}
        meta_data = {}

        if os.path.exists(cache_meta_file):
            try:
                with open(cache_meta_file, 'r') as f:
                    meta_data = json.load(f)
                if not should_fetch(meta_data, group_id):
                    if os.path.exists(cache_file):
                        with open(cache_file, 'r') as f:
                            return json.load(f)
                    return None
                if 'etag' in meta_data:
                    headers['If-None-Match'] = meta_data['etag']
            except Exception as meta_err:
                print(f"Metadata read error: {meta_err}")

        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 304:
            print(f"  No changes for {group_id}")
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    return json.load(f)
            return None

        if response.status_code != 200:
            print(f"  Error fetching {url}: {response.status_code}")
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    return json.load(f)
            return None

        cal = icalendar.Calendar.from_ical(response.content)
        group_name = group.get('name') if group else group_id
        if group_name is None:
            group_name = str(cal.get('x-wr-calname', '')) or None

        start_dt = datetime.now(timezone.utc)
        end_dt = start_dt + timedelta(days=HORIZON_DAYS)

        events = []

        def _append_event_data(event):
            dtstart = event.get('dtstart').dt
            if isinstance(dtstart, datetime):
                dtstart_utc = dtstart.astimezone(timezone.utc)
                if dtstart_utc < start_dt or dtstart_utc > end_dt:
                    return
                dt_local = dtstart.astimezone(local_tz)
                date_str = dt_local.strftime('%Y-%m-%d')
                time_str = dt_local.strftime('%H:%M')
                if time_str == '00:00':
                    time_str = ''
            else:
                if dtstart < start_dt.date() or dtstart > end_dt.date():
                    return
                date_str = dtstart.strftime('%Y-%m-%d')
                time_str = ''

            dtend = event.get('dtend')
            end_date_str = ''
            end_time_str = ''
            if dtend:
                dtend = dtend.dt
                if isinstance(dtend, datetime):
                    dt_end_local = dtend.astimezone(local_tz)
                    end_date_str = dt_end_local.strftime('%Y-%m-%d')
                    end_time_str = dt_end_local.strftime('%H:%M')
                else:
                    end_date_str = dtend.strftime('%Y-%m-%d')

            title = _normalize_title(str(event.get('summary', 'Untitled Event')))
            url_field = event.get('url')
            if url_field:
                event_url = str(url_field)
            else:
                desc = str(event.get('description', ''))
                url_match = re.search(r'https?://[^\s<>"]+|www\.[^\s<>"]+', desc)
                event_url = url_match.group(0) if url_match else (group.get('website', '') if group else '')

            is_virtual = False
            ical_location = str(event.get('location', ''))
            resolved_location = ical_location
            if event_url:
                ld_data = fetch_json_ld_data(event_url)
                if ld_data.get('cancelled'):
                    print(f"  Skipping cancelled event: {title}")
                    return
                if ld_data.get('title'):
                    title = _normalize_title(ld_data['title'])
                is_virtual = ld_data.get('is_virtual', False)
                if not ical_location and ld_data.get('location'):
                    resolved_location = ld_data['location']

            if not is_virtual:
                virtual_keywords = ['virtual', 'online', 'remote', 'zoom', 'webinar', 'livestream', 'live stream', 'internet']
                is_virtual = any(kw in resolved_location.lower() for kw in virtual_keywords)

            event_data = {
                'title': title,
                'date': date_str,
                'time': time_str,
                'end_date': end_date_str,
                'end_time': end_time_str,
                'url': event_url,
                'location': resolved_location,
                'description': str(event.get('description', '')),
                'group': group_name,
                'group_id': group_id,
                'location_type': 'virtual' if is_virtual else 'physical',
            }

            event_categories = set(group.get('categories', []) if group else [])
            ical_categories = event.get('categories')
            if ical_categories:
                if not isinstance(ical_categories, list):
                    ical_categories = [ical_categories]
                for cat in ical_categories:
                    if hasattr(cat, 'to_ical'):
                        val = cat.to_ical().decode('utf-8')
                        for c in val.split(','):
                            c_clean = c.strip()
                            if c_clean:
                                event_categories.add(c_clean)
                    elif isinstance(cat, str):
                        for c in cat.split(','):
                            c_clean = c.strip()
                            if c_clean:
                                event_categories.add(c_clean)

            event_data['categories'] = list(event_categories)
            events.append(event_data)

        for vevent in cal.walk('VEVENT'):
            if vevent.get('rrule'):
                recurring_cal = icalendar.Calendar()
                recurring_cal.add_component(vevent)
                for expanded_event in recurring_ical_events.of(recurring_cal).between(start_dt, end_dt):
                    _append_event_data(expanded_event)
            else:
                _append_event_data(vevent)

        with open(cache_file, 'w') as f:
            json.dump(events, f)

        meta_data = {
            'last_fetch': datetime.now(timezone.utc).isoformat(),
            'etag': response.headers.get('ETag'),
            'last_modified': response.headers.get('Last-Modified'),
        }
        with open(cache_meta_file, 'w') as f:
            json.dump(meta_data, f)

        return events

    except Exception as e:
        print(f"  Error processing {group_id}: {e}")
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                return json.load(f)
        return None


