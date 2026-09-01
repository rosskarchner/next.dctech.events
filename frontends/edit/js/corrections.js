(function() {
  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  const FIELD_LABELS = {
    description: 'Description', location: 'Venue / location', url: 'URL',
    time: 'Time', end_time: 'End time',
  };

  let currentCorrections = [];

  function renderProposedFields(correction) {
    const fields = correction.fields || {};
    return Object.entries(fields).map(([field, value]) => `
      <div class="detail-row">
        <span class="detail-label">${escapeHtml(FIELD_LABELS[field] || field)}:</span>
        <span class="detail-value">${escapeHtml(value)}</span>
      </div>
    `).join('');
  }

  function targetLabel(c) {
    const type = c.target_type || 'event';
    const title = c.target_title || c.target_id || c.target_guid || 'Untitled';
    if (type === 'recurring_series') return `Recurring series: ${title}`;
    if (type === 'recurring_instance') return `Recurring series: ${title} — ${c.target_date}`;
    return title;
  }

  function targetSourceLabel(c) {
    const type = c.target_type || 'event';
    if (type === 'recurring_series') return 'recurring series definition';
    if (type === 'recurring_instance') return 'one occurrence only';
    return c.target_source || 'unknown';
  }

  function conflictKey(c) {
    // Only proposals that would actually clobber the same stored value on
    // approval count as conflicting: two recurring_instance corrections for
    // different dates of the same series write different OVERRIDE# rows and
    // don't clobber each other, so date is part of the key.
    const type = c.target_type || 'event';
    const id = c.target_id || c.target_guid;
    if (type === 'recurring_instance') return `instance|${id}|${c.target_date}`;
    if (type === 'recurring_series') return `series|${id}`;
    return `event|${id}`;
  }

  function renderCorrectionRow(correction, siblingCount) {
    const conflictNote = siblingCount > 1
      ? `<p class="hint" style="color:#a15c00;">
           ⚠ ${siblingCount - 1} other pending correction${siblingCount > 2 ? 's' : ''}
           also target this — approving more than one applies each in
           turn, last one wins per field.
         </p>`
      : '';

    return `
      <tr class="approve-form-row">
        <td colspan="2">
          <div class="approve-form">
            <div class="approve-form-header">
              <strong>${escapeHtml(targetLabel(correction))}</strong>
              <span class="approve-form-submitter">
                proposed by ${escapeHtml(correction.submitter_email || 'unknown')}
                &middot; ${escapeHtml(targetSourceLabel(correction))}
              </span>
            </div>
            ${conflictNote}
            <div class="draft-content">
              ${renderProposedFields(correction)}
              <div class="detail-row">
                <span class="detail-label">Reason:</span>
                <span class="detail-value">${escapeHtml(correction.reason || '')}</span>
              </div>
            </div>
            <div class="approve-form-actions">
              <button type="button" class="btn btn-success btn-sm" data-action="approve" data-correction-id="${escapeHtml(correction.id)}">Approve</button>
              <button type="button" class="btn btn-danger btn-sm" data-action="reject" data-correction-id="${escapeHtml(correction.id)}">Reject</button>
            </div>
          </div>
        </td>
      </tr>
    `;
  }

  function renderCorrections() {
    const container = document.getElementById('corrections-list');
    if (!container) return;

    if (!currentCorrections.length) {
      container.innerHTML = '<div class="draft-queue"><h2>Corrections</h2><p>No pending corrections.</p></div>';
      return;
    }

    const byTarget = {};
    currentCorrections.forEach((c) => {
      const key = conflictKey(c);
      byTarget[key] = (byTarget[key] || 0) + 1;
    });

    const rows = currentCorrections
      .map((c) => renderCorrectionRow(c, byTarget[conflictKey(c)]))
      .join('');

    container.innerHTML = `
      <div class="draft-queue">
        <h2>Pending Corrections (${currentCorrections.length})</h2>
        <table>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  async function loadCorrections() {
    const loading = document.getElementById('corrections-loading');
    if (loading) loading.style.display = 'block';

    try {
      const response = await DctechAuth.authorizedFetch(
        '/api/admin/corrections?status=pending');
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || 'Failed to load corrections.');
      }
      currentCorrections = payload.corrections || [];
      renderCorrections();
    } catch (err) {
      const container = document.getElementById('corrections-list');
      if (container) {
        container.innerHTML = `<div class="message message-error"><p>${escapeHtml(err.message)}</p></div>`;
      }
    } finally {
      if (loading) loading.style.display = 'none';
    }
  }

  async function approveCorrection(correctionId) {
    const response = await DctechAuth.authorizedFetch(
      `/api/admin/corrections/${correctionId}/approve`, { method: 'POST' });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || 'Failed to approve correction.');
    }
    await loadCorrections();
  }

  async function rejectCorrection(correctionId) {
    const reason = window.prompt('Reason for rejecting (optional):', '') || undefined;
    const response = await DctechAuth.authorizedFetch(
      `/api/admin/corrections/${correctionId}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || 'Failed to reject correction.');
    }
    await loadCorrections();
  }

  function bindEvents() {
    const refreshButton = document.getElementById('corrections-refresh');
    if (refreshButton) {
      refreshButton.addEventListener('click', loadCorrections);
    }

    document.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;

      const action = button.getAttribute('data-action');
      const correctionId = button.getAttribute('data-correction-id');
      if (!correctionId) return;

      try {
        if (action === 'approve') {
          await approveCorrection(correctionId);
        } else if (action === 'reject') {
          await rejectCorrection(correctionId);
        }
      } catch (err) {
        const container = document.getElementById('corrections-list');
        if (container) {
          container.insertAdjacentHTML(
            'afterbegin',
            `<div class="message message-error"><p>${escapeHtml(err.message)}</p></div>`);
        }
      }
    });
  }

  async function initCorrectionsPage() {
    const isAdmin = DctechAuth.requireAdmin();
    if (!isAdmin) return;
    bindEvents();
    await loadCorrections();
  }

  window.DctechCorrectionsPage = {
    init: initCorrectionsPage,
  };
})();
