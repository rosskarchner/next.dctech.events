(function() {
  function detectSiteFromHostname() {
    const host = window.location.hostname;
    if (host === 'dcstem.events' || host === 'www.dcstem.events' || host.includes('dcstem')) {
      return 'dcstem';
    }
    return 'dctech';
  }

  function setSiteField() {
    const site = detectSiteFromHostname();
    const siteField = document.getElementById('site-field');
    if (siteField) {
      siteField.value = site;
    }
  }

  function setResponse(container, message, isError) {
    if (!container) return;
    container.innerHTML = `<div class="message ${isError ? 'message-error' : 'message-success'}"><p>${message}</p></div>`;
  }

  function collectFormData(form) {
    const data = new URLSearchParams();
    const formData = new FormData(form);
    for (const [key, value] of formData.entries()) {
      data.append(key, value);
    }
    return data;
  }

  async function loadCategories() {
    const container = document.getElementById('category-checkboxes');
    if (!container) return;

    try {
      const response = await fetch('/categories.json');
      if (!response.ok) throw new Error('Failed to load categories');
      const categories = await response.json();

      container.innerHTML = '';
      Object.entries(categories)
        .sort((a, b) => a[1].name.localeCompare(b[1].name))
        .forEach(([slug, cat]) => {
          const label = document.createElement('label');
          label.innerHTML = `<input type="checkbox" name="categories" value="${slug}"> ${cat.name}`;
          container.appendChild(label);
        });
    } catch (err) {
      console.error('Error loading categories:', err);
      container.innerHTML = '<span class="error">Failed to load categories. Please try again later.</span>';
    }
  }

  // ---- Magic link ----------------------------------------------------
  // A submitter proves control of their email by clicking a signed link, so
  // the page works with no Cognito session at all. The token rides in the
  // query string; we keep it in memory and replay it with each submission.
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
    // Keep the signed token out of the address bar, browser history, and any
    // Referer sent to a third-party link in the form.
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
        body: JSON.stringify({ email }),
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

  async function handleSubmit(form, typeLabel) {
    const responseArea = document.getElementById('form-response');
    const submitButton = form.querySelector('button[type="submit"]');
    const originalDisabled = submitButton ? submitButton.disabled : false;
    if (submitButton) submitButton.disabled = true;

    try {
      const body = collectFormData(form);
      let response;
      if (magicToken) {
        body.append('mlt_e', magicToken.e);
        body.append('mlt_t', magicToken.t);
        body.append('mlt_s', magicToken.s);
        response = await fetch(DctechEditConfig.apiUrl('/api/submissions'), {
          method: 'POST',
          body,
        });
      } else {
        response = await DctechAuth.authorizedFetch('/api/submissions', {
          method: 'POST',
          body,
        });
      }

      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `Failed to submit ${typeLabel}.`);
      }

      form.reset();
      const extra = payload.subscribed
        ? ' You are also signed up for the weekly newsletter.'
        : '';
      setResponse(responseArea, `Thanks — your ${typeLabel} was submitted for review. Draft ID: ${payload.draft_id}.${extra}`, false);
      if (typeLabel === 'event') {
        loadCategories();
      }
    } catch (err) {
      setResponse(responseArea, err.message, true);
    } finally {
      if (submitButton) submitButton.disabled = originalDisabled;
    }
  }

  function showEventForm(asEmail) {
    const banner = document.getElementById('submitting-as');
    if (banner && asEmail) {
      banner.textContent = `Submitting as ${asEmail}`;
      banner.classList.remove('hidden');
    }
    const linkRequest = document.getElementById('link-request');
    if (linkRequest) linkRequest.classList.add('hidden');
    const eventForm = document.getElementById('event-form');
    if (eventForm) eventForm.classList.remove('hidden');
  }

  function showLinkRequest() {
    const linkRequest = document.getElementById('link-request');
    if (linkRequest) linkRequest.classList.remove('hidden');
    const eventForm = document.getElementById('event-form');
    if (eventForm) eventForm.classList.add('hidden');
  }

  function initSubmissionPage() {
    setSiteField();

    const linkForm = document.getElementById('link-form');
    if (linkForm) linkForm.addEventListener('submit', requestLink);

    magicToken = readMagicToken();
    if (magicToken) {
      stripTokenFromUrl();
      showEventForm(magicToken.email);
    } else if (DctechAuth.isAuthenticated()) {
      const info = DctechAuth.getUserInfo ? DctechAuth.getUserInfo() : null;
      showEventForm(info && info.email ? info.email : null);
    } else {
      // No token and no session: ask for an email rather than bouncing the
      // visitor to a Cognito login they cannot even sign up for.
      showLinkRequest();
      return;
    }

    const eventForm = document.getElementById('event-form');
    if (eventForm) {
      loadCategories();
      eventForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        await handleSubmit(eventForm, 'event');
      });
    }

    const groupForm = document.getElementById('group-form');
    if (groupForm) {
      groupForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        await handleSubmit(groupForm, 'group');
      });
    }
  }

  window.DctechSubmissionPage = {
    init: initSubmissionPage,
  };
})();
