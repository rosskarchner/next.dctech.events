(function() {
  // Public booking form. No sign-in: the API holds the week for 30 minutes
  // and hands back the fields for a PayPal Buy Now form, which this page
  // submits; PayPal's IPN confirms the payment server-side. A coupon skips
  // PayPal entirely.
  let price = null;

  function el(id) {
    return document.getElementById(id);
  }

  function showMessage(text, kind) {
    el('sponsor-message').innerHTML = text
      ? `<div class="message message-${kind}"><p>${DctechUtil.escapeHtml(text)}</p></div>`
      : '';
    if (text) el('sponsor-message').scrollIntoView({ behavior: 'smooth' });
  }

  function weekLabel(week) {
    const start = new Date(`${week.week_start}T12:00:00`);
    const opts = { month: 'short', day: 'numeric', year: 'numeric' };
    return `Week of ${start.toLocaleDateString('en-US', opts)}`;
  }

  async function loadAvailability() {
    const select = el('week');
    try {
      const response = await fetch(DctechEditConfig.apiUrl('/api/sponsorships/availability'));
      const data = await response.json();
      price = data.price_usd && data.price_usd !== '0' ? data.price_usd : null;
      const wanted = new URLSearchParams(window.location.search).get('week');
      const open = (data.weeks || []).filter((w) => w.available);
      select.innerHTML = open.length
        ? '<option value="">Choose a week…</option>' + open.map((w) =>
            `<option value="${DctechUtil.escapeHtml(w.week_id)}"${w.week_id === wanted ? ' selected' : ''}>` +
            `${DctechUtil.escapeHtml(weekLabel(w))}</option>`).join('')
        : '<option value="">No weeks are open right now</option>';
      updateButton();
    } catch (error) {
      select.innerHTML = '<option value="">Could not load weeks</option>';
      showMessage('Could not load available weeks. Please try again later.', 'error');
    }
  }

  function updateButton() {
    const coupon = el('coupon').value.trim();
    el('submit-button').textContent = coupon ? 'Book with coupon' : 'Continue to PayPal';
    el('price-note').textContent = coupon
      ? ''
      : (price ? `A week costs $${price}. You'll pay on PayPal's site.` : 'Sponsorships are by coupon only right now.');
  }

  function updatePreview() {
    el('preview-name').textContent = el('name').value.trim() || 'Acme Corp';
    el('preview-message').textContent = el('message').value.trim() || 'your message';
    document.querySelectorAll('.counter').forEach((counter) => {
      const input = el(counter.dataset.for);
      const max = Number(counter.dataset.max);
      counter.textContent = `${input.value.length}/${max}`;
      counter.classList.toggle('over', input.value.length > max);
    });
  }

  function postToPayPal(url, fields) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = url;
    Object.entries(fields).forEach(([name, value]) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  async function submit(event) {
    event.preventDefault();
    const form = el('sponsor-form');
    const body = Object.fromEntries(new FormData(form).entries());
    body.coupon_code = (body.coupon_code || '').trim();
    const button = el('submit-button');
    button.disabled = true;
    showMessage('', '');

    try {
      const response = await fetch(DctechEditConfig.apiUrl('/api/sponsorships'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `Request failed (HTTP ${response.status})`);
      }
      if (data.paypal_url) {
        showMessage('Taking you to PayPal…', 'info');
        postToPayPal(data.paypal_url, data.paypal_fields);
        return;
      }
      form.classList.add('hidden');
      showMessage(
        "You're booked! We'll review your message and email you to confirm before your week starts.",
        'success');
    } catch (error) {
      showMessage(error.message, 'error');
      button.disabled = false;
      if (/booked/.test(error.message)) loadAvailability();
    }
  }

  function showReturnStatus() {
    const status = new URLSearchParams(window.location.search).get('status');
    if (status === 'returned') {
      el('sponsor-form').classList.add('hidden');
      showMessage(
        "Thanks! Once PayPal confirms your payment we'll email you a receipt. "
        + "We review every sponsorship and will confirm by email before your week starts.",
        'success');
    } else if (status === 'cancelled') {
      showMessage('Payment cancelled. Your week is held for a few more minutes if you want to try again.', 'warning');
    }
  }

  function init() {
    ['name', 'message'].forEach((id) => el(id).addEventListener('input', updatePreview));
    el('coupon').addEventListener('input', updateButton);
    el('sponsor-form').addEventListener('submit', submit);
    updatePreview();
    showReturnStatus();
    loadAvailability();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
