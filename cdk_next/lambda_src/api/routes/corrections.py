"""
Public corrections to published events, and their moderation queue.

A correction is a pending proposal against one existing event — never merged
into the event's live overlay until a moderator approves it. Identity for the
public half is resolved exactly like a new-event submission (magic link or
Cognito, via routes.submit._resolve_submitter): no new auth code, same
rate-limited /api/submit-link flow, just pointed at a different form.

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
    """POST /api/corrections — propose a change to an existing event."""
    data = _parse_body(event)
    submitter_email, submitter_id, err = _resolve_submitter(event, data)
    if err:
        return err

    guid = str(data.get('guid') or '').strip()
    if not guid:
        return _json(400, _error('guid is required'), event)

    record = db.get_event_from_config(guid)
    if not record:
        return _json(404, _error(f'No such event: {guid}'), event)

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

    source = record.get('source') or 'manual'
    try:
        db.check_correction_fields(source, fields)
    except ValueError as exc:
        return _json(422, _error(str(exc)), event)

    correction_id = db.create_correction(
        guid, fields, reason, submitter_email, submitter_id)

    return _json(201, {'correction_id': correction_id}, event)


# ─── Public: read enough of an event to build the form ────────────

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
        # Also what a recurring-event occurrence's guid hits, since those
        # have no EVENT#{guid} row — the correction form's caller turns this
        # specific 404 into "this event can't currently accept corrections
        # here" rather than a generic error.
        return _json(404, _error(f'No such event: {guid}'), event)

    effective = db.effective_event(record)
    source = effective.get('source') or 'manual'
    payload = {field: effective.get(field) for field in _PUBLIC_EVENT_FIELDS}
    payload['guid'] = guid
    payload['correctable_fields'] = list(db.correction_allowed_fields(source))
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
    target event's *current* state (not just the submission-time snapshot),
    so a moderator sees whether it's since been hidden, merged, or re-sourced.
    """
    claims, err = _admin_check(event)
    if err:
        return err

    correction = db.get_correction(correction_id)
    if not correction:
        return _admin_json(404, {'error': f'No such correction: {correction_id}'})

    target_event = db.get_event_from_config(correction['target_guid'])
    correction['target_event'] = (
        db.effective_event(target_event) if target_event else None)
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
