(function() {
  // Fields the public correction form knows how to render, in display order.
  // Which of these actually show for a given event comes from the server's
  // `correctable_fields` (routes/corrections.py:get_public_event_json) — this
  // list is just "every field this form has UI for."
  const KNOWN_FIELDS = ['description', 'location', 'url', 'time', 'end_time'];

  let currentEvent = null;
  let guid = null;

  function setResponse(container, message, isError) {
    if (!container) return;
    container.innerHTML = `<div class="message ${isError ? 'message-error' : 'message-success'}"><p>${message}</p></div>`;
  }

  function readGuid() {
    const params = new URLSearchParams(window.location.search);
    return params.get('guid') || '';
  }

  // ---- Magic link ---- (identical mechanics to submission.js — a
  // correction submitter proves control of their email the same way a new
  // event submitter does.)
  let magicToken = null;

  function readMagicToken() {
    const params = new URLSearchParams(window.location.search);
    const e = params.get('e');
    const t = params.get('t');
    const s = params.get('s');
    if (!e || !t || !s) return null;
    let email = '';
    try {
      email = atob(e.replace(/-/g, '+').replace(/_/g, '/'));
    } catch {
      return null;
    }
    return { e, t, s, email };
  }

  function stripTokenFromUrl() {
    const url = new URL(window.location.href);
    ['e', 't', 's'].forEach((k) => url.searchParams.delete(k));
    window.history.replaceState({}, document.title, url.pathname + url.search);
  }

  async function requestLink(event) {
    event.preventDefault();
    const responseArea = document.getElementById('form-response');
    const button = document.getElementById('link-btn');
    const email = document.getElementById('link-email').value.trim();
    if (!email) return;

    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = 'Sending…';

    try {
      const response = await fetch(DctechEditConfig.apiUrl('/api/submit-link'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          redirect_path: `/edit/correct-event.html?guid=${encodeURIComponent(guid)}`,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || 'Could not send the link. Please try again.');
      }
      setResponse(responseArea, payload.message, false);
      document.getElementById('link-form').reset();
    } catch (err) {
      setResponse(responseArea, err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  // ---- The event and its correctable fields ----

  async function fetchEvent() {
    const response = await fetch(
      DctechEditConfig.apiUrl(`/api/public/events/${encodeURIComponent(guid)}`));
    if (response.status === 404) return null;
    if (!response.ok) throw new Error('Could not load that event. Please try again.');
    return response.json();
  }

  function renderField(field, current) {
    const group = document.getElementById(`field-${field}`);
    const input = document.getElementById(`field-${field}-input`);
    const currentLabel = document.getElementById(`field-${field}-current`);
    if (!group || !input) return;
    group.classList.remove('hidden');
    currentLabel.textContent = current ? `Currently: ${current}` : 'Currently: (not set)';
    if (field === 'time' || field === 'end_time') {
      input.value = current || '';
    }
  }

  function renderForm(eventData) {
    currentEvent = eventData;
    document.getElementById('event-title-heading').textContent = eventData.title || 'This event';

    const correctable = new Set(eventData.correctable_fields || []);
    KNOWN_FIELDS.forEach((field) => {
      if (correctable.has(field)) {
        renderField(field, eventData[field]);
      }
    });

    if (!correctable.has('time')) {
      const locked = document.getElementById('field-time-locked');
      const value = document.getElementById('field-time-locked-value');
      if (locked && value) {
        const when = [eventData.date, eventData.time].filter(Boolean).join(' ');
        value.textContent = when || '(see the event page)';
        locked.classList.remove('hidden');
      }
    }

    document.getElementById('correction-form').classList.remove('hidden');
  }

  function collectChangedFields() {
    const fields = {};
    const correctable = new Set((currentEvent && currentEvent.correctable_fields) || []);
    KNOWN_FIELDS.forEach((field) => {
      if (!correctable.has(field)) return;
      const input = document.getElementById(`field-${field}-input`);
      if (!input) return;
      const value = input.value.trim();
      const original = (currentEvent[field] || '').toString().trim();
      if (value && value !== original) {
        fields[field] = value;
      }
    });
    return fields;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const responseArea = document.getElementById('form-response');
    const submitButton = document.getElementById('submit-btn');

    const fields = collectChangedFields();
    if (Object.keys(fields).length === 0) {
      setResponse(responseArea, 'Change at least one field before submitting.', true);
      return;
    }

    const reason = document.getElementById('reason').value.trim();
    if (!reason) {
      setResponse(responseArea, 'Please say what is wrong and how you know.', true);
      return;
    }

    submitButton.disabled = true;

    try {
      const body = { guid, fields, reason };
      if (magicToken) {
        body.mlt_e = magicToken.e;
        body.mlt_t = magicToken.t;
        body.mlt_s = magicToken.s;
      }

      const response = magicToken
        ? await fetch(DctechEditConfig.apiUrl('/api/corrections'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          })
        : await DctechAuth.authorizedFetch('/api/corrections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });

      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || 'Failed to submit correction.');
      }

      document.getElementById('correction-form').classList.add('hidden');
      setResponse(responseArea, 'Thanks — this suggestion was submitted for review.', false);
    } catch (err) {
      setResponse(responseArea, err.message, true);
    } finally {
      submitButton.disabled = false;
    }
  }

  function showLinkRequest() {
    document.getElementById('link-request').classList.remove('hidden');
  }

  function showFormAs(asEmail) {
    const banner = document.getElementById('submitting-as');
    if (banner && asEmail) {
      banner.textContent = `Submitting as ${asEmail}`;
      banner.classList.remove('hidden');
    }
    document.getElementById('link-request').classList.add('hidden');
  }

  async function initCorrectionPage() {
    guid = readGuid();
    if (!guid) {
      document.getElementById('no-guid').classList.remove('hidden');
      return;
    }

    magicToken = readMagicToken();
    if (magicToken) {
      stripTokenFromUrl();
      showFormAs(magicToken.email);
    } else if (DctechAuth.isAuthenticated()) {
      const info = DctechAuth.getUserInfo ? DctechAuth.getUserInfo() : null;
      showFormAs(info && info.email ? info.email : null);
    } else {
      showLinkRequest();
      const linkForm = document.getElementById('link-form');
      if (linkForm) linkForm.addEventListener('submit', requestLink);
      return;
    }

    let eventData;
    try {
      eventData = await fetchEvent();
    } catch (err) {
      setResponse(document.getElementById('form-response'), err.message, true);
      return;
    }

    if (!eventData) {
      document.getElementById('not-correctable').classList.remove('hidden');
      return;
    }

    renderForm(eventData);
    document.getElementById('correction-form')
      .addEventListener('submit', handleSubmit);
  }

  window.DctechCorrectionPage = {
    init: initCorrectionPage,
  };
})();
