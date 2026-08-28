// Events & QA — the human half of calendar moderation.
//
// Everything the weekly QC agent can do, a person can do here: correct a title,
// location or categories, hide a listing, merge a duplicate, and clear the
// review queue. Both go through the same overlay writer in db.py, so a fix
// applied here is byte-for-byte a fix the agent would have applied.
//
// Follows js/queue.js: vanilla fetch, string templating, one delegated click
// listener, and a full re-render of the table after every mutation. The corpus
// is one request, so a re-render is cheap and removes partial-state bugs.
(function() {
  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  let allEvents = [];
  let categoriesBySlug = {};
  let editableFields = [];
  let selected = new Set();
  let expandedGuid = null;
  // Set while the merge confirmation is open: {guids} awaiting a canonical
  // choice. Never resolved from selection order — see renderMergeConfirm.
  let merging = null;
  let lastUndo = null;

  const view = { state: 'pending_qa', source: '', month: '', category: '',
                 q: '', includePast: false };

  // ── data ────────────────────────────────────────────────────────

  function query() {
    const params = new URLSearchParams();
    // "Needs review" and "Flagged" are review_status values, which the API
    // serves from GSI5 — cheaper than a full sweep, and with no date bound, so
    // past-dated events still sitting in the queue come back too.
    if (view.state === 'pending_qa' || view.state === 'flagged') {
      params.set('review_status', view.state);
    } else if (view.state !== 'any') {
      params.set('state', view.state);
    }
    if (view.source) params.set('source', view.source);
    if (view.month) params.set('month', view.month);
    if (view.category) params.set('category', view.category);
    if (view.q) params.set('q', view.q);
    if (view.includePast) params.set('include_past', '1');
    return params.toString();
  }

  async function loadEvents() {
    const loading = document.getElementById('events-loading');
    if (loading) loading.style.display = 'block';
    try {
      const response = await DctechAuth.authorizedFetch(
        `/api/admin/events?${query()}`);
      if (!response.ok) throw new Error(await errorText(response));
      const payload = await response.json();
      allEvents = payload.events || [];
      editableFields = payload.editable_fields || [];
      // A guid that scrolled out of the current view must not stay selected
      // and get swept into the next bulk action.
      const visible = new Set(allEvents.map(e => e.guid));
      selected = new Set([...selected].filter(g => visible.has(g)));
      renderMonths(payload.months || []);
      renderTable();
      renderFooter(payload);
      renderBulkState();
    } finally {
      if (loading) loading.style.display = 'none';
    }
  }

  async function loadCategories() {
    const response = await fetch(DctechAuth.getApiUrl('/api/categories'));
    if (!response.ok) return;
    categoriesBySlug = await response.json();
    const options = Object.entries(categoriesBySlug)
      .sort((a, b) => (a[1].name || a[0]).localeCompare(b[1].name || b[0]))
      .map(([slug, cat]) =>
        `<option value="${escapeHtml(slug)}">${escapeHtml(cat.name || slug)}</option>`)
      .join('');

    const bulk = document.getElementById('bulk-category');
    if (bulk) bulk.innerHTML = '<option value="">Add category…</option>' + options;

    const filter = document.getElementById('filter-category');
    if (filter) filter.innerHTML = '<option value="">Any category</option>' + options;
  }

  function monthLabel(month) {
    // 'YYYY-MM' -> 'September 2026'. Parsed as UTC noon so a timezone west of
    // UTC cannot roll it back into the previous month.
    const [year, mon] = month.split('-');
    const when = new Date(Date.UTC(Number(year), Number(mon) - 1, 15));
    return when.toLocaleDateString(undefined,
      { month: 'long', year: 'numeric', timeZone: 'UTC' });
  }

  function renderMonths(months) {
    const select = document.getElementById('filter-month');
    if (!select) return;
    // The API returns the months present *before* its own month filter runs,
    // so the list stays complete and you can switch away from the month you
    // just chose. Keep the current value even if it somehow drops out, or the
    // select would silently reset and widen the results.
    const options = [...months];
    if (view.month && !options.includes(view.month)) options.push(view.month);
    options.sort().reverse();

    select.innerHTML = '<option value="">Any month</option>' +
      options.map(m =>
        `<option value="${escapeHtml(m)}">${escapeHtml(monthLabel(m))}</option>`)
        .join('');
    select.value = view.month;
  }

  async function errorText(response) {
    try {
      const body = await response.json();
      if (body.error) return body.error;
      return JSON.stringify(body);
    } catch (err) {
      return `Request failed (${response.status})`;
    }
  }

  // ── rendering ───────────────────────────────────────────────────

  function byGuid(guid) {
    return allEvents.find(e => e.guid === guid);
  }

  function categoryNames(slugs) {
    return (slugs || []).map(s => (categoriesBySlug[s]?.name) || s);
  }

  function timeCell(event) {
    const t = event.time;
    // A multi-day conference can carry a {date: 'HH:MM'} map — later on its
    // first day than the rest. calgen renders that shape, so show it.
    if (t && typeof t === 'object') {
      return Object.entries(t)
        .sort()
        .map(([day, at]) =>
          `<div style="font-size:0.75rem; white-space:nowrap;">${escapeHtml(day)}: ${escapeHtml(at)}</div>`)
        .join('');
    }
    return escapeHtml(t || 'All day');
  }

  const DUPLICATE_BADGES = {
    ok: ['badge-merged', 'Merged', 'Hidden as a duplicate of another listing.'],
    dangling: ['badge-merged', 'Dangling',
      'Points at an event that is not in the calendar, so it is ignored at ' +
      'render and this listing still shows.'],
    'into-hidden': ['badge-merged', 'Both gone',
      'Merged into an event that is itself hidden, so both listings disappear.'],
    chain: ['badge-merged', 'Chained',
      'Merged into an event that is itself merged into another.'],
  };

  function badges(event) {
    const out = [];
    const eff = event.effective || {};
    const meta = event.overlay_meta || {};

    if (eff.hidden) {
      out.push(badge('badge-hidden', 'Hidden', 'Not shown on the calendar.'));
    }
    const dup = DUPLICATE_BADGES[event.duplicate_state];
    if (dup) out.push(badge(dup[0], dup[1], dup[2]));

    const agent = meta.run_id || String(meta.edited_by || '').startsWith('agent:');
    if (agent) {
      out.push(badge('badge-tag', 'Agent', meta.comment || 'Edited by the QC agent.'));
    } else if (Object.keys(event.overlay || {}).length) {
      out.push(badge('badge-tag', 'Edited',
        `Hand-edited${meta.edited_by ? ' by ' + meta.edited_by : ''}.`));
    }

    if (event.review_status === 'pending_qa') {
      out.push(badge('badge-tag', 'Needs review'));
    } else if (event.review_status === 'flagged') {
      out.push(badge('badge-hidden', 'Flagged', 'Flagged for a human.'));
    }

    categoryNames(eff.categories).forEach(name =>
      out.push(badge('badge-tag', name)));
    return out.join(' ');
  }

  function badge(cls, label, title) {
    const attr = title ? ` title="${escapeHtml(title)}"` : '';
    return `<span class="badge ${cls}"${attr}>${escapeHtml(label)}</span>`;
  }

  function renderRow(event) {
    const eff = event.effective || {};
    const dim = (eff.hidden || eff.duplicate_of) ? ' style="opacity:0.6;"' : '';
    const checked = selected.has(event.guid) ? ' checked' : '';
    const location = eff.location ||
      [event.city, event.state].filter(Boolean).join(', ');

    return `
      <tr id="event-row-${escapeHtml(event.guid)}"${dim}>
        <td class="col-check">
          <input type="checkbox" class="event-checkbox"
                 data-guid="${escapeHtml(event.guid)}"${checked}>
        </td>
        <td class="col-title">
          <div style="font-weight:600; margin-bottom:0.25rem;">
            ${event.url
              ? `<a href="${escapeHtml(event.url)}" target="_blank" rel="noopener">${escapeHtml(eff.title || 'Untitled')}</a>`
              : escapeHtml(eff.title || 'Untitled')}
          </div>
          <div style="display:flex; flex-wrap:wrap; gap:4px;">${badges(event)}</div>
        </td>
        <td class="col-date">
          <div style="font-weight:500;">${escapeHtml(event.date || '')}</div>
          ${event.end_date && event.end_date !== event.date
            ? `<div style="font-size:0.75rem;" class="text-muted">to ${escapeHtml(event.end_date)}</div>`
            : ''}
        </td>
        <td class="col-time">${timeCell(event)}</td>
        <td class="col-loc"><div style="font-size:0.8rem;">${escapeHtml(location)}</div></td>
        <td class="col-src">
          <span class="badge badge-tag">${escapeHtml(event.source || 'manual')}</span>
        </td>
        <td class="col-actions">
          <button type="button" class="btn btn-sm btn-outline"
                  data-action="edit" data-guid="${escapeHtml(event.guid)}">
            ${expandedGuid === event.guid ? 'Close' : 'Edit'}
          </button>
        </td>
      </tr>
      ${expandedGuid === event.guid ? renderEditRow(event) : ''}`;
  }

  function provenanceBlock(event) {
    const meta = event.overlay_meta || {};
    const agent = meta.run_id || String(meta.edited_by || '').startsWith('agent:');
    if (!Object.keys(event.overlay || {}).length && !meta.comment) return '';

    const touched = [...new Set([
      ...Object.keys(meta.prior || {}), ...(meta.added || []),
    ])];
    const who = agent ? 'The QC agent' : escapeHtml(meta.edited_by || 'Someone');

    return `
      <div class="trust-section" style="margin-bottom:1rem;">
        <div class="trust-label">${who} edited this event.</div>
        ${meta.run_id ? `<div style="font-size:0.85rem;">Run <code>${escapeHtml(meta.run_id)}</code></div>` : ''}
        ${meta.edited_at ? `<div style="font-size:0.85rem;" class="text-muted">${escapeHtml(meta.edited_at)}</div>` : ''}
        ${touched.length ? `<div style="font-size:0.85rem;">It set: ${escapeHtml(touched.join(', '))}</div>` : ''}
        ${meta.comment ? `<div style="font-size:0.85rem; margin-top:0.4rem;"><em>${escapeHtml(meta.comment)}</em></div>` : ''}
        ${meta.run_id ? `<div style="font-size:0.8rem; margin-top:0.4rem;" class="text-muted">
          Undo the whole run with the MCP tool <code>revert_qa_run("${escapeHtml(meta.run_id)}")</code>.
        </div>` : ''}
      </div>`;
  }

  function readOnlyRow(label, value) {
    if (value === null || value === undefined || value === '') return '';
    return `<div class="detail-row">
      <span class="detail-label">${escapeHtml(label)}</span>
      <span class="detail-value">${escapeHtml(value)}</span>
    </div>`;
  }

  function renderEditRow(event) {
    const eff = event.effective || {};
    const overlay = event.overlay || {};
    const feedNote = event.source === 'ical'
      ? `iCal event from <strong>${escapeHtml(event.group || 'a feed')}</strong> — the feed
         rewrites this record every few hours, so your edit is stored as an
         overlay on top of it. Date, time and group come from the feed and
         cannot be changed here.`
      : `${escapeHtml(event.source || 'manual')} event — your edit is stored as an
         overlay, so the original submission stays intact underneath and the
         change is revertible.`;

    const cats = eff.categories || [];
    const checkboxes = Object.entries(categoriesBySlug)
      .sort((a, b) => (a[1].name || a[0]).localeCompare(b[1].name || b[0]))
      .map(([slug, cat]) => `
        <label class="category-checkbox">
          <input type="checkbox" class="edit-category" value="${escapeHtml(slug)}"
                 ${cats.includes(slug) ? 'checked' : ''}>
          ${escapeHtml(cat.name || slug)}
        </label>`).join('');

    return `
      <tr class="approve-form-row">
        <td colspan="7">
          <div class="approve-form">
            <div class="approve-form-header">
              <strong>${escapeHtml(eff.title || 'Untitled')}</strong>
              <div class="approve-form-submitter" style="max-width:52%;">${feedNote}</div>
            </div>

            ${provenanceBlock(event)}

            <div class="draft-content">
              ${readOnlyRow('Date', event.date)}
              ${readOnlyRow('End date', event.end_date)}
              ${readOnlyRow('Group', event.group)}
              ${readOnlyRow('Source', event.source)}
              ${readOnlyRow('Review status', event.review_status)}
              ${readOnlyRow('Guid', event.guid)}
              ${eff.duplicate_of
                ? readOnlyRow('Merged into', eff.duplicate_of)
                : ''}
            </div>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.75rem; margin:1rem 0;">
              <label style="display:flex; flex-direction:column; gap:2px; font-size:0.85rem;">
                Title
                <input type="text" class="form-control" id="edit-title"
                       value="${escapeHtml(overlay.title ?? '')}"
                       placeholder="${escapeHtml(event.title || '')}">
              </label>
              <label style="display:flex; flex-direction:column; gap:2px; font-size:0.85rem;">
                Location
                <input type="text" class="form-control" id="edit-location"
                       value="${escapeHtml(overlay.location ?? '')}"
                       placeholder="${escapeHtml(event.location || '')}">
              </label>
            </div>
            <p class="text-muted" style="font-size:0.8rem; margin:0 0 1rem;">
              An empty box means the feed's own value is used. The grey text is
              what that is.
            </p>

            <div class="categories-section-edit">
              <h4>Categories</h4>
              <div class="categories-wrapper">${checkboxes}</div>
            </div>

            <label style="display:flex; flex-direction:column; gap:2px; font-size:0.85rem; margin:1rem 0;">
              Why (recorded with the change, and read by whoever reviews it)
              <input type="text" class="form-control" id="edit-comment"
                     placeholder="what was wrong, and where each new value came from">
            </label>

            <div class="approve-form-actions">
              <button type="button" class="btn btn-primary btn-sm"
                      data-action="save" data-guid="${escapeHtml(event.guid)}">Save</button>
              ${eff.hidden
                ? `<button type="button" class="btn btn-outline btn-sm" data-action="unhide" data-guid="${escapeHtml(event.guid)}">Show on calendar</button>`
                : `<button type="button" class="btn btn-danger btn-sm" data-action="hide" data-guid="${escapeHtml(event.guid)}">Hide from calendar</button>`}
              ${eff.duplicate_of
                ? `<button type="button" class="btn btn-outline btn-sm" data-action="unmerge" data-guid="${escapeHtml(event.guid)}">Not a duplicate</button>`
                : ''}
              ${event.review_status === 'pending_qa'
                ? `<button type="button" class="btn btn-success btn-sm" data-action="approve" data-guid="${escapeHtml(event.guid)}">Approve</button>
                   <button type="button" class="btn btn-outline btn-sm" data-action="flag" data-guid="${escapeHtml(event.guid)}">Flag</button>`
                : ''}
              ${Object.keys(overlay).length
                ? `<button type="button" class="btn btn-outline btn-sm" data-action="clear-overlay" data-guid="${escapeHtml(event.guid)}">Clear all edits</button>`
                : ''}
              <button type="button" class="btn btn-outline btn-sm"
                      data-action="edit" data-guid="${escapeHtml(event.guid)}">Close</button>
            </div>
          </div>
        </td>
      </tr>`;
  }

  function renderTable() {
    const container = document.getElementById('events-table');
    if (!container) return;
    if (!allEvents.length) {
      container.innerHTML = '<p class="text-muted">No events match these filters.</p>';
      return;
    }
    container.innerHTML = `
      <div class="table-container">
        <table class="admin-table">
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" id="select-all"></th>
              <th class="col-title">Title</th>
              <th class="col-date">Date</th>
              <th class="col-time">Time</th>
              <th class="col-loc">Location</th>
              <th class="col-src">Source</th>
              <th class="col-actions"></th>
            </tr>
          </thead>
          <tbody>${allEvents.map(renderRow).join('')}</tbody>
        </table>
      </div>`;
  }

  function renderFooter(payload) {
    const footer = document.getElementById('events-footer');
    if (!footer) return;
    const needing = allEvents.filter(e => e.review_status === 'pending_qa').length;
    const problems = allEvents.filter(e =>
      ['dangling', 'into-hidden', 'chain'].includes(e.duplicate_state)).length;
    const bits = [`${payload.count} event${payload.count === 1 ? '' : 's'} shown`];
    if (needing) bits.push(`${needing} need review`);
    if (problems) bits.push(`${problems} with a broken merge`);
    if (payload.truncated) bits.push('list truncated');

    footer.innerHTML = `
      <div>${escapeHtml(bits.join(' · '))}</div>
      ${lastUndo ? `<div style="margin-top:0.4rem;">${escapeHtml(lastUndo.label)}
        <button type="button" class="btn btn-sm btn-outline" data-action="undo">Undo</button></div>` : ''}
      <div style="margin-top:0.4rem;">
        Edits reach the calendar on the next site build, usually within a couple
        of minutes. Recurring series are managed separately and do not appear here.
      </div>`;
  }

  function renderBulkState() {
    const count = selected.size;
    document.querySelectorAll('button[data-bulk]').forEach(button => {
      button.disabled = count === 0;
      const label = button.getAttribute('data-bulk');
      const base = {
        approve: 'Approve reviewed', flag: 'Flag', add_category: 'Apply',
        unhide: 'Unhide', hide: 'Hide', combine: 'Merge…',
      }[label];
      button.textContent = count ? `${base} (${count})` : base;
    });
  }

  // ── the merge confirmation ──────────────────────────────────────

  function renderMergeConfirm() {
    const container = document.getElementById('events-confirm');
    if (!container) return;
    if (!merging) { container.innerHTML = ''; return; }

    const rows = merging.guids.map(byGuid).filter(Boolean);
    container.innerHTML = `
      <div class="approve-form" style="margin-bottom:1rem;">
        <div class="approve-form-header">
          <strong>Merge ${rows.length} events — which one should the calendar keep?</strong>
        </div>
        <p class="text-muted" style="font-size:0.85rem;">
          The ones you do not keep disappear from the calendar. The kept listing
          gains an “also published by” credit for each one merged into it.
        </p>
        ${rows.map(event => {
          const eff = event.effective || {};
          const blocked = eff.hidden || eff.duplicate_of;
          return `
            <label style="display:flex; gap:0.6rem; align-items:flex-start; padding:0.5rem 0; ${blocked ? 'opacity:0.55;' : 'cursor:pointer;'}">
              <input type="radio" name="merge-canonical" class="merge-canonical"
                     value="${escapeHtml(event.guid)}" ${blocked ? 'disabled' : ''}>
              <span>
                <span style="font-weight:600;">${escapeHtml(eff.title || 'Untitled')}</span>
                <div style="font-size:0.8rem;" class="text-muted">
                  ${escapeHtml(event.date || '')} ·
                  ${escapeHtml(event.group || 'no group')} ·
                  ${escapeHtml(eff.location || '')}
                </div>
                <div style="display:flex; flex-wrap:wrap; gap:4px; margin-top:2px;">${badges(event)}</div>
                ${blocked ? '<div style="font-size:0.8rem;" class="text-muted">Cannot be the survivor: it is already hidden or merged.</div>' : ''}
              </span>
            </label>`;
        }).join('')}
        <label style="display:flex; flex-direction:column; gap:2px; font-size:0.85rem; margin:0.75rem 0;">
          Why
          <input type="text" class="form-control" id="merge-comment"
                 placeholder="e.g. same event posted by two groups; keeping the organiser's listing">
        </label>
        <div class="approve-form-actions">
          <button type="button" class="btn btn-danger btn-sm" data-action="confirm-merge">Merge</button>
          <button type="button" class="btn btn-outline btn-sm" data-action="cancel-merge">Cancel</button>
        </div>
      </div>`;
  }

  // ── writes ──────────────────────────────────────────────────────

  function showMessage(text, kind) {
    const box = document.getElementById('events-message');
    if (!box) return;
    box.innerHTML = `<div class="message message-${kind}"><p>${escapeHtml(text)}</p></div>`;
    if (kind === 'success') setTimeout(() => { box.innerHTML = ''; }, 6000);
  }

  async function putOverlay(guid, body) {
    const response = await DctechAuth.authorizedFetch(
      `/api/admin/events/${guid}/overlay`,
      { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    if (response.status === 409) {
      const payload = await response.json();
      throw new Error(
        'Someone else changed this event while you were editing it. ' +
        `It now reads ${JSON.stringify(payload.current?.overlay || {})}. ` +
        'Close the row, reopen it, and redo your change.');
    }
    if (!response.ok) throw new Error(await errorText(response));
    return response.json();
  }

  function agentFieldsOf(event) {
    const meta = event.overlay_meta || {};
    return new Set([...Object.keys(meta.prior || {}), ...(meta.added || [])]);
  }

  async function saveEvent(guid) {
    const event = byGuid(guid);
    if (!event) return;
    const comment = document.getElementById('edit-comment')?.value.trim() || '';
    if (!comment) {
      showMessage('Say why, briefly — it is what the next reviewer reads.', 'error');
      return;
    }

    const title = document.getElementById('edit-title')?.value.trim() ?? '';
    const location = document.getElementById('edit-location')?.value.trim() ?? '';
    const cats = [...document.querySelectorAll('.edit-category:checked')]
      .map(cb => cb.value);

    const overlay = event.overlay || {};
    const fields = {};
    const clear = [];

    if (title) fields.title = title;
    else if ('title' in overlay) clear.push('title');
    if (location) fields.location = location;
    else if ('location' in overlay) clear.push('location');

    const currentCats = (event.effective || {}).categories || [];
    if (cats.join() !== currentCats.join()) fields.categories = cats;

    if (!Object.keys(fields).length && !clear.length) {
      showMessage('Nothing changed.', 'error');
      return;
    }

    // Warn only about fields the agent actually set and this save replaces.
    const agentFields = agentFieldsOf(event);
    const clashes = [...Object.keys(fields), ...clear]
      .filter(f => agentFields.has(f));
    if (clashes.length) {
      const meta = event.overlay_meta || {};
      const ok = window.confirm(
        `The QC agent set ${clashes.join(', ')} on this event` +
        (meta.run_id ? ` in run ${meta.run_id}` : '') + '.\n\n' +
        (meta.comment ? `Its reason: "${meta.comment}"\n\n` : '') +
        'Saving replaces the agent\'s value, and reverting that run will no ' +
        'longer restore it for those fields. Continue?');
      if (!ok) return;
    }

    const body = { fields, comment };
    if (clear.length) body.clear = clear;
    // The revision this form was rendered from, so a concurrent write is a
    // conflict rather than a silent clobber.
    body.expected_rev = (event.overlay_meta || {}).rev || null;

    await putOverlay(guid, body);
    showMessage('Saved.', 'success');
    expandedGuid = null;
    await loadEvents();
  }

  async function setVisibility(guid, hidden) {
    const event = byGuid(guid);
    if (hidden && !window.confirm(
        'Hide this event? It disappears from the calendar for everyone.')) return;
    const body = hidden
      ? { fields: { hidden: true }, comment: 'hidden by a moderator' }
      : { clear: ['hidden'], comment: 'unhidden by a moderator' };
    body.expected_rev = (event?.overlay_meta || {}).rev || null;
    await putOverlay(guid, body);
    lastUndo = hidden
      ? { label: 'Hid 1 event.', action: 'unhide', guids: [guid] }
      : { label: 'Unhid 1 event.', action: 'hide', guids: [guid] };
    expandedGuid = null;
    await loadEvents();
  }

  async function unmerge(guid) {
    const event = byGuid(guid);
    await putOverlay(guid, {
      clear: ['duplicate_of', 'hidden'],
      comment: 'not a duplicate',
      expected_rev: (event?.overlay_meta || {}).rev || null,
    });
    expandedGuid = null;
    await loadEvents();
  }

  async function clearOverlay(guid) {
    if (!window.confirm(
        'Clear every edit on this event and fall back to what the feed says?')) return;
    const response = await DctechAuth.authorizedFetch(
      `/api/admin/events/${guid}/overlay`, { method: 'DELETE' });
    if (!response.ok) throw new Error(await errorText(response));
    showMessage('Edits cleared.', 'success');
    expandedGuid = null;
    await loadEvents();
  }

  async function setReviewStatus(guid, status) {
    const response = await DctechAuth.authorizedFetch(
      `/api/admin/events/${guid}/review-status`,
      { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review_status: status }) });
    if (!response.ok) throw new Error(await errorText(response));
    await loadEvents();
  }

  async function bulk(action, extra) {
    const guids = [...selected];
    const response = await DctechAuth.authorizedFetch('/api/admin/events/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, guids, ...(extra || {}) }),
    });
    if (!response.ok) throw new Error(await errorText(response));
    const payload = await response.json();
    const failed = (payload.failed || []).length;
    showMessage(
      `${payload.updated} event${payload.updated === 1 ? '' : 's'} updated` +
      (failed ? `, ${failed} failed: ${payload.failed[0].error}` : '.'),
      failed ? 'error' : 'success');
    return { payload, guids };
  }

  const UNDOABLE = { hide: 'unhide', unhide: 'hide', combine: 'unmerge' };

  async function runBulk(action) {
    if (action === 'approve') {
      await bulk('set_review_status', { review_status: 'approved' });
    } else if (action === 'flag') {
      await bulk('set_review_status', { review_status: 'flagged' });
    } else if (action === 'add_category') {
      const slug = document.getElementById('bulk-category')?.value;
      if (!slug) { showMessage('Pick a category first.', 'error'); return; }
      await bulk('add_category', { category_slug: slug });
    } else if (action === 'hide') {
      if (!window.confirm(
          `Hide ${selected.size} event(s)? They disappear from the calendar.`)) return;
      const { payload, guids } = await bulk('hide');
      lastUndo = { label: `Hid ${payload.updated} event(s).`,
                   action: 'unhide', guids };
    } else if (action === 'unhide') {
      const { payload, guids } = await bulk('unhide');
      lastUndo = { label: `Unhid ${payload.updated} event(s).`,
                   action: 'hide', guids };
    } else if (action === 'combine') {
      if (selected.size < 2) {
        showMessage('Select at least two events to merge.', 'error');
        return;
      }
      merging = { guids: [...selected] };
      renderMergeConfirm();
      return;
    }
    selected = new Set();
    await loadEvents();
  }

  async function confirmMerge() {
    const canonical = document.querySelector('.merge-canonical:checked')?.value;
    if (!canonical) {
      showMessage('Choose which event the calendar should keep.', 'error');
      return;
    }
    const comment = document.getElementById('merge-comment')?.value.trim()
      || 'merged by a moderator';
    const guids = merging.guids.filter(g => g !== canonical);
    const response = await DctechAuth.authorizedFetch('/api/admin/events/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'combine', guids,
                             canonical_guid: canonical, comment }),
    });
    if (!response.ok) throw new Error(await errorText(response));
    const payload = await response.json();
    lastUndo = { label: `Merged ${payload.updated} event(s).`,
                 action: 'unmerge', guids };
    merging = null;
    selected = new Set();
    renderMergeConfirm();
    showMessage(`${payload.updated} event(s) merged.`, 'success');
    await loadEvents();
  }

  async function undo() {
    if (!lastUndo) return;
    const { action, guids } = lastUndo;
    lastUndo = null;
    const response = await DctechAuth.authorizedFetch('/api/admin/events/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, guids, comment: 'undone by a moderator' }),
    });
    if (!response.ok) throw new Error(await errorText(response));
    showMessage('Undone.', 'success');
    await loadEvents();
  }

  async function rebuild() {
    const response = await DctechAuth.authorizedFetch('/api/admin/rebuild',
      { method: 'POST' });
    if (!response.ok) throw new Error(await errorText(response));
    showMessage('Site rebuild started; live in a couple of minutes.', 'success');
  }

  // ── wiring ──────────────────────────────────────────────────────

  function bindEvents() {
    const on = (id, event, handler) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener(event, handler);
    };

    on('filter-view', 'change', async (e) => {
      view.state = e.target.value; expandedGuid = null; await loadEvents();
    });
    on('filter-source', 'change', async (e) => {
      view.source = e.target.value; await loadEvents();
    });
    on('filter-month', 'change', async (e) => {
      view.month = e.target.value; expandedGuid = null; await loadEvents();
    });
    on('filter-category', 'change', async (e) => {
      view.category = e.target.value; expandedGuid = null; await loadEvents();
    });
    on('filter-past', 'change', async (e) => {
      view.includePast = e.target.checked; await loadEvents();
    });
    on('filter-q', 'input', debounce(async (e) => {
      view.q = e.target.value.trim(); await loadEvents();
    }, 250));
    on('events-refresh', 'click', async () => {
      expandedGuid = null; selected = new Set(); await loadEvents();
    });
    on('events-rebuild', 'click', () => guard(rebuild()));

    document.addEventListener('change', (e) => {
      if (e.target.id === 'select-all') {
        document.querySelectorAll('.event-checkbox').forEach(cb => {
          cb.checked = e.target.checked;
          if (cb.checked) selected.add(cb.dataset.guid);
          else selected.delete(cb.dataset.guid);
        });
        renderBulkState();
      } else if (e.target.classList.contains('event-checkbox')) {
        if (e.target.checked) selected.add(e.target.dataset.guid);
        else selected.delete(e.target.dataset.guid);
        renderBulkState();
      }
    });

    document.addEventListener('click', (e) => {
      const bulkButton = e.target.closest('button[data-bulk]');
      if (bulkButton) {
        guard(runBulk(bulkButton.getAttribute('data-bulk')));
        return;
      }

      const button = e.target.closest('button[data-action]');
      if (!button) return;
      const action = button.getAttribute('data-action');
      const guid = button.getAttribute('data-guid');

      if (action === 'edit') {
        expandedGuid = expandedGuid === guid ? null : guid;
        renderTable();
      } else if (action === 'save') {
        guard(saveEvent(guid));
      } else if (action === 'hide') {
        guard(setVisibility(guid, true));
      } else if (action === 'unhide') {
        guard(setVisibility(guid, false));
      } else if (action === 'unmerge') {
        guard(unmerge(guid));
      } else if (action === 'clear-overlay') {
        guard(clearOverlay(guid));
      } else if (action === 'approve') {
        guard(setReviewStatus(guid, 'approved'));
      } else if (action === 'flag') {
        guard(setReviewStatus(guid, 'flagged'));
      } else if (action === 'confirm-merge') {
        guard(confirmMerge());
      } else if (action === 'cancel-merge') {
        merging = null;
        renderMergeConfirm();
      } else if (action === 'undo') {
        guard(undo());
      }
    });
  }

  function guard(promise) {
    Promise.resolve(promise).catch(err => showMessage(err.message, 'error'));
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  async function init() {
    // Before any fetch: requireAdmin can redirect to sign-in.
    if (!DctechAuth.requireAdmin()) return;
    bindEvents();
    await loadCategories();
    await loadEvents();
  }

  window.DctechEventsPage = { init };
})();
