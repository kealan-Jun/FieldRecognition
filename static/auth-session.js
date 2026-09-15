// Direct access is the default. Only attach session CSRF when login is enabled.
(() => {
  const originalFetch = window.fetch.bind(window);
  let session;
  const getSession = () => {
    if (!session) {
      session = originalFetch('/api/auth/me').then(async response => {
        if (response.status === 401) return { auth_enabled: true };
        if (!response.ok) throw new Error('无法读取访问设置');
        const state = await response.json();
        if (state.auth_enabled === false) sessionStorage.removeItem('field_csrf');
        return state;
      }).catch(error => { session = undefined; throw error; });
    }
    return session;
  };
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin !== location.origin) return originalFetch(input, init);
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const state = await getSession();
      if (state.auth_enabled !== false && state.csrf_token) headers.set('X-CSRF-Token', state.csrf_token);
    }
    const response = await originalFetch(input, { ...init, headers });
    if (response.status === 401 && !url.pathname.startsWith('/api/auth/')) {
      if ((await getSession()).auth_enabled !== false) {
        sessionStorage.removeItem('field_csrf'); location.replace('/login');
      }
    }
    return response;
  };
})();
