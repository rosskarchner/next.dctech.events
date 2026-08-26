"""Frozen per-week listings — the site's only memory of what has happened.

`get_events()` and the pipeline both drop anything dated before today, and
organizers' feeds drop past events too, so once a week is over there is no
way to reconstruct what the calendar showed for it. The updates publisher
writes an `ARCHIVE#<week_id>` row for the current and coming week on every
run, merging into whatever is already stored rather than replacing it; the
site exporter lands those in `_archive/`.

This module is a pure read of those files — it never invents a week. Two
consequences worth knowing:

* Coverage starts when archiving started. Weeks before that are simply gone,
  and no code here pretends otherwise.
* Because captures accumulate, a week is only as complete as the last merge
  before it ended — an event added and held on the same day could slip
  through. In practice a week is merged at least twice while it is still
  upcoming and once while it is under way.

Merging with live data is *also* the caller's job — see `merge_events` —
because for the current week and month both sources are partly right: the
archive holds the days already past, live data holds the rest.
"""
import os

import yaml

ARCHIVE_DIR = '_archive'


def _load_week(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not data.get('week_id'):
        return None
    events = data.get('events') or []
    for event in events:
        # Marks these as coming from a snapshot rather than the live set.
        # prepare_events_by_day uses it to withhold the /events/ permalink:
        # per-event pages are only built for current events, so linking one
        # from an archived listing would be a dead internal link.
        event['archived'] = True
    return {
        'week_id': data['week_id'],
        'week_start': data.get('week_start'),
        'week_end': data.get('week_end'),
        'captured_at': data.get('captured_at'),
        'event_count': data.get('event_count', len(events)),
        'events': events,
    }


def get_archived_weeks():
    """{week_id: week} for every captured week, or {} when none exist."""
    weeks = {}
    if not os.path.isdir(ARCHIVE_DIR):
        return weeks
    for filename in sorted(os.listdir(ARCHIVE_DIR)):
        if not filename.endswith('.yaml'):
            continue
        week = _load_week(os.path.join(ARCHIVE_DIR, filename))
        if week:
            weeks[week['week_id']] = week
    return weeks


def get_archived_week(week_id):
    return get_archived_weeks().get(week_id)


def get_archived_events():
    """Every archived event, across all weeks."""
    events = []
    for week in get_archived_weeks().values():
        events.extend(week['events'])
    return events


def get_archived_event_count():
    return sum(week['event_count'] for week in get_archived_weeks().values())


def _identity(event):
    """What makes two listings the same event.

    Deliberately not the guid: archived events carry only the snapshot
    fields, and a guid is not among them. These four are, and they are what
    calculate_event_hash already hashes.
    """
    return (event.get('date'), event.get('time') or '',
            event.get('title'), event.get('url'))


def merge_events(live, archived):
    """Live events plus archived ones that live no longer covers.

    Live wins on conflict: for a week or month still partly in the future,
    the live entry is current and the snapshot may be up to a week stale.
    """
    merged = list(live)
    seen = {_identity(e) for e in merged}
    for event in archived:
        key = _identity(event)
        if key not in seen:
            seen.add(key)
            merged.append(event)
    return merged
