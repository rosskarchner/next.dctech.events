(function() {
  // Shared magic-link mechanics for the public submission and correction
  // forms: a visitor proves control of their email by clicking a signed
  // link (no Cognito account needed), the token rides in the query string,
  // and we keep it in memory and replay it with each form submission.

  // #form-response is a live region: role is set to "status" (polite) for
  // a success message, "alert" (assertive) for an error, so a screen-reader
  // user submitting or correcting an event -- a stranger with no other way
  // to tell what happened -- actually hears the outcome.
  function setResponse(container, message, isError) {
    if (!container) return;
    // Set both explicitly (rather than leaning on role="alert"'s implicit
    // assertive live-region semantics) so behavior doesn't depend on how a
    // given screen reader resolves the two when they'd otherwise disagree.
    container.setAttribute('role', isError ? 'alert' : 'status');
    container.setAttribute('aria-live', isError ? 'assertive' : 'polite');
    container.innerHTML = `<div class="message ${isError ? 'message-error' : 'message-success'}"><p>${message}</p></div>`;
  }

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

  // Moves focus to the first focusable field in `container`. Call this right
  // after swapping #link-request out for the real form: the swap itself
  // moves nothing, so a screen-reader or keyboard user -- typically arriving
  // straight from an emailed link, with focus nowhere in particular -- gets
  // no cue the page changed state otherwise.
  function focusFirstField(container) {
    if (!container) return;
    const field = container.querySelector('input, textarea, select, button');
    if (field) field.focus();
  }

  // `extraBody` (plain object, or a function returning one) lets a caller
  // add fields beyond `email` -- e.g. correction.js's redirect_path -- to
  // the /api/submit-link request without duplicating the rest of the
  // request/response handling.
  async function requestLink(event, { extraBody } = {}) {
    event.preventDefault();
    const responseArea = document.getElementById('form-response');
    const button = document.getElementById('link-btn');
    const email = document.getElementById('link-email').value.trim();
    if (!email) return;

    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = 'Sending…';

    try {
      const extra = typeof extraBody === 'function' ? extraBody() : extraBody;
      const body = Object.assign({ email }, extra);
      const response = await fetch(DctechEditConfig.apiUrl('/api/submit-link'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
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

  window.DctechMagicLink = {
    setResponse,
    readMagicToken,
    stripTokenFromUrl,
    focusFirstField,
    requestLink,
  };
})();
