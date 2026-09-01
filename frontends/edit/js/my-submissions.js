(function() {
  function renderSubmissions(submissions) {
    if (!submissions.length) {
      return `
        <div class="message message-info">
          <p>You haven't submitted anything yet.</p>
          <p><a href="submit-event.html">Submit an event</a> or <a href="submit-group.html">submit a group</a> to get started.</p>
        </div>
      `;
    }

    const rows = submissions.map((submission) => {
      const name = submission.name || submission.title || 'Untitled';
      const status = submission.status || '';
      const submitted = submission.created_at ? submission.created_at.slice(0, 10) : '';
      const type = submission.draft_type || '';
      const linkedName = status === 'approved' && submission.commit_url
        ? `<a href="${DctechUtil.escapeHtml(submission.commit_url)}" target="_blank" rel="noopener">${DctechUtil.escapeHtml(name)}</a>`
        : DctechUtil.escapeHtml(name);

      return `
        <tr>
          <td>${DctechUtil.escapeHtml(type)}</td>
          <td>${linkedName}</td>
          <td><span class="status-badge status-${DctechUtil.escapeHtml(status)}">${DctechUtil.escapeHtml(status)}</span></td>
          <td>${DctechUtil.escapeHtml(submitted)}</td>
        </tr>
      `;
    }).join('');

    return `
      <table class="submissions-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Name</th>
            <th>Status</th>
            <th>Submitted</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  async function initMySubmissionsPage() {
    const hasAuth = DctechAuth.requireAuth();
    if (!hasAuth) return;

    const list = document.getElementById('submissions-list');
    const loading = document.getElementById('loading-indicator');
    if (!list) return;

    try {
      const response = await DctechAuth.authorizedFetch('/api/my-submissions');
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || 'Failed to load your submissions.');
      }
      list.innerHTML = renderSubmissions(payload.submissions || []);
    } catch (err) {
      list.innerHTML = `<div class="message message-error"><p>${DctechUtil.escapeHtml(err.message)}</p></div>`;
    } finally {
      if (loading) {
        loading.style.display = 'none';
      }
    }
  }

  window.DctechMySubmissionsPage = {
    init: initMySubmissionsPage,
  };
})();
