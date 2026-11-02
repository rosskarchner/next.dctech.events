"""Match highlighted events (exported from DynamoDB) to this build's events.

The export writes `_data/highlights.json`: every active HIGHLIGHT# row, with
the target it was bought for and a snapshot of the event at the time. Events
move under a highlight's feet — iCal guids hash date, time, title and url, so
a reschedule or a retitled listing gets a new guid, and a duplicate can be
merged into a different parent — so matching falls back in order:

  1. a recurring occurrence: series slug + date (its guid is recomputed every
     build, so it is never stored);
  2. the guid it was bought for, then the guid the last build matched;
  3. the listing that absorbed it as a duplicate (same url and date, or
     credited in also_published_by on its date);
  4. the only upcoming event from the same group with the same url, then the
     only one with the same group and normalized title.

A match sets event['highlight'] = {'kind', 'key', 'label'}; templates read
that. Every highlight's outcome goes to `_data/highlight_matches.json`
(key -> guid or null), which the build's reconcile step stores back in
DynamoDB and uses to notice an event that has gone missing.
"""
import json
import os
import re
import unicodedata

LABELS = {'paid': 'Paid', 'editor': "Editor's pick"}


def _norm_title(title):
    title = unicodedata.normalize('NFKC', str(title or '')).lower()
    return re.sub(r'[^a-z0-9]+', ' ', title).strip()


def _norm_url(url):
    url = str(url or '').strip().lower()
    url = re.sub(r'^https?://(www\.)?', '', url)
    return url.split('#', 1)[0].rstrip('/')


def load_highlights(data_dir='_data'):
    path = os.path.join(data_dir, 'highlights.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
    except (OSError, ValueError) as exc:
        print(f'Ignoring unreadable {path}: {exc}')
        return []
    return [r for r in rows if isinstance(r, dict) and r.get('target_key')]


def _only(candidates):
    return candidates[0] if len(candidates) == 1 else None


def find_match(highlight, events, today):
    """The event `highlight` belongs to in this build, or None."""
    visible = [e for e in events if not e.get('hidden')]
    snap_date = highlight.get('date')

    slug = highlight.get('recurring_slug')
    if slug:
        # An occurrence is its series on its date; nothing else identifies it.
        return _only([e for e in visible if e.get('recurring_slug') == slug
                      and e.get('date') == snap_date])

    by_guid = {e.get('guid'): e for e in visible if e.get('guid')}
    for guid in (highlight.get('guid'), highlight.get('resolved_guid')):
        if guid and guid in by_guid:
            return by_guid[guid]

    url = _norm_url(highlight.get('url'))
    group = highlight.get('group') or ''

    # Merged into another listing as a duplicate.
    if url and snap_date:
        hit = _only([e for e in visible if e.get('date') == snap_date
                     and _norm_url(e.get('url')) == url])
        if hit:
            return hit
    if group and snap_date:
        hit = _only([e for e in visible if e.get('date') == snap_date
                     and any(c.get('group') == group
                             for c in e.get('also_published_by') or [])])
        if hit:
            return hit

    # Rescheduled (a new date means a new guid).
    if group:
        upcoming = [e for e in visible if (e.get('date') or '') >= today
                    and e.get('group') == group]
        if url:
            hit = _only([e for e in upcoming if _norm_url(e.get('url')) == url])
            if hit:
                return hit
        title = _norm_title(highlight.get('title'))
        if title:
            hit = _only([e for e in upcoming if _norm_title(e.get('title')) == title])
            if hit:
                return hit
    return None


def apply_highlights(events, highlights, today):
    """Mark matched events in place. Returns {target_key: guid or None}."""
    matches = {}
    for h in highlights:
        key = h['target_key']
        event = find_match(h, events, today)
        if event is None or event.get('highlight'):
            # Two highlights resolving to one listing (a duplicate merged
            # into an already-highlighted parent): the first keeps it.
            matches[key] = None if event is None else event.get('guid')
            continue
        kind = h.get('kind') if h.get('kind') in LABELS else 'paid'
        event['highlight'] = {'kind': kind, 'key': key, 'label': LABELS[kind]}
        matches[key] = event.get('guid')
    return matches


def run(events, today, data_dir='_data'):
    """Pipeline step: load, match, and write highlight_matches.json."""
    highlights = load_highlights(data_dir)
    matches = apply_highlights(events, highlights, today)
    with open(os.path.join(data_dir, 'highlight_matches.json'), 'w',
              encoding='utf-8') as f:
        json.dump(matches, f, indent=1, sort_keys=True)
    if highlights:
        found = sum(1 for g in matches.values() if g)
        print(f'Highlights: {found} of {len(highlights)} matched')
    return matches
