"""
Public corrections to published events, recurring series, and single
occurrences of a recurring series — and their moderation queue.

A correction is a pending proposal against one target — never merged into
the target's live state until a moderator approves it. `target_type`
('event' | 'recurring_series' | 'recurring_instance') decides which one.
Identity for the public half is resolved exactly like a new-event submission
(magic link or Cognito, via routes.submit._resolve_submitter): no new auth
code, same rate-limited /api/submit-link flow, just pointed at a different
form.

All the actual rules (which fields, what counts as valid) live in db.py, next
to the DRAFT#/overlay machinery this mirrors — nothing here reimplements them.
"""

import db
from routes.admin import _admin_check, _json as _admin_json, _post_payload
from routes.responses import json as _json_response
from routes.submit import _parse_body, _resolve_submitter


def _json(status_code, body, event=None):
    return _json_response(status_code, body, event)


def _error(message):
    return {'error': message}


# ─── Public: submit a correction ──────────────────────────────────

def submit_correction_json(event, jinja_env):
    """POST /api/corrections — propose a change to an event, a recurring
    series' definition, or one occurrence of a recurring series.

    Body: {target_type, target_id, fields, reason, [target_date]} plus
    magic-link fields (mlt_e/mlt_t/mlt_s) if not signed in. target_type
    defaults to 'event' and target_id falls back to `guid` if absent, so a
    caller still sending the pre-generalization {guid, fields, reason} shape
    keeps working.
    """
    data = _parse_body(event)
    submitter_email, submitter_id, err = _resolve_submitter(event, data)
    if err:
        return err

    target_type = str(data.get('target_type') or 'event').strip()
    if target_type not in db.CORRECTION_TARGET_TYPES:
        return _json(400, _error(f'Unknown target_type: {target_type}'), event)

    target_id = str(data.get('target_id') or data.get('guid') or '').strip()
    if not target_id:
        return _json(400, _error('target_id is required'), event)

    target_date = str(data.get('target_date') or '').strip() or None
    if target_type == 'recurring_instance' and not target_date:
        return _json(
            400,
            _error('target_date is required for a recurring_instance correction'),
            event,
        )

    if target_type == 'event':
        record = db.get_event_from_config(target_id)
        if not record:
            return _json(404, _error(f'No such event: {target_id}'), event)
        source = record.get('source') or 'manual'
    else:
        record = db.get_recurring_event(target_id)
        if not record:
            return _json(404, _error(f'No such recurring event: {target_id}'), event)
        source = None

    fields = data.get('fields')
    if not isinstance(fields, dict) or not fields:
        return _json(
            400,
            _error('fields must be a non-empty object of {field: new value}'),
            event,
        )

    reason = str(data.get('reason') or '').strip()
    if not reason:
        # Required, same reasoning as the admin overlay endpoint's comment:
        # a moderator cannot act on an unexplained proposed change.
        return _json(
            400,
            _error('Please say what is wrong and where the correct '
                  'information comes from'),
            event,
        )

    try:
        db.check_correction_fields(target_type, fields, source=source)
    except ValueError as exc:
        return _json(422, _error(str(exc)), event)

    correction_id = db.create_correction(
        target_type, target_id, fields, reason, submitter_email, submitter_id,
        target_date=target_date)

    return _json(201, {'correction_id': correction_id}, event)


# ─── Public: read enough of a target to build the form ────────────

_PUBLIC_EVENT_FIELDS = (
    'title', 'date', 'time', 'end_time', 'location', 'url', 'description',
    'source', 'hidden', 'duplicate_of',
)


def get_public_event_json(event, jinja_env, guid):
    """GET /api/public/events/{guid} — the minimum needed to render a
    correction form: current values, and which fields may be corrected.

    Deliberately not a thin wrapper on the admin detail endpoint, which
    requires the admins group and returns overlay bookkeeping the public form
    has no business seeing. Built from effective_event so a submitter
    corrects against what is actually showing, not stale source data — the
    same reason the static site is not embedded with this at build time
    instead (a value baked in at last build could already be wrong by the
    time someone clicks "report an issue").
    """
    record = db.get_event_from_config(guid)
    if not record:
        # Also what a recurring-event occurrence's guid hits if a stale link
        # is used, since occurrences have no EVENT#{guid} row of their own —
        # recurring corrections go through get_public_recurring_json instead.
        return _json(404, _error(f'No such event: {guid}'), event)

    effective = db.effective_event(record)
    source = effective.get('source') or 'manual'
    payload = {field: effective.get(field) for field in _PUBLIC_EVENT_FIELDS}
    payload['guid'] = guid
    payload['correctable_fields'] = list(db.correction_allowed_fields(source))
    return _json(200, payload, event)


_PUBLIC_RECURRING_FIELDS = ('title', 'location', 'url', 'description', 'time')


def get_public_recurring_json(event, jinja_env, slug):
    """GET /api/public/recurring/{slug}?date=YYYY-MM-DD — the minimum needed
    to render a recurring correction form in either mode.

    Always returns the series' own base values (for a recurring_series
    correction). If `date` is supplied, also returns that one occurrence's
    *effective* values — the series merged with any existing per-date
    override — i.e. exactly what's rendering on that occurrence's page
    today, so a submitter corrects against what's actually showing (same
    reasoning as get_public_event_json).

    `date` is accepted as any well-formed YYYY-MM-DD and is NOT required to
    be a currently-in-window rrule occurrence — a correction against a date
    that has since aged out of the site's expansion window must still be
    submittable; it just won't visibly apply until (if ever) that date
    re-enters the window on a future build.
    """
    record = db.get_recurring_event(slug)
    if not record:
        return _json(404, _error(f'No such recurring series: {slug}'), event)

    qs = event.get('queryStringParameters') or {}
    target_date = (qs.get('date') or '').strip() or None

    payload = {
        'slug': slug,
        'correctable_fields': list(db.RECURRING_CORRECTION_FIELDS),
        'series': {f: record.get(f) for f in _PUBLIC_RECURRING_FIELDS},
    }

    if target_date:
        override = db.get_recurring_instance_override(slug, target_date) or {}
        effective = dict(payload['series'])
        effective.update({k: v for k, v in override.items() if not k.startswith('_')})
        payload['instance'] = {
            'date': target_date,
            **{f: effective.get(f) for f in _PUBLIC_RECURRING_FIELDS},
            'has_override': bool(override),
        }

    return _json(200, payload, event)


# ─── Admin: moderation queue ───────────────────────────────────────

def list_corrections_json(event, jinja_env):
    """GET /api/admin/corrections?status=pending."""
    claims, err = _admin_check(event)
    if err:
        return err

    qs = event.get('queryStringParameters') or {}
    status = qs.get('status') or 'pending'
    return _admin_json(200, {'corrections': db.get_corrections_by_status(status)})


def get_correction_json(event, jinja_env, correction_id):
    """GET /api/admin/corrections/{id} — one correction, enriched with the
    target's *current* state (not just the submission-time snapshot), so a
    moderator sees whether it's since been hidden, merged, re-sourced, or
    (for a recurring series) edited out from under the proposal.
    """
    claims, err = _admin_check(event)
    if err:
        return err

    correction = db.get_correction(correction_id)
    if not correction:
        return _admin_json(404, {'error': f'No such correction: {correction_id}'})

    target_type = correction.get('target_type', 'event')
    target_id = correction.get('target_id') or correction.get('target_guid')

    if target_type == 'event':
        target_event = db.get_event_from_config(target_id)
        correction['target_event'] = (
            db.effective_event(target_event) if target_event else None)
    else:
        correction['target_recurring'] = db.get_recurring_event(target_id)
        if target_type == 'recurring_instance':
            correction['target_instance_override'] = db.get_recurring_instance_override(
                target_id, correction.get('target_date'))

    return _admin_json(200, {'correction': correction})


def _resolve_error(exc):
    message = str(exc)
    if 'No such correction' in message or 'no longer exists' in message:
        return _admin_json(404, {'error': message})
    if 'already' in message:
        return _admin_json(409, {'error': message})
    # A field no longer allowed under the target event's current source: the
    # request was well-formed, the world changed under it.
    return _admin_json(422, {'error': message})


def approve_correction_json(event, jinja_env, correction_id):
    """POST /api/admin/corrections/{id}/approve."""
    claims, err = _admin_check(event)
    if err:
        return err

    try:
        result = db.approve_correction(correction_id, claims.get('email', ''))
    except ValueError as exc:
        return _resolve_error(exc)

    return _admin_json(200, result)


def reject_correction_json(event, jinja_env, correction_id):
    """POST /api/admin/corrections/{id}/reject."""
    claims, err = _admin_check(event)
    if err:
        return err

    data = _post_payload(event)
    reason = data.get('reason') or None

    try:
        result = db.reject_correction(correction_id, claims.get('email', ''), reason)
    except ValueError as exc:
        return _resolve_error(exc)

    return _admin_json(200, result)
