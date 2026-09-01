(function() {
  // Fields the public correction form knows how to render, in display order.
  // Which of these actually show for a given target comes from the server's
  // `correctable_fields` (routes/corrections.py get_public_event_json /
  // get_public_recurring_json) — this list is just "every field this form
  // has UI for." Recurring targets never include end_time (no such field on
  // a recurring definition), so that group simply never shows for them —
  // no extra branching needed here.
  const KNOWN_FIELDS = ['description', 'location', 'url', 'time', 'end_time'];

  let currentTarget = null;
  let target = null;

  const { setResponse, readMagicToken, stripTokenFromUrl, focusFirstField, requestLink: requestMagicLink } = DctechMagicLink;

  // ---- Which of the three targets is this page for? ----
  // ?guid=...                    -> a single event
  // ?recurring=slug              -> the recurring series' own definition
  // ?recurring=slug&date=...     -> one occurrence of that series

  function readTarget() {
    const params = new URLSearchParams(window.location.search);
    const guid = params.get('guid');
    if (guid) return { targetType: 'event', targetId: guid, targetDate: null };
    const recurring = params.get('recurring');
    if (recurring) {
      const date = params.get('date');
      return {
        targetType: date ? 'recurring_instance' : 'recurring_series',
        targetId: recurring,
        targetDate: date || null,
      };
    }
    return null;
  }

  function redirectPathFor(t) {
    if (t.targetType === 'event') {
      return `/edit/correct-event.html?guid=${encodeURIComponent(t.targetId)}`;
    }
    const dateParam = t.targetDate ? `&date=${encodeURIComponent(t.targetDate)}` : '';
    return `/edit/correct-event.html?recurring=${encodeURIComponent(t.targetId)}${dateParam}`;
  }

  // ---- Magic link ---- (identical mechanics to submission.js — a
  // correction submitter proves control of their email the same way a new
  // event submitter does. Shared via magic-link.js; only the extra
  // redirect_path field is specific to this page.)
  let magicToken = null;

  function requestLink(event) {
    return requestMagicLink(event, { extraBody: () => ({ redirect_path: redirectPathFor(target) }) });
  }

  // ---- The target and its correctable fields ----

  async function fetchTarget(t) {
    if (t.targetType === 'event') {
      const response = await fetch(
        DctechEditConfig.apiUrl(`/api/public/events/${encodeURIComponent(t.targetId)}`));
      if (response.status === 404) return null;
      if (!response.ok) throw new Error('Could not load that event. Please try again.');
      return response.json();
    }

    const qs = t.targetDate ? `?date=${encodeURIComponent(t.targetDate)}` : '';
    const response = await fetch(DctechEditConfig.apiUrl(
      `/api/public/recurring/${encodeURIComponent(t.targetId)}${qs}`));
    if (response.status === 404) return null;
    if (!response.ok) {
      throw new Error('Could not load that recurring event. Please try again.');
    }
    const payload = await response.json();
    const values = (t.targetType === 'recurring_instance' && payload.instance)
      ? payload.instance
      : payload.series;
    return { correctable_fields: payload.correctable_fields, ...values };
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

  function renderForm(values) {
    currentTarget = values;
    document.getElementById('event-title-heading').textContent = values.title || 'This event';

    const correctable = new Set(values.correctable_fields || []);
    KNOWN_FIELDS.forEach((field) => {
      if (correctable.has(field)) {
        renderField(field, values[field]);
      }
    });

    if (!correctable.has('time')) {
      const locked = document.getElementById('field-time-locked');
      const value = document.getElementById('field-time-locked-value');
      if (locked && value) {
        const when = [values.date, values.time].filter(Boolean).join(' ');
        value.textContent = when || '(see the event page)';
        locked.classList.remove('hidden');
      }
    }

    const seriesToggle = document.getElementById('series-toggle');
    if (seriesToggle) {
      seriesToggle.classList.toggle('hidden', target.targetType !== 'recurring_instance');
    }

    const correctionForm = document.getElementById('correction-form');
    correctionForm.classList.remove('hidden');
    // Arriving fresh off an emailed magic link, focus is otherwise nowhere
    // in particular after this page-state swap reveals the form.
    if (magicToken) focusFirstField(correctionForm);
  }

  function collectChangedFields() {
    const fields = {};
    const correctable = new Set((currentTarget && currentTarget.correctable_fields) || []);
    KNOWN_FIELDS.forEach((field) => {
      if (!correctable.has(field)) return;
      const input = document.getElementById(`field-${field}-input`);
      if (!input) return;
      const value = input.value.trim();
      const original = (currentTarget[field] || '').toString().trim();
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
      const seriesToggle = document.getElementById('apply-to-series');
      const effectiveType =
        (target.targetType === 'recurring_instance' && seriesToggle && seriesToggle.checked)
          ? 'recurring_series'
          : target.targetType;

      const body = { target_type: effectiveType, target_id: target.targetId, fields, reason };
      if (effectiveType === 'recurring_instance') {
        body.target_date = target.targetDate;
      }
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
    target = readTarget();
    if (!target) {
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

    let values;
    try {
      values = await fetchTarget(target);
    } catch (err) {
      setResponse(document.getElementById('form-response'), err.message, true);
      return;
    }

    if (!values) {
      document.getElementById('not-correctable').classList.remove('hidden');
      return;
    }

    renderForm(values);
    document.getElementById('correction-form')
      .addEventListener('submit', handleSubmit);
  }

  window.DctechCorrectionPage = {
    init: initCorrectionPage,
  };
})();
