(function() {
  const esc = (v) => DctechUtil.escapeHtml(v);
  const REVIEWABLE = ['held', 'payment_pending', 'paid'];

  function el(id) {
    return document.getElementById(id);
  }

  function showMessage(text, kind) {
    el('sp-message').innerHTML = text
      ? `<div class="message message-${kind}"><p>${esc(text)}</p></div>`
      : '';
    if (text && kind === 'success') {
      setTimeout(() => { el('sp-message').innerHTML = ''; }, 5000);
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

  function jsonBody(method, body) {
    return {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    };
  }

  // ── bookings ──────────────────────────────────────────────────────

  function bookingRow(b) {
    const week = esc(b.week_id);
    const editable = REVIEWABLE.includes(b.status) || b.status === 'approved';
    const copy = editable
      ? `<input data-field="name" value="${esc(b.name)}" maxlength="60">
         <input data-field="message" value="${esc(b.message)}" maxlength="140">
         <input data-field="url" value="${esc(b.url)}">`
      : `<strong>${esc(b.name)}</strong> — ${esc(b.message)}<br><span class="muted">${esc(b.url)}</span>`;
    const how = b.payment_method === 'coupon'
      ? `coupon <code>${esc(b.coupon_code)}</code>`
      : b.payment_method === 'manual' ? 'manual' : `PayPal ${esc(b.txn_id || '')}`;
    const actions = [];
    if (editable) actions.push('<button class="btn btn-sm btn-outline" data-act="save">Save copy</button>');
    if (REVIEWABLE.includes(b.status)) actions.push('<button class="btn btn-sm btn-success" data-act="approve">Approve</button>');
    if (REVIEWABLE.includes(b.status) || b.status === 'approved') {
      actions.push('<button class="btn btn-sm btn-danger" data-act="reject">Reject</button>');
      actions.push('<button class="btn btn-sm btn-outline" data-act="cancel" title="Free the week without emailing the booker">Cancel</button>');
    }
    return `
      <tr data-week="${week}">
        <td><a href="/week/${week}/" target="_blank" rel="noopener">${week}</a></td>
        <td style="min-width: 18rem;">${copy}</td>
        <td>${esc(b.contact_email || '—')}<br><span class="muted">${how} · $${esc(b.price_usd)}</span></td>
        <td><span class="badge badge-${esc(b.status)}">${esc(b.status)}</span>
            ${b.reviewed_by ? `<br><span class="muted">${esc(b.reviewed_by)}</span>` : ''}</td>
        <td><div class="row-actions">${actions.join('')}</div></td>
      </tr>`;
  }

  async function loadBookings() {
    const status = el('status-filter').value;
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    try {
      const data = await request(`/api/admin/sponsorships${query}`);
      el('price-label').textContent = data.price_usd && data.price_usd !== '0'
        ? `Price: $${data.price_usd}/week` : 'Price not set: PayPal booking is off';
      const rows = data.sponsorships || [];
      el('booking-rows').innerHTML = rows.length
        ? rows.map(bookingRow).join('')
        : '<tr><td colspan="5" class="muted">Nothing here.</td></tr>';
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  async function onBookingAction(event) {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const row = button.closest('tr');
    const week = row.dataset.week;
    const act = button.dataset.act;
    const base = `/api/admin/sponsorships/${encodeURIComponent(week)}`;
    const copy = {};
    row.querySelectorAll('input[data-field]').forEach((input) => {
      copy[input.dataset.field] = input.value;
    });

    try {
      button.disabled = true;
      if (act === 'save') {
        await request(base, jsonBody('PUT', copy));
        showMessage(`Saved ${week}.`, 'success');
      } else if (act === 'approve') {
        // Save first, so whatever is in the boxes is what gets approved.
        if (Object.keys(copy).length) await request(base, jsonBody('PUT', copy));
        await request(`${base}/approve`, jsonBody('POST'));
        showMessage(`Approved ${week}. The sponsor has been emailed.`, 'success');
      } else if (act === 'reject') {
        const reason = window.prompt(`Reject ${week}? This emails the sponsor. Optional note to include:`, '');
        if (reason === null) return;
        await request(`${base}/reject`, jsonBody('POST', { reason }));
        showMessage(`Rejected ${week}. If this was a PayPal booking, refund it in PayPal.`, 'success');
      } else if (act === 'cancel') {
        if (!window.confirm(`Cancel ${week} without emailing the booker?`)) return;
        await request(`${base}/cancel`, jsonBody('POST'));
        showMessage(`Cancelled ${week}.`, 'success');
      }
      await Promise.all([loadBookings(), loadCoupons()]);
    } catch (error) {
      showMessage(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function onManualBooking(event) {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(event.target).entries());
    try {
      await request('/api/admin/sponsorships', jsonBody('POST', body));
      event.target.reset();
      showMessage(`Booked and approved ${body.week_id}.`, 'success');
      await loadBookings();
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  // ── coupons ───────────────────────────────────────────────────────

  function couponRow(c) {
    const used = c.redeemed_week
      ? `<a href="/week/${esc(c.redeemed_week)}/" target="_blank" rel="noopener">${esc(c.redeemed_week)}</a>`
      : (c.revoked ? '<span class="muted">revoked</span>' : '<span class="muted">unused</span>');
    const revoke = !c.redeemed_week && !c.revoked
      ? `<button class="btn btn-sm btn-danger" data-code="${esc(c.code)}">Revoke</button>` : '';
    return `
      <tr>
        <td><code class="coupon">${esc(c.code)}</code></td>
        <td>${esc(c.note || '')}</td>
        <td class="muted">${esc((c.created_at || '').slice(0, 10))}</td>
        <td class="muted">${esc(c.expires_on || '—')}</td>
        <td>${used}</td>
        <td>${revoke}</td>
      </tr>`;
  }

  async function loadCoupons() {
    try {
      const data = await request('/api/admin/coupons');
      const rows = data.coupons || [];
      el('coupon-rows').innerHTML = rows.length
        ? rows.map(couponRow).join('')
        : '<tr><td colspan="6" class="muted">No coupons yet.</td></tr>';
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  async function onMint(event) {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(event.target).entries());
    try {
      const data = await request('/api/admin/coupons', jsonBody('POST', body));
      const codes = (data.coupons || []).map((c) => c.code).join(', ');
      showMessage(`Minted: ${codes}`, 'success');
      event.target.reset();
      await loadCoupons();
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  async function onRevoke(event) {
    const button = event.target.closest('button[data-code]');
    if (!button) return;
    const code = button.dataset.code;
    if (!window.confirm(`Revoke coupon ${code}?`)) return;
    try {
      await request(`/api/admin/coupons/${encodeURIComponent(code)}`, { method: 'DELETE' });
      showMessage(`Revoked ${code}.`, 'success');
      await loadCoupons();
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  function init() {
    if (!DctechAuth.requireAdmin()) return;
    el('status-filter').addEventListener('change', loadBookings);
    el('booking-rows').addEventListener('click', onBookingAction);
    el('manual-form').addEventListener('submit', onManualBooking);
    el('mint-form').addEventListener('submit', onMint);
    el('coupon-rows').addEventListener('click', onRevoke);
    loadBookings();
    loadCoupons();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
