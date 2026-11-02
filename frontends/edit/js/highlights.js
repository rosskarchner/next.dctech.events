(function() {
  const esc = (v) => DctechUtil.escapeHtml(v == null ? '' : String(v));
  const LIVE = ['held', 'payment_pending', 'active'];

  function el(id) {
    return document.getElementById(id);
  }

  function showMessage(text, kind) {
    el('hl-message').innerHTML = text
      ? `<div class="message message-${kind}"><p>${esc(text)}</p></div>`
      : '';
    if (text && kind === 'success') {
      setTimeout(() => { el('hl-message').innerHTML = ''; }, 5000);
    }
  }

  async function request(path, options) {
    const response = await DctechAuth.authorizedFetch(path, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      // A non-JSON body (gateway error page) still needs a usable message.
    }
    if (!response.ok) {
      throw new Error(payload.error || `Request failed (HTTP ${response.status})`);
    }
    return payload;
  }

  function jsonBody(body) {
    return { method: 'POST', headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify(body || {}) };
  }

  function addDays(iso, n) {
    const d = new Date(`${iso}T12:00:00`);
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  }

  function row(h) {
    const start = h.date ? addDays(h.date, -21) : '';
    const buyer = h.kind === 'paid'
      ? `${esc(h.contact_email || '—')}<br><span class="muted">PayPal ${esc(h.txn_id || '')} · $${esc(h.price_usd)}</span>`
      : `<span class="muted">${esc(h.created_by || '')}${h.note ? ` — ${esc(h.note)}` : ''}</span>`;
    const actions = LIVE.includes(h.status)
      ? `<button class="btn btn-sm btn-danger" data-act="revoke">Revoke</button>` : '';
    return `
      <tr data-key="${esc(h.target_key)}" data-kind="${esc(h.kind)}">
        <td style="min-width: 16rem;"><a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)}</a>
            <br><span class="muted">${esc(h.date)} ${esc(h.time || '')} · ${esc(h.target_key)}</span></td>
        <td><span class="badge kind-${esc(h.kind)}">${h.kind === 'editor' ? "Editor's pick" : 'Paid'}</span></td>
        <td class="muted">${esc(start)} → ${esc(h.date)}</td>
        <td>${buyer}</td>
        <td><span class="badge badge-${esc(h.status)}">${esc(h.status)}</span>
            ${h.revoked_by ? `<br><span class="muted">${esc(h.revoked_by)}</span>` : ''}</td>
        <td><div class="row-actions">${actions}</div></td>
      </tr>`;
  }

  async function load() {
    const status = el('status-filter').value;
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    try {
      const data = await request(`/api/admin/highlights${query}`);
      el('price-label').textContent = data.price_usd && data.price_usd !== '0'
        ? `Price: $${data.price_usd}` : 'Price not set: paid highlights are off';
      const rows = data.highlights || [];
      el('hl-rows').innerHTML = rows.length
        ? rows.map(row).join('')
        : '<tr><td colspan="6" class="muted">Nothing here.</td></tr>';
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  async function onAction(event) {
    const button = event.target.closest('button[data-act="revoke"]');
    if (!button) return;
    const tr = button.closest('tr');
    const paid = tr.dataset.kind === 'paid';
    const reason = window.prompt(paid
      ? 'Revoke this paid highlight? Remember to refund it in PayPal. Reason (optional):'
      : 'Revoke this editor\'s pick? Reason (optional):', '');
    if (reason === null) return;
    button.disabled = true;
    try {
      await request(`/api/admin/highlights/${encodeURIComponent(tr.dataset.key)}/revoke`,
                    jsonBody({ reason }));
      showMessage(paid ? 'Revoked. Refund it in PayPal.' : 'Revoked.', 'success');
      load();
    } catch (error) {
      showMessage(error.message, 'error');
      button.disabled = false;
    }
  }

  // An event page URL (/events/{date}-{title}-{guid[:8]}/) names only a guid
  // prefix, so look the full guid up in the published events.json.
  async function resolveGuid(input) {
    const text = input.trim();
    const m = text.match(/\/events\/(\d{4}-\d{2}-\d{2})-[^/]*-([0-9a-f]{8})\/?/i);
    if (!m) return text;
    const response = await fetch('/events.json', { cache: 'no-store' });
    const events = await response.json();
    const hit = events.find((e) => e.date === m[1] && String(e.guid || '').startsWith(m[2]));
    if (!hit) throw new Error('No event on the calendar matches that URL.');
    return hit.guid;
  }

  async function onPick(event) {
    event.preventDefault();
    const form = el('pick-form');
    const data = Object.fromEntries(new FormData(form).entries());
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const guid = await resolveGuid(data.guid);
      const result = await request('/api/admin/highlights', jsonBody({ guid, note: data.note }));
      showMessage(`Highlighted "${result.highlight.title}". It will show after the next rebuild.`, 'success');
      form.reset();
      load();
    } catch (error) {
      showMessage(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  function init() {
    if (!DctechAuth.requireAdmin()) return;
    el('status-filter').addEventListener('change', load);
    el('hl-rows').addEventListener('click', onAction);
    el('pick-form').addEventListener('submit', onPick);
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
