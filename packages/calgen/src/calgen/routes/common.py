"""Shared data-fetching/filtering/formatting layer behind every route module.

Most of what calgen's routes need is not resource-specific — get_events,
get_categories, prepare_events_by_day (the single most-used helper, called
from 9 different routes across 5 resource areas) and friends are each used
by 2+ resource modules, so they live here rather than being duplicated or
tangled in a web of resource-module-to-resource-module imports. Matches the
rule already established in cdk_next/lambda_src/api/db.py's docstring for
promote_draft: shared/business logic lives where every consumer can reach
it without duplication; route modules stay thin.

One more reason some of this lives here specifically: anything freeze.py
imports directly (get_category_month_combos, get_all_week_ids,
get_upcoming_weeks — each has zero-or-one call sites among the route
functions themselves) has to live here regardless of how cross-cutting it
is among routes, or freeze.py's import line breaks. Don't "clean up" one of
these into a resource module just because it looks single-purpose.

Two known-dead members carried over unchanged from app.py's original
layout, not deleted here (that's a separate cleanup, out of scope for a
pure reorganization): filter_events_by_location, VALID_US_STATES.
"""
import calendar
import hashlib
import io
import json
import os
import yaml
import pytz
from datetime import date, datetime, timedelta
from email.utils import formatdate
from urllib.parse import urlparse
import xml.etree.ElementTree as ET  # nosec B405

from flask import Response
from icalendar import Calendar, Event as ICalEvent

from calgen.added_at import group_by_added_date
from calgen.archive import get_archived_events, get_archived_weeks
from calgen.event_utils import calculate_event_hash, event_slug
from calgen.location_utils import extract_location_info
from calgen.site_config import get_config

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

# The browse sidebar crosses every category with every month, and each combo
# is frozen as its own page. Below this many events a combo has nothing its
# parent /categories/<slug>/ page does not already say, so it ships noindex
# and stays out of the sitemap rather than competing with that parent.
THIN_FACET_MIN_EVENTS = 3

# How many distinct days /just-added/ shows. The page is a freshness signal,
# not an archive — older additions are already findable by date, category and
# location, so an unbounded list would just grow duplicate listings.
JUST_ADDED_DAYS = 30

# How many of the newest additions the homepage teases.
RECENTLY_ADDED_PREVIEW = 5

# How far out the homepage and newsletter look. A multi-day event starting
# inside this window still gets clipped to it in prepare_events_by_day, so
# raising this doesn't let a long-running listing bleed further past the edge.
UPCOMING_WINDOW_DAYS = 21


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

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


def get_all_week_ids(num_weeks=12):
    """Week pages worth building: the upcoming ones plus every archived one.

    Without the archive half, a week page vanishes the moment its events do
    and the URL starts 404ing — the site deploys with `s3 sync --delete`, so
    the previously published file is actively removed.
    """
    return sorted(set(get_upcoming_weeks(num_weeks)) | set(get_archived_weeks()))


def get_all_months():
    """Months worth building: those with upcoming events plus archived ones."""
    months = {(m['year'], m['month']) for m in get_upcoming_months()}
    for event in get_archived_events():
        try:
            event_date = datetime.strptime(event['date'], '%Y-%m-%d').date()
        except (KeyError, ValueError, TypeError):
            continue
        months.add((event_date.year, event_date.month))
    return sorted(months)


def get_events_by_slug():
    """{slug: event} for every current event, for the per-event pages.

    Collisions are impossible in practice (the slug carries a guid prefix)
    but a feed that publishes the same event twice would produce the same
    guid, so first-wins keeps the page deterministic.
    """
    index = {}
    for event in get_events():
        index.setdefault(event_slug(event), event)
    return index


def get_event_by_slug(slug):
    return get_events_by_slug().get(slug)


def get_recently_added(limit=RECENTLY_ADDED_PREVIEW):
    """The newest additions, flattened — the homepage's one-line teaser."""
    events = []
    for day in group_by_added_date(get_events(), limit_days=limit):
        for event in day['events']:
            events.append(event)
            if len(events) >= limit:
                return events
    return events


def get_category_month_counts():
    """Return {(slug, year, month): event_count} for every non-empty combo."""
    events = get_events()
    categories = get_categories()
    counts = {}
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
                key = (slug, event_date.year, event_date.month)
                counts[key] = counts.get(key, 0) + 1
    return counts


def get_category_month_combos():
    """Return sorted (slug, year, month) tuples that have at least one event.

    Used to enumerate the category+month pages worth freezing.
    """
    return sorted(get_category_month_counts())


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

def filter_events_to_upcoming_days(events, window_end):
    """Keep events starting between today and `window_end`, inclusive.

    Only the start date is checked here — a multi-day event that starts
    within the window but ends after it is still included. prepare_events_by_day
    clips its day-by-day expansion to the same `window_end` so it doesn't
    render past the edge this filter implies.
    """
    today = datetime.now(local_tz).date()
    return [
        e for e in events
        if e.get('date') and today <= datetime.strptime(e['date'], '%Y-%m-%d').date() <= window_end
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

def get_week_identifier(target_date):
    iso_year, iso_week, _ = target_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


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

def prepare_events_by_day(events, add_week_links=False, window_end=None):
    """Group events by the day(s) they occur on.

    A multi-day event is expanded into one entry per day it spans. When
    `window_end` is given, that expansion is clipped to it — otherwise a
    long-running event whose start date passed an upstream "next N days"
    filter would keep appearing on days well past that window
    (next_dctech_events bug: TIX ON POSH bleeding past the homepage's
    two-week cutoff).
    """
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
        if window_end is not None and end_date is not None and end_date > window_end:
            end_date = window_end
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
            # Permalink to this event's own page. Every list template renders
            # it as a low-prominence '#' beside the title, never in place of
            # the outbound link: the title always goes straight to the
            # organizer. See site/templates/event_page.html for the rest of
            # the reasoning.
            if not event.get('archived'):
                event_copy['permalink'] = f"/events/{event_slug(event)}/"

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
