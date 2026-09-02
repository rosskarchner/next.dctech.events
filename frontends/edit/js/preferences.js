(function() {
  // Subscriber category/region newsletter preferences — account-free, like
  // event submission/correction: a subscriber proves control of their
  // email by clicking an emailed magic link (purpose='prefs' server-side),
  // no Cognito account involved. Unlike correction.js, there's no
  // logged-in-admin fallback here — a Cognito login isn't a newsletter
  // subscription, so the magic link is the only way in.

  const { setResponse, readMagicToken, stripTokenFromUrl, focusFirstField, requestLink: requestMagicLink } = DctechMagicLink;

  let magicToken = null;
  let selected = { categories: [], regions: [] };

  function requestLink(event) {
    return requestMagicLink(event, { extraBody: () => ({ redirect_path: '/edit/preferences.html' }) });
  }

  function renderCheckboxes(containerId, name, items, selectedSlugs) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const selectedSet = new Set(selectedSlugs);
    container.innerHTML = '';
    Object.entries(items)
      .sort((a, b) => a[1].name.localeCompare(b[1].name))
      .forEach(([slug, item]) => {
        const label = document.createElement('label');
        const checked = selectedSet.has(slug) ? ' checked' : '';
        label.innerHTML = `<input type="checkbox" name="${name}" value="${slug}"${checked}> ${item.name}`;
        container.appendChild(label);
      });
  }

  function collectSelected(name) {
    return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
      .map((el) => el.value);
  }

  async function fetchPreferences(token) {
    const url = DctechEditConfig.apiUrl(
      `/api/preferences?e=${encodeURIComponent(token.e)}&t=${encodeURIComponent(token.t)}&s=${encodeURIComponent(token.s)}`);
    const response = await fetch(url);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || 'Could not load your preferences. Please request a new link.');
    }
    return payload;
  }

  function renderForm(data) {
    selected = { categories: data.categories || [], regions: data.regions || [] };
    renderCheckboxes('category-checkboxes', 'categories', data.all_categories || {}, selected.categories);
    renderCheckboxes('region-checkboxes', 'regions', data.all_regions || {}, selected.regions);

    const banner = document.getElementById('editing-as');
    if (banner && data.email) {
      banner.textContent = `Editing preferences for ${data.email}`;
      banner.classList.remove('hidden');
    }

    const form = document.getElementById('preferences-form');
    form.classList.remove('hidden');
    // Arriving fresh off an emailed magic link, focus is otherwise nowhere
    // in particular after this page-state swap reveals the form.
    focusFirstField(form);
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const responseArea = document.getElementById('form-response');
    const submitButton = document.getElementById('submit-btn');
    submitButton.disabled = true;

    try {
      const body = {
        mlt_e: magicToken.e, mlt_t: magicToken.t, mlt_s: magicToken.s,
        categories: collectSelected('categories'),
        regions: collectSelected('regions'),
      };
      const response = await fetch(DctechEditConfig.apiUrl('/api/preferences'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || 'Failed to save your preferences.');
      }
      setResponse(responseArea, 'Saved — your preferences have been updated.', false);
    } catch (err) {
      setResponse(responseArea, err.message, true);
    } finally {
      submitButton.disabled = false;
    }
  }

  function showLinkRequest() {
    document.getElementById('link-request').classList.remove('hidden');
  }

  async function initPreferencesPage() {
    magicToken = readMagicToken();
    if (!magicToken) {
      showLinkRequest();
      const linkForm = document.getElementById('link-form');
      if (linkForm) linkForm.addEventListener('submit', requestLink);
      return;
    }
    stripTokenFromUrl();

    let data;
    try {
      data = await fetchPreferences(magicToken);
    } catch (err) {
      setResponse(document.getElementById('form-response'), err.message, true);
      showLinkRequest();
      const linkForm = document.getElementById('link-form');
      if (linkForm) linkForm.addEventListener('submit', requestLink);
      return;
    }

    renderForm(data);
    document.getElementById('preferences-form')
      .addEventListener('submit', handleSubmit);
  }

  window.DctechPreferencesPage = {
    init: initPreferencesPage,
  };
})();
