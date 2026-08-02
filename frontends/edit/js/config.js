(function() {
  // {{API_BASE}} and {{COGNITO_CLIENT_ID}} are substituted at deploy time
  // by scripts/deploy_edit_ui.sh — see next-architecture-plan.md.
  const EXECUTE_API_BASE = '{{API_BASE}}';
  const host = window.location.hostname;
  const onMainSite = host === 'dctech.events' || host === 'www.dctech.events';
  const appBasePath = onMainSite ? '/edit/' : '/';
  const apiBaseUrl = onMainSite ? EXECUTE_API_BASE : '';

  function ensureLeadingSlash(path) {
    return path.startsWith('/') ? path : `/${path}`;
  }

  function trimTrailingSlash(path) {
    return path.endsWith('/') ? path.slice(0, -1) : path;
  }

  function appUrl(path) {
    const normalized = path ? path.replace(/^\/+/, '') : '';
    return `${appBasePath}${normalized}`;
  }

  function apiUrl(path) {
    const normalized = ensureLeadingSlash(path);
    return apiBaseUrl ? `${trimTrailingSlash(apiBaseUrl)}${normalized}` : normalized;
  }

  window.DctechEditConfig = {
    appBasePath,
    apiBaseUrl,
    authCallbackPath: appUrl('auth/callback.html'),
    appHomePath: appBasePath,
    appUrl,
    apiUrl,
  };
})();
