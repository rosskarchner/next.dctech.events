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


def calculate_event_hash(date, time, title, url=None):
    uid_parts = [date, time, title]
    if url:
        uid_parts.append(url)
    uid_base = '-'.join(str(p) for p in uid_parts)
    return hashlib.md5(uid_base.encode('utf-8'), usedforsecurity=False).hexdigest()
