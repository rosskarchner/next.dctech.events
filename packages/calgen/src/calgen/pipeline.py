#!/usr/bin/env python3
"""
Data pipeline: reads groups, iCal caches, single events, and recurring events,
then writes _data/all_events.json.
"""
import os
import yaml
import icalendar
import pytz
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import sys
import calendar as cal_module
import dateparser
import json
import requests
from dateutil import rrule

from calgen.site_config import get_config
from calgen.event_utils import calculate_event_hash, _normalize_title
from calgen.overlay import apply_overlay
from calgen.regions import EventRejected, load_region_plugin

config = get_config()
timezone_name = config.get('timezone', 'US/Eastern')
local_tz = pytz.timezone(timezone_name)

DATA_DIR = '_data'
CACHE_DIR = '_cache'
ICAL_CACHE_DIR = os.path.join(CACHE_DIR, 'ical')
GROUPS_DIR = '_groups'
CATEGORIES_DIR = '_categories'
SINGLE_EVENTS_DIR = '_single_events'
OVERLAY_DIR = '_overlay'
RECURRING_EVENTS_DIR = '_recurring_events'

RECURRING_MAX_FUTURE_DAYS = 90


def _apply_region(event, region_plugin):
    """Set event['region'] via the plugin. Return False if the event should be rejected."""
    if not region_plugin:
        return True
    try:
        event['region'] = region_plugin.location_to_region(event.get('location', '') or '')
        return True
    except EventRejected:
        return False


def are_events_duplicates(e1, e2):
    url1, url2 = e1.get('url'), e2.get('url')
    if url1 and url1 == url2 and e1.get('date') == e2.get('date'):
        return True
    t1 = _normalize_title(e1.get('title') or '').lower()
    t2 = _normalize_title(e2.get('title') or '').lower()
    return (t1 == t2 and
            e1.get('date') == e2.get('date') and
            e1.get('time') == e2.get('time'))


def _credit_duplicate(parent, event):
    """Record who else published this event, if they are actually named.

    Manual and recurring events carry no group, so crediting them rendered
    a literal "Also published by None" on the site. A group-less duplicate
    still gets merged away — it just contributes no attribution.
    """
    group = event.get('group')
    if not group:
        return
    credits = parent.setdefault('also_published_by', [])
    if any(c.get('group') == group for c in credits):
        return
    credits.append({
        'group': group,
        'group_website': event.get('group_website'),
    })


def remove_duplicates(events):
    guid_index = {}
    for i, e in enumerate(events):
        if e.get('guid'):
            guid_index[e['guid']] = i

    result = []
    seen_indices = set()
    explicit_children = set()

    for i, event in enumerate(events):
        dup_of = event.get('duplicate_of')
        if dup_of and dup_of in guid_index:
            parent = events[guid_index[dup_of]]
            _credit_duplicate(parent, event)
            explicit_children.add(i)

    for i, event in enumerate(events):
        if i in explicit_children or i in seen_indices:
            continue
        duplicate_found = False
        for j in range(len(result)):
            if are_events_duplicates(result[j], event):
                _credit_duplicate(result[j], event)
                duplicate_found = True
                break
        if not duplicate_found:
            result.append(event)
        seen_indices.add(i)
    return result


def get_groups():
    groups = []
    if not os.path.exists(GROUPS_DIR):
        return groups
    for filename in os.listdir(GROUPS_DIR):
        if filename.endswith('.yaml'):
            slug = filename[:-5]
            with open(os.path.join(GROUPS_DIR, filename), 'r') as f:
                group = yaml.safe_load(f)
                group['id'] = slug
                groups.append(group)
    return groups


def get_categories():
    categories = {}
    if not os.path.exists(CATEGORIES_DIR):
        return categories
    for filename in os.listdir(CATEGORIES_DIR):
        if filename.endswith('.yaml'):
            slug = filename[:-5]
            with open(os.path.join(CATEGORIES_DIR, filename), 'r') as f:
                categories[slug] = yaml.safe_load(f)
                categories[slug]['slug'] = slug
    return categories


def load_single_events():
    events = []
    if not os.path.exists(SINGLE_EVENTS_DIR):
        return events
    for filename in os.listdir(SINGLE_EVENTS_DIR):
        if not filename.endswith('.yaml'):
            continue
        filepath = os.path.join(SINGLE_EVENTS_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                event = yaml.safe_load(f)
            if not event:
                continue
            event['id'] = os.path.splitext(filename)[0]
            event['source'] = 'manual'
            if 'date' in event:
                if isinstance(event['date'], date):
                    event['date'] = event['date'].strftime('%Y-%m-%d')
                else:
                    parsed = dateparser.parse(str(event['date']), settings={
                        'TIMEZONE': timezone_name,
                        'DATE_ORDER': 'YMD',
                        'PREFER_DATES_FROM': 'future',
                    })
                    if parsed:
                        event['date'] = parsed.strftime('%Y-%m-%d')
            # DynamoDB owns event identity. A submitted event is keyed
            # EVENT#<8 hex> there while this hash is 32 chars, so recomputing
            # unconditionally meant _overlay/{guid}.yaml could never match one
            # and an overlay on a submission silently did nothing
            # (next_dctech_events-p8o). Only compute a guid when the exporter
            # did not supply one.
            if not event.get('guid'):
                event['guid'] = calculate_event_hash(
                    event.get('date', ''),
                    event.get('time', ''),
                    event.get('title', ''),
                    event.get('url'),
                )
            events.append(event)
        except Exception as e:
            print(f"Error loading single event {filename}: {e}")
    return events


def load_overlays():
    overlays = {}
    if not os.path.exists(OVERLAY_DIR):
        return overlays
    for filename in os.listdir(OVERLAY_DIR):
        if not filename.endswith('.yaml'):
            continue
        guid = os.path.splitext(filename)[0]
        filepath = os.path.join(OVERLAY_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data:
                overlays[guid] = data
        except Exception as e:
            print(f"Error loading overlay {filepath}: {e}")
    return overlays


def load_recurring_events():
    events = []
    if not os.path.exists(RECURRING_EVENTS_DIR):
        return events
    for filename in os.listdir(RECURRING_EVENTS_DIR):
        if not filename.endswith('.yaml'):
            continue
        filepath = os.path.join(RECURRING_EVENTS_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                event = yaml.safe_load(f)
            if not event:
                continue
            event['id'] = os.path.splitext(filename)[0]
            event['source'] = 'recurring'
            event['is_recurring'] = True
            if 'date' in event:
                if isinstance(event['date'], date):
                    event['date'] = event['date'].strftime('%Y-%m-%d')
                else:
                    parsed = dateparser.parse(str(event['date']), settings={
                        'TIMEZONE': timezone_name,
                        'DATE_ORDER': 'YMD',
                        'PREFER_DATES_FROM': 'future',
                    })
                    if parsed:
                        event['date'] = parsed.strftime('%Y-%m-%d')
            events.append(event)
        except Exception as e:
            print(f"Error loading recurring event {filename}: {e}")
    return events


def expand_recurring_events(recurring_events, today=None, max_days=RECURRING_MAX_FUTURE_DAYS):
    if today is None:
        today = datetime.now(local_tz).date()
    expanded = []
    max_date = today + timedelta(days=max_days)
    for event in recurring_events:
        rrule_str = event.get('rrule', '')
        if not rrule_str:
            continue
        try:
            start_date = datetime.strptime(event['date'], '%Y-%m-%d').date()
        except (KeyError, ValueError):
            continue
        if start_date > max_date:
            continue
        try:
            start_dt = local_tz.localize(datetime.combine(start_date, datetime.min.time()))
            rule = rrule.rrulestr(rrule_str, dtstart=start_dt)
            for occurrence_dt in rule:
                occurrence_date = occurrence_dt.date()
                if occurrence_date > max_date:
                    break
                if occurrence_date < today:
                    continue
                instance = dict(event)
                instance['date'] = occurrence_date.strftime('%Y-%m-%d')
                instance['guid'] = calculate_event_hash(
                    instance['date'],
                    instance.get('time', ''),
                    instance['title'],
                    instance.get('url'),
                )
                expanded.append(instance)
        except Exception as e:
            print(f"Error expanding recurring event {event.get('id', 'unknown')}: {e}")
    return expanded


def process_events(groups, categories, single_events, ical_events, recurring_events,
                   today=None, event_overrides=None, region_plugin=None):
    if today is None:
        today = datetime.now(local_tz).date()
    if event_overrides is None:
        event_overrides = {}

    regular_events = []
    submitted_events = []

    for group in groups:
        if not group.get('active', True):
            continue
        group_id = group.get('id', '')
        suppress_urls = group.get('suppress_urls') or []
        if suppress_urls is True or suppress_urls is False:
            suppress_urls = []
        skip_phrases = group.get('skip_phrases') or []
        if skip_phrases is True or skip_phrases is False:
            skip_phrases = []
        skip_phrases_lower = [p.lower() for p in skip_phrases if isinstance(p, str)]
        for event in ical_events.get(group_id, []):
            if event.get('url', '') in suppress_urls:
                continue
            if skip_phrases_lower:
                title_lower = _normalize_title(event.get('title', '') or '').lower()
                keywords = event.get('keywords') or []
                keywords_lower = ' '.join(str(k) for k in keywords).lower()
                if any(phrase in title_lower or phrase in keywords_lower
                       for phrase in skip_phrases_lower):
                    continue
            try:
                event_date = datetime.strptime(event['date'], '%Y-%m-%d').date()
            except (KeyError, ValueError):
                continue
            if event_date < today:
                continue
            processed = dict(event)
            processed['group'] = group.get('name', group_id)
            processed['group_website'] = group.get('website', '')
            processed['source'] = 'ical'
            processed['categories'] = list(group.get('categories', []))
            processed['guid'] = calculate_event_hash(
                event.get('date', ''), event.get('time', ''),
                event.get('title', ''), event.get('url'),
            )
            # Overlay before region: `location` is overlay-editable and the
            # region is derived from it.
            apply_overlay(processed, event_overrides.get(processed['guid']))
            if not _apply_region(processed, region_plugin):
                continue
            regular_events.append(processed)

    for event in single_events:
        try:
            event_date = datetime.strptime(event['date'], '%Y-%m-%d').date()
        except (KeyError, ValueError):
            continue
        if event_date < today:
            continue
        processed = dict(event)
        processed.setdefault('source', 'manual')
        if 'guid' not in processed:
            processed['guid'] = calculate_event_hash(
                event.get('date', ''), event.get('time', ''),
                event.get('title', ''), event.get('url'),
            )
        apply_overlay(processed, event_overrides.get(processed['guid']))
        if not _apply_region(processed, region_plugin):
            continue
        submitted_events.append(processed)

    # Occurrences have no EVENT# row of their own, so nothing can write an
    # overlay keyed to one today. Applied anyway so that "an overlay applies
    # whatever the source" is literally true if occurrences are ever
    # materialised, rather than being a fourth special case to discover later.
    recurring_expanded = []
    for occurrence in expand_recurring_events(recurring_events, today=today):
        apply_overlay(occurrence, event_overrides.get(occurrence.get('guid')))
        if _apply_region(occurrence, region_plugin):
            recurring_expanded.append(occurrence)

    combined = recurring_expanded + submitted_events + regular_events
    # Dedup over visible events only. remove_duplicates keeps whichever of a
    # pair it reaches first and merges the other away, and app.get_events
    # filters `hidden` afterwards — so a hidden event reached first would
    # absorb its visible twin and both listings would disappear. Hidden events
    # stay in the output because get_events(include_hidden=True) is a real
    # caller; they are just not eligible to swallow anything.
    visible = [e for e in combined if not e.get('hidden')]
    unique_events = remove_duplicates(visible)
    unique_events += [e for e in combined if e.get('hidden')]

    def _sort_time(event):
        t = event.get('time', '')
        if isinstance(t, dict):
            return t.get(event.get('date', ''), '') or ''
        return t or ''

    unique_events.sort(key=lambda x: (x.get('date', ''), _sort_time(x)))
    return unique_events


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    groups = get_groups()
    categories = get_categories()
    single_events = load_single_events()
    recurring_events = load_recurring_events()
    event_overrides = load_overlays()

    site_dir = os.environ.get('CALGEN_SITE_DIR', os.getcwd())
    region_plugin = load_region_plugin(site_dir)

    ical_events = {}
    for group in groups:
        group_id = group['id']
        cache_file = os.path.join(ICAL_CACHE_DIR, f"{group_id}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    ical_events[group_id] = json.load(f)
            except Exception as e:
                print(f"Error reading cache {cache_file}: {e}")

    unique_events = process_events(
        groups, categories, single_events, ical_events,
        recurring_events, event_overrides=event_overrides, region_plugin=region_plugin,
    )

    with open(os.path.join(DATA_DIR, 'all_events.json'), 'w') as f:
        json.dump(unique_events, f, indent=2)

    print(f"Generated data for {len(unique_events)} events")


if __name__ == "__main__":
    main()
