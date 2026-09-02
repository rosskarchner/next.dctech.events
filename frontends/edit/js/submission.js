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

  const { setResponse, readMagicToken, stripTokenFromUrl, focusFirstField, requestLink: requestMagicLink } = DctechMagicLink;

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
  // Shared with correction.js via magic-link.js.
  let magicToken = null;

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

  // Whichever of the two exists on this page — submit-event.html and
  // submit-group.html each have exactly one, never both.
  function submissionForm() {
    return document.getElementById('event-form') || document.getElementById('group-form');
  }

  function showSubmissionForm(asEmail, { moveFocus } = {}) {
    const banner = document.getElementById('submitting-as');
    if (banner && asEmail) {
      banner.textContent = `Submitting as ${asEmail}`;
      banner.classList.remove('hidden');
    }
    const linkRequest = document.getElementById('link-request');
    if (linkRequest) linkRequest.classList.add('hidden');
    const form = submissionForm();
    if (form) {
      form.classList.remove('hidden');
      if (moveFocus) focusFirstField(form);
    }
  }

  function showLinkRequest() {
    const linkRequest = document.getElementById('link-request');
    if (linkRequest) linkRequest.classList.remove('hidden');
    const form = submissionForm();
    if (form) form.classList.add('hidden');
  }

  function initSubmissionPage() {
    setSiteField();

    const linkForm = document.getElementById('link-form');
    if (linkForm) linkForm.addEventListener('submit', requestMagicLink);

    magicToken = readMagicToken();
    if (magicToken) {
      stripTokenFromUrl();
      showSubmissionForm(magicToken.email, { moveFocus: true });
    } else if (DctechAuth.isAuthenticated()) {
      const info = DctechAuth.getUserInfo ? DctechAuth.getUserInfo() : null;
      showSubmissionForm(info && info.email ? info.email : null);
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
