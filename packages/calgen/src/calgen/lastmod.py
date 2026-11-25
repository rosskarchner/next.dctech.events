"""An honest <lastmod> for event pages, carried from one build to the next.

Nothing in the event data says when a listing last *changed*. `createdAt` is
when it was first recorded, and the table's `updated_at` is stamped on every
rewrite, changed or not (see the site-generator trigger's NON_SEMANTIC_ATTRS).
A sitemap that stamps build time on every URL teaches crawlers to ignore the
field, which is why sitemap.xml long emitted no lastmod for events at all.

So the build remembers. `calgen lastmod` fingerprints the fields an event page
actually renders, compares them with the previous build's state file, and
keeps the old date for anything unchanged. Only a real change (or a new page)
gets today's date. The state rides along with the published site as
`/static/lastmod.json` so the next build can fetch it back; the buildspec does
that with `aws s3 cp` before running this step.

When there is no previous state (the first build, or the fetch failed), an
event gets its `createdAt` day if known and no lastmod otherwise. An absent
lastmod is honest; a guessed one is not.
"""
import hashlib
import json
import os

from calgen.added_at import added_on, get_added_at_index
from calgen.event_utils import event_slug

STATE_FILE = os.path.join('_data', 'event_lastmod.json')
PREVIOUS_STATE_FILE = os.path.join('_data', 'previous_event_lastmod.json')
CHANGED_FILE = os.path.join('_data', 'changed_event_slugs.json')
# Under static/ so Frozen-Flask copies it into the published site.
PUBLISHED_STATE_PATH = os.path.join('static', 'lastmod.json')

STATE_VERSION = 1

# What event_page.html shows. The description is deliberately absent: the
# page never republishes it, so a feed rewording it changes nothing a crawler
# would see.
_RENDERED_FIELDS = ('title', 'date', 'time', 'end_date', 'end_time', 'url',
                    'location', 'location_type', 'cost', 'group',
                    'group_website', 'categories')


def fingerprint(event):
    rendered = {k: event.get(k) for k in _RENDERED_FIELDS}
    if isinstance(rendered.get('categories'), list):
        rendered['categories'] = sorted(rendered['categories'])
    blob = json.dumps(rendered, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def compute_state(events, previous, today, added_index=({}, {})):
    """New state {slug: {'h': fingerprint, 'lastmod': 'YYYY-MM-DD' | None}}
    plus the slugs whose page is new or changed since `previous`.

    `previous` is the last build's state, or None when there is none to
    compare with. `today` is the build date in the site's timezone.
    """
    by_guid, by_title = added_index
    state, changed = {}, []
    for event in events:
        slug = event_slug(event)
        h = fingerprint(event)
        created = added_on(event, by_guid, by_title)
        old = (previous or {}).get(slug)
        if old and old.get('h') == h:
            lastmod = old.get('lastmod')
        elif previous is None:
            # Nothing to compare with: we cannot tell an edit from no edit.
            lastmod = created
        else:
            lastmod = today
            changed.append(slug)
        # Never date a page before the event was recorded, nor after today.
        if lastmod and created and lastmod < created:
            lastmod = created
        if lastmod and lastmod > today:
            lastmod = today
        state[slug] = {'h': h, 'lastmod': lastmod}
    return state, sorted(changed)


def load_previous(path=PREVIOUS_STATE_FILE):
    """The previous build's events map, or None if absent or unreadable."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
        return None
    events = data.get('events')
    return events if isinstance(events, dict) else None


def write_state(state, changed, today):
    doc = {'version': STATE_VERSION, 'built_on': today, 'events': state}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    for path in (STATE_FILE, PUBLISHED_STATE_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, separators=(',', ':'), sort_keys=True)
    with open(CHANGED_FILE, 'w', encoding='utf-8') as f:
        json.dump(changed, f)


def get_event_lastmods():
    """{slug: 'YYYY-MM-DD'} from this build's state; {} if the step has not run."""
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {slug: entry.get('lastmod')
            for slug, entry in (data.get('events') or {}).items()
            if isinstance(entry, dict) and entry.get('lastmod')}


def get_changed_slugs():
    try:
        with open(CHANGED_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def run(events, today):
    """Compute and write this build's state. Returns (state, changed)."""
    state, changed = compute_state(events, load_previous(), today, get_added_at_index())
    write_state(state, changed, today)
    return state, changed
