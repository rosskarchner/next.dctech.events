"""Applying an overlay at render time.

An overlay is a per-event override written by a moderator in /edit or by the
weekly QC agent, exported to `_overlay/{guid}.yaml`. This module is the render
side of it; the write side lives in the api Lambda's db.py, and the two are
deliberately separate because they answer different questions.

db.py asks "which fields may a caller write" and keeps an allowlist, because the
edit form is generated from it. This module asks "which keys must never clobber
an event's identity", and keeps a denylist, because it also has to be defensive
about `_overlay/` YAML it did not write.

Identity is source-derived; presentation is overlay-derived. The pipeline
computes an event's guid from the *feed's* title, time and url and only then
merges the overlay, so overriding a title changes what renders without changing
what the event is. Overriding `date` is refused outright — the date is how the
pipeline decides whether an event is still upcoming at all.
"""

# Set by the pipeline from the source feed. db.py keeps an identical set for the
# write side; if these two ever disagree, the looser one wins and the stricter
# one becomes a lie.
OVERLAY_PROTECTED_FIELDS = frozenset({
    'group', 'group_id', 'group_website', 'date', 'end_date', 'guid', 'source',
})


def apply_overlay(event, overlay):
    """Merge one overlay onto one event, in place. Returns the event.

    Underscore keys are the write side's private bookkeeping (`_comment`,
    `_qa_run`, `_rev`, `_edited_by`, `_edited_at`, `_field_edits`). The exporter
    already strips them, but it and this module are two independently deployed
    halves, so skipping them here as well is what stops a stale exporter from
    painting `_qa_run` onto a rendered event.
    """
    for key, value in (overlay or {}).items():
        if key.startswith('_') or key in OVERLAY_PROTECTED_FIELDS:
            continue
        event[key] = value
    return event
