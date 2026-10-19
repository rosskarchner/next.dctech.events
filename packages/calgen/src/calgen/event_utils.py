import hashlib
import re
import unicodedata


def _normalize_title(s: str) -> str:
    """Strip invisible/control Unicode characters and collapse whitespace.

    Feeds sometimes embed zero-width spaces, joiners, BOMs, or soft hyphens
    in titles; two titles differing only by those should compare equal.

    Applied at ingest (calendars.py) so everything downstream — guid hashing,
    skip_phrases matching, duplicate detection, display — sees a clean title.
    calculate_event_hash below is left untouched: it hashes whatever title it
    is handed, which is already normalized for feed-sourced events.
    """
    cleaned = ''.join(
        ch for ch in s
        if unicodedata.category(ch) not in ('Cf', 'Cc') and ch != '­'
    )
    return re.sub(r'\s+', ' ', cleaned).strip()


def has_specific_time(raw):
    """True if `raw` names an actual time of day, not calgen's all-day sentinel.

    db.py's put_event/update_event/etc. all fall back to storing the literal
    string '00:00' for events with no time (so GSI1SK/GSI5SK sort keys always
    have a TIME# component) — see cdk_next/lambda_src/api/db.py. Every display
    path that parses `event['time']` as a clock time must treat '00:00' the
    same as '' or missing, or an all-day event renders as "12:00 am".
    """
    return bool(raw) and isinstance(raw, str) and ':' in raw and raw.strip() != '00:00'


def matches_skip_phrases(event, skip_phrases):
    """True if a group's `skip_phrases` exclude this feed event.

    Case-insensitive substring match against the (normalized) title and any
    feed keywords. Shared by pipeline.process_events, which drops matches at
    render, and the iCal aggregator, which never materializes them as EVENT#
    rows — the two must agree, or an event the site hides still lands in the
    QA and re-check queues.
    """
    if not isinstance(skip_phrases, list):
        return False  # unset, or a YAML-era bare true/false
    phrases = [p.lower() for p in skip_phrases if isinstance(p, str) and p.strip()]
    if not phrases:
        return False
    title = _normalize_title(event.get('title', '') or '').lower()
    keywords = ' '.join(str(k) for k in (event.get('keywords') or [])).lower()
    return any(p in title or p in keywords for p in phrases)


def calculate_event_hash(date, time, title, url=None):
    uid_parts = [date, time, title]
    if url:
        uid_parts.append(url)
    uid_base = '-'.join(str(p) for p in uid_parts)
    return hashlib.md5(uid_base.encode('utf-8'), usedforsecurity=False).hexdigest()


def slugify(text, max_length=60):
    """Lowercase ASCII slug: 'DC Python: Lightning Talks!' -> 'dc-python-lightning-talks'."""
    ascii_text = (unicodedata.normalize('NFKD', str(text or ''))
                  .encode('ascii', 'ignore').decode('ascii'))
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')
    if len(slug) > max_length:
        # Cut on a word boundary so the URL does not end mid-word.
        slug = slug[:max_length].rsplit('-', 1)[0]
    return slug


def event_slug(event):
    """Stable, readable URL slug for one event's own page.

    Shape: {date}-{title}-{guid[:8]}, e.g.
    '2026-08-14-dc-python-monthly-meetup-a1b2c3d4'.

    The date leads so the URL reads chronologically and sorts usefully. The
    guid prefix trails because the readable part alone is not unique — two
    groups run "Monthly Meetup" on the same night — and because the guid is
    what already identifies an event everywhere else in the pipeline, so the
    slug stays stable for exactly as long as the event's identity does.
    """
    guid = str(event.get('guid') or '')
    if not guid:
        guid = calculate_event_hash(
            event.get('date', ''), event.get('time', ''),
            event.get('title', ''), event.get('url'),
        )
    parts = [str(event.get('date', '')), slugify(event.get('title', ''))]
    return '-'.join(p for p in parts if p) + '-' + guid[:8]
