"""Time-window listing views: homepage, virtual-only, week, month, and the
just-added freshness feed."""
import calendar
from datetime import date, datetime, timedelta

from flask import render_template

from calgen.site_config import get_config
from calgen.archive import get_archived_events, get_archived_week, merge_events
from calgen.added_at import group_by_added_date
from calgen.routes.common import (
    JUST_ADDED_DAYS, UPCOMING_WINDOW_DAYS, local_tz,
    get_events, get_stats, get_upcoming_months, get_categories_with_event_counts,
    get_all_months, get_sidebar_data, filter_events_to_upcoming_days,
    filter_events_by_month, is_virtual_event, prepare_events_by_day,
)

RECENTLY_ADDED_WINDOW_DAYS = 7


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


def get_iso_week_dates(year, week):
    jan4 = date(year, 1, 4)
    week_1_monday = jan4 - timedelta(days=jan4.weekday())
    week_start = week_1_monday + timedelta(weeks=week - 1)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def parse_week_identifier(week_id):
    parts = week_id.split('-W')
    return int(parts[0]), int(parts[1])


def _format_added_day(day):
    try:
        return datetime.strptime(day, '%Y-%m-%d').date().strftime('%B %-d, %Y')
    except (ValueError, TypeError):
        return day


def get_recently_added_count(window_days=RECENTLY_ADDED_WINDOW_DAYS):
    """How many events were added in the last N days."""
    cutoff = (datetime.now(local_tz).date() - timedelta(days=window_days - 1)).isoformat()
    count = 0
    for day in group_by_added_date(get_events()):
        if day['date'] < cutoff:
            break
        count += day['count']
    return count


def register_routes(app):
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
                               "Nothing has been recorded as newly added yet.",
                               sidebar=get_sidebar_data())
