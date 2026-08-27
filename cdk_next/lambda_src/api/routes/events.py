"""Admin event routes — the HTTP half of the human QA surface.

Until now the only thing that could write an overlay was the MCP server, which
is IAM-authed for agents. These routes give a moderator with a Cognito session
the same writer, so a fix applied in /edit is byte-for-byte a fix the weekly QC
agent would have applied. All the rules live in db.py; nothing here reimplements
them.

Separate from admin.py, which is already 546 lines of drafts, posts, subscribers
and trusted submitters — a different entity with a different lifecycle.

Two things worth knowing before reading further.

`description` is deliberately absent from the list response. A single event's
description runs to several kilobytes and there are hundreds of them; the QA
workflow never edits one. `has_description` says whether to offer a link.

The overlay is split into `overlay` (what renders, writable) and `overlay_meta`
(bookkeeping, read-only) rather than passed through raw. That is the same view
the MCP tools present, so the two admin surfaces cannot drift, and it means the
browser cannot round-trip a forged `_qa_run` into a write and have anything
trust it.
"""
import db
from routes.admin import _admin_check, _json, _post_payload

# What the table renders per row, and what a merge has to be checked against.
_EFFECTIVE_FIELDS = ('title', 'location', 'categories', 'hidden', 'duplicate_of')

# Straight from the record. `overrides` is reshaped, `description` dropped.
_ROW_FIELDS = (
    'guid', 'title', 'date', 'time', 'end_date', 'end_time', 'location',
    'location_type', 'city', 'state', 'all_day', 'cost', 'url', 'source',
    'group', 'group_id', 'group_website', 'categories', 'slug',
    'hidden', 'duplicate_of', 'review_status', 'createdAt',
)

_LIST_CAP = 2000
_BULK_CAP = 200

_BULK_ACTIONS = ('hide', 'unhide', 'add_category', 'combine', 'unmerge',
                 'set_review_status')


def _effective(event):
    merged = db.effective_event(event)
    return {field: merged.get(field) for field in _EFFECTIVE_FIELDS}


def _duplicate_state(target, by_guid):
    """Why a merge is or is not doing what its author intended.

    calgen only honours a `duplicate_of` whose target is in the corpus, and it
    credits the parent before the hidden filter runs — so there are three ways
    for a merge to look done and not be.
    """
    if not target:
        return 'none'
    other = by_guid.get(target)
    if other is None:
        # Renders normally, so the duplicate is still on the calendar.
        return 'dangling'
    if other['hidden']:
        # Child dropped as a duplicate, parent dropped as hidden: both gone.
        return 'into-hidden'
    if other['duplicate_of']:
        return 'chain'
    return 'ok'


def _row(event, by_guid=None):
    overrides = event.get('overrides') or {}
    effective = _effective(event)
    row = {field: db._to_plain(event.get(field)) for field in _ROW_FIELDS}
    row['has_description'] = bool(event.get('description'))
    row['overlay'] = db.public_overlay(overrides)
    row['overlay_meta'] = db.overlay_bookkeeping(overrides)
    row['effective'] = effective
    if by_guid is not None:
        row['duplicate_state'] = _duplicate_state(
            effective.get('duplicate_of'), by_guid)
    return row


def _matches(row, params):
    source = params.get('source')
    if source and row.get('source') != source:
        return False

    group_id = params.get('group_id')
    if group_id and row.get('group_id') != group_id:
        return False

    query = (params.get('q') or '').strip().casefold()
    if query:
        haystack = ' '.join(str(row['effective'].get(f) or '')
                            for f in ('title', 'location'))
        haystack += ' ' + str(row.get('group') or '')
        if query not in haystack.casefold():
            return False

    state = params.get('state') or 'any'
    effective = row['effective']
    overlaid = bool(row['overlay'])
    problem = row.get('duplicate_state') in ('dangling', 'into-hidden', 'chain')
    checks = {
        'any': True,
        'visible': not effective['hidden'] and not effective['duplicate_of'],
        'hidden': bool(effective['hidden']),
        'duplicate': bool(effective['duplicate_of']),
        'overlaid': overlaid,
        # `_edited_by` is the precise signal; a run stamp catches overlays
        # written before authorship was recorded. Deliberately not `comment`,
        # which a hand edit writes too.
        'agent': bool(row['overlay_meta']['run_id']
                      or str(row['overlay_meta']['edited_by']).startswith('agent:')),
        'uncategorized': not effective['categories'],
        'problems': problem,
    }
    return checks.get(state, True)


def list_events_json(event, jinja_env):
    """GET /api/admin/events — the QA table's one request.

    Filters: month (YYYY-MM), include_past, source, review_status, group_id,
    q, state, limit. `review_status` alone goes through GSI5, which is much
    cheaper than a GSI4 sweep and — more to the point — has no date bound, so
    it returns past-dated events still sitting in the queue.
    """
    claims, err = _admin_check(event)
    if err:
        return err

    params = event.get('queryStringParameters') or {}
    month = (params.get('month') or '').strip() or None
    review_status = (params.get('review_status') or '').strip() or None
    include_past = str(params.get('include_past') or '') in ('1', 'true', 'yes')

    if review_status:
        if review_status not in db.REVIEW_STATUSES:
            return _json(400, {'error': f'Unknown review_status: {review_status}',
                               'valid': list(db.REVIEW_STATUSES)})
        events = db.get_events_by_review_status(review_status)
        mode = 'review_status'
    else:
        try:
            events = db.get_all_events(date_prefix=month,
                                       include_past=include_past)
        except (ValueError, AttributeError):
            return _json(400, {'error': "month must look like 'YYYY-MM'"})
        mode = 'all'

    # Effective values for the whole corpus first: _duplicate_state has to ask
    # about events that may themselves be filtered out of the response.
    by_guid = {e['guid']: _effective(e) for e in events if e.get('guid')}
    rows = [_row(e, by_guid) for e in events]
    rows = [r for r in rows if _matches(r, params)]

    try:
        limit = min(int(params.get('limit') or _LIST_CAP), _LIST_CAP)
    except (TypeError, ValueError):
        limit = _LIST_CAP

    return _json(200, {
        'events': rows[:limit],
        'count': len(rows),
        'truncated': len(rows) > limit,
        'mode': mode,
        'editable_fields': list(db.OVERLAY_EDITABLE_FIELDS),
    })


def get_event_json(event, jinja_env, guid):
    """GET /api/admin/events/{guid} — one event, with everything to edit it."""
    claims, err = _admin_check(event)
    if err:
        return err

    record = db.get_event_from_config(guid)
    if not record:
        return _json(404, {'error': f'No such event: {guid}'})

    row = _row(record)
    row['description'] = db._to_plain(record.get('description') or '')

    target = row['effective'].get('duplicate_of')
    if target:
        canonical = db.get_event_from_config(target)
        row['duplicate_of_event'] = None if not canonical else {
            'guid': target,
            'title': canonical.get('title', ''),
            'date': canonical.get('date', ''),
            'group': canonical.get('group', ''),
        }

    return _json(200, {
        'event': row,
        # The form is generated from this, so the UI can never offer a field
        # the write path would reject.
        'editable_fields': list(db.OVERLAY_EDITABLE_FIELDS),
    })


def _expected_rev(data):
    """Absent means "merge"; present — even as null — means "check"."""
    return data['expected_rev'] if 'expected_rev' in data else db._UNSET


def _conflict(exc):
    return _json(409, {
        'error': str(exc),
        'current': {'rev': exc.current_rev, 'overlay': exc.current_overlay},
    })


def put_overlay_json(event, jinja_env, guid):
    """PUT /api/admin/events/{guid}/overlay."""
    claims, err = _admin_check(event)
    if err:
        return err

    data = _post_payload(event)
    fields = data.get('fields')
    if not isinstance(fields, dict):
        fields = {}
    clear = data.get('clear') or ()
    if not isinstance(clear, (list, tuple)):
        return _json(400, {'error': 'clear must be a list of field names'})
    if not fields and not clear:
        return _json(400, {'error': 'Nothing to change'})

    comment = str(data.get('comment') or '').strip()
    if not comment:
        # Required, not optional: the digest and the UI both key off _comment,
        # and an unexplained overlay is the thing a reviewer cannot act on.
        return _json(400, {'error': 'A comment is required, saying what was '
                                    'wrong and where each new value came from'})

    try:
        result = db.set_event_overlay(
            guid, fields, comment,
            actor=claims.get('email', ''),
            clear=tuple(clear),
            expected_rev=_expected_rev(data),
        )
    except db.OverlayConflict as exc:
        return _conflict(exc)
    except ValueError as exc:
        message = str(exc)
        if message.startswith('No such event:'):
            return _json(404, {'error': message})
        # Unknown category, dangling or self-referential duplicate_of: the
        # request was well-formed, the values were not.
        if 'category' in message or 'merge into' in message or 'duplicate' in message:
            return _json(422, {'error': message})
        return _json(400, {'error': message})

    return _json(200, result)


def delete_overlay_json(event, jinja_env, guid):
    """DELETE /api/admin/events/{guid}/overlay — back to what the source says."""
    claims, err = _admin_check(event)
    if err:
        return err

    params = event.get('queryStringParameters') or {}
    expected = db._UNSET
    if 'expected_rev' in params:
        raw = params['expected_rev']
        expected = None if raw in ('', 'null', None) else int(raw)

    try:
        result = db.clear_event_overlay(
            guid, actor=claims.get('email', ''), expected_rev=expected)
    except db.OverlayConflict as exc:
        return _conflict(exc)
    except ValueError as exc:
        return _json(404, {'error': str(exc)})

    return _json(200, result)


def put_review_status_json(event, jinja_env, guid):
    """PUT /api/admin/events/{guid}/review-status — clear the QA queue by hand."""
    claims, err = _admin_check(event)
    if err:
        return err

    status = str(_post_payload(event).get('review_status') or '').strip()
    if status not in db.REVIEW_STATUSES:
        return _json(400, {'error': f'review_status must be one of '
                                    f'{list(db.REVIEW_STATUSES)}'})

    if not db.set_event_review_status(guid, status):
        return _json(404, {'error': f'No such event: {guid}'})
    return _json(200, {'guid': guid, 'review_status': status})


def bulk_json(event, jinja_env):
    """POST /api/admin/events/bulk.

    Always 200 with a per-guid result, never a partial 500: twenty-two hides
    where two failed must not read as total failure.
    """
    claims, err = _admin_check(event)
    if err:
        return err

    data = _post_payload(event)
    action = str(data.get('action') or '').strip()
    if action not in _BULK_ACTIONS:
        return _json(400, {'error': f'action must be one of '
                                    f'{list(_BULK_ACTIONS)}'})

    guids = data.get('guids') or []
    if not isinstance(guids, list) or not guids:
        return _json(400, {'error': 'guids must be a non-empty list'})
    if len(guids) > _BULK_CAP:
        return _json(400, {'error': f'At most {_BULK_CAP} events at a time '
                                    f'({len(guids)} given)'})

    actor = claims.get('email', '')
    comment = str(data.get('comment') or '').strip()

    if action == 'set_review_status':
        status = str(data.get('review_status') or '').strip()
        if status not in db.REVIEW_STATUSES:
            return _json(400, {'error': f'review_status must be one of '
                                        f'{list(db.REVIEW_STATUSES)}'})
        updated, failed = 0, []
        for guid in guids:
            if db.set_event_review_status(guid, status):
                updated += 1
            else:
                failed.append({'guid': guid, 'error': f'No such event: {guid}'})
        result = {'updated': updated, 'failed': failed}

    elif action == 'hide':
        result = db.bulk_delete_events(guids, actor,
                                       comment or 'hidden in bulk')
    elif action == 'unhide':
        result = db.bulk_unhide_events(guids, actor,
                                       comment or 'unhidden in bulk')
    elif action == 'unmerge':
        result = db.bulk_unmerge_events(guids, actor,
                                        comment or 'unmerged in bulk')
    elif action == 'add_category':
        slug = str(data.get('category_slug') or '').strip()
        if not slug:
            return _json(400, {'error': 'category_slug is required'})
        result = db.bulk_set_category(guids, slug, actor,
                                      comment or 'category added in bulk')
    else:  # combine
        target = str(data.get('canonical_guid') or '').strip()
        if not target:
            # Never inferred from selection order: which listing readers keep
            # is the entire decision.
            return _json(400, {'error': 'canonical_guid is required — say '
                                        'which event should survive'})
        canonical = db.get_event_from_config(target)
        if not canonical:
            return _json(422, {'error': f'No such event to merge into: {target}'})
        canonical_effective = _effective(canonical)
        if canonical_effective['hidden']:
            return _json(422, {'error': 'The event you are merging into is '
                                        'hidden, so both listings would '
                                        'disappear'})
        if canonical_effective['duplicate_of']:
            return _json(422, {'error': 'The event you are merging into is '
                                        'itself merged into another'})
        result = db.bulk_combine_events(guids, target, actor,
                                        comment or 'merged in bulk')

    result['action'] = action
    return _json(200, result)


def get_qa_run_json(event, jinja_env, run_id):
    """GET /api/admin/qa-runs/{run_id} — what one agent run changed."""
    claims, err = _admin_check(event)
    if err:
        return err
    changes = db.describe_qa_run(run_id)
    return _json(200, {'run_id': run_id, 'count': len(changes),
                       'changes': changes})


def revert_qa_run_json(event, jinja_env, run_id):
    """POST /api/admin/qa-runs/{run_id}/revert.

    What makes the digest email's revert_qa_run("qc-…") instruction something a
    human can click instead of paste into an MCP call.
    """
    claims, err = _admin_check(event)
    if err:
        return err
    return _json(200, db.revert_qa_run(run_id, actor=claims.get('email', '')))
