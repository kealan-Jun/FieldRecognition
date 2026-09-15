// Session cookies stay HTTP-only. Attach the server-derived CSRF token to mutations.
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin !== location.origin) return originalFetch(input, init);
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      let token = sessionStorage.getItem('field_csrf');
      if (!token) {
        const me = await originalFetch('/api/auth/me');
        if (me.ok) { token = (await me.json()).csrf_token; if (token) sessionStorage.setItem('field_csrf', token); }
      }
      if (token) headers.set('X-CSRF-Token', token);
    }
    const response = await originalFetch(input, { ...init, headers });
    if (response.status === 401 && !url.pathname.startsWith('/api/auth/')) {
      sessionStorage.removeItem('field_csrf'); location.replace('/login');
    }
    return response;
  };
})();
