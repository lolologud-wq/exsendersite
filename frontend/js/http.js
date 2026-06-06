/**
 * CSRF-safe fetch — token from /api/auth/csrf JSON (cookie as fallback).
 */
(function (global) {
  let csrfMem = "";

  function readCsrfCookie() {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function syncCsrfFromCookie() {
    const fromCookie = readCsrfCookie();
    if (fromCookie) csrfMem = fromCookie;
    return fromCookie;
  }

  function getCsrf() {
    return csrfMem || readCsrfCookie();
  }

  function setCsrfToken(token) {
    csrfMem = String(token || "").trim();
  }

  let csrfPromise = null;

  async function fetchCsrfToken() {
    const r = await fetch("/api/auth/csrf", { credentials: "same-origin", cache: "no-store" });
    if (!r.ok) throw new Error("csrf fetch failed");
    const data = await r.json().catch(() => ({}));
    csrfMem = data.csrf || syncCsrfFromCookie() || "";
    return csrfMem;
  }

  async function ensureCsrf() {
    const synced = syncCsrfFromCookie();
    if (synced) return synced;
    if (csrfMem) return csrfMem;
    if (csrfPromise) return csrfPromise;
    csrfPromise = fetchCsrfToken();
    try {
      return await csrfPromise;
    } finally {
      csrfPromise = null;
    }
  }

  async function refreshCsrf() {
    csrfMem = "";
    csrfPromise = null;
    return fetchCsrfToken();
  }

  async function secureFetch(url, opts, retried) {
    const options = { ...(opts || {}), credentials: "same-origin", cache: "no-store" };
    const method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      await ensureCsrf();
      syncCsrfFromCookie();
      options.headers = { ...(options.headers || {}) };
      let tok = getCsrf();
      if (!tok) {
        await fetchCsrfToken();
        tok = getCsrf();
      }
      if (tok) options.headers["X-CSRF-Token"] = tok;
    }
    const res = await fetch(url, options);
    if (!retried && method !== "GET" && method !== "HEAD" && res.status === 403) {
      const data = await res.clone().json().catch(() => ({}));
      if (data.detail === "csrf validation failed") {
        await refreshCsrf();
        return secureFetch(url, opts, true);
      }
    }
    return res;
  }

  global.readCsrf = getCsrf;
  global.setCsrfToken = setCsrfToken;
  global.syncCsrfFromCookie = syncCsrfFromCookie;
  global.ensureCsrf = ensureCsrf;
  global.refreshCsrf = refreshCsrf;
  global.secureFetch = secureFetch;
})(typeof window !== "undefined" ? window : globalThis);
