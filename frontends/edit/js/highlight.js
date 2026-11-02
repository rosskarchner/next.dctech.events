(function() {
  // Public highlight checkout. No sign-in: the API quotes the event from the
  // published calendar, holds it for 30 minutes, and hands back the fields
  // for a PayPal Buy Now form, which this page submits. PayPal's IPN makes
  // the highlight live server-side; this page never decides that.
  const HOLD_KEY = 'dctech-highlight-hold';
  const params = new URLSearchParams(window.location.search);
  const guid = params.get('guid') || '';
  let quote = null;

  function el(id) {
    return document.getElementById(id);
  }

  function esc(text) {
    return DctechUtil.escapeHtml(text == null ? '' : String(text));
  }

  function savedHold() {
    try {
      return JSON.parse(sessionStorage.getItem(HOLD_KEY) || 'null');
    } catch {
      return null;
    }
  }

  function saveHold(targetKey, bookingId) {
    try {
      sessionStorage.setItem(HOLD_KEY, JSON.stringify({ target_key: targetKey, booking_id: bookingId }));
    } catch {
      // Storage blocked: a retry within 30 minutes just reports the event as taken.
    }
  }

  function showMessage(text, kind) {
    el('highlight-message').innerHTML = text
      ? `<div class="message message-${kind}"><p>${esc(text)}</p></div>`
      : '';
  }

  function longDate(iso) {
    if (!iso) return '';
    return new Date(`${iso}T12:00:00`).toLocaleDateString('en-US',
      { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  }

  function shortDate(iso) {
    return new Date(`${iso}T12:00:00`).toLocaleDateString('en-US',
      { weekday: 'short', month: 'short', day: 'numeric' });
  }

  function timeLabel(hhmm) {
    if (!hhmm) return '';
    const [h, m] = hhmm.split(':').map(Number);
    const suffix = h < 12 ? 'am' : 'pm';
    return `${h % 12 || 12}${m ? `:${String(m).padStart(2, '0')}` : ''}${suffix}`;
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  function renderEvent(event) {
    const when = [longDate(event.date), timeLabel(event.time)].filter(Boolean).join(', ');
    el('event-card').innerHTML =
      `<h2><a href="${esc(event.url)}" target="_blank" rel="noopener noreferrer">${esc(event.title)}</a></h2>` +
      `<p>${esc(when)}</p>` +
      (event.location ? `<p>${esc(event.location)}</p>` : '') +
      (event.group ? `<p>${esc(event.group)}</p>` : '');
    el('event-card').classList.remove('hidden');
  }

  function renderTerms(q) {
    const newsletters = q.newsletters === 0
      ? 'no more Monday newsletters (the last one before the event has gone out)'
      : `${plural(q.newsletters, 'Monday newsletter')} (to subscribers whose preferences match)`;
    el('terms').innerHTML =
      `Shows from <strong>${esc(shortDate(q.start_date))}</strong> to ` +
      `<strong>${esc(shortDate(q.end_date))}</strong>: ` +
      `${esc(plural(q.days, 'day'))} on the site, in ${esc(newsletters)}. ` +
      'Labeled <span class="pick-label">Paid</span>. Live as soon as PayPal confirms. ' +
      'One occurrence only.';
    el('submit-button').textContent = `Pay $${q.price_usd} with PayPal`;
    el('highlight-form').classList.remove('hidden');
  }

  async function loadQuote() {
    if (!guid) {
      showMessage('No event chosen. Open an event on the calendar and choose "Highlight this event".', 'error');
      return;
    }
    try {
      const response = await fetch(DctechEditConfig.apiUrl(
        `/api/highlights/quote?guid=${encodeURIComponent(guid)}`));
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Could not load this event (HTTP ${response.status})`);
      quote = data;
      renderEvent(data.event);
      if (!data.eligible) {
        showMessage(data.reason || "This event can't be highlighted.", 'warning');
        return;
      }
      if (!data.price_usd || data.price_usd === '0') {
        showMessage('Highlights are not on sale right now.', 'warning');
        return;
      }
      renderTerms(data);
    } catch (error) {
      showMessage(error.message, 'error');
    }
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
    const body = { guid, contact_email: el('email').value.trim() };
    const hold = savedHold();
    if (hold && quote && hold.target_key === quote.target_key) body.replace_booking_id = hold.booking_id;
    const button = el('submit-button');
    button.disabled = true;
    showMessage('', '');
    try {
      const response = await fetch(DctechEditConfig.apiUrl('/api/highlights'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status})`);
      saveHold(data.target_key, data.booking_id);
      showMessage('Taking you to PayPal…', 'info');
      postToPayPal(data.paypal_url, data.paypal_fields);
    } catch (error) {
      showMessage(error.message, 'error');
      button.disabled = false;
    }
  }

  function showReturnStatus() {
    const status = params.get('status');
    if (status === 'returned') {
      showMessage(
        "Thanks! Your highlight goes live as soon as PayPal confirms the payment — "
        + "usually within a few minutes — and we'll email you a receipt.",
        'success');
      return true;
    }
    if (status === 'cancelled') {
      showMessage('Payment cancelled. The event is held for you for a few more minutes if you want to try again.', 'warning');
    }
    return false;
  }

  function init() {
    el('highlight-form').addEventListener('submit', submit);
    const done = showReturnStatus();
    loadQuote().then(() => {
      if (done) el('highlight-form').classList.add('hidden');
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
