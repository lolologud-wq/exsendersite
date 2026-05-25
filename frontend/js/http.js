/**
 * CSRF-safe fetch — token from /api/auth/csrf JSON (cookie as fallback).
 */
(function (global) {
  let csrfMem = "";

  function readCsrfCookie() {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function getCsrf() {
    return csrfMem || readCsrfCookie();
  }

  let csrfPromise = null;

  async function ensureCsrf() {
    if (csrfMem) return csrfMem;
    const fromCookie = readCsrfCookie();
    if (fromCookie) {
      csrfMem = fromCookie;
      return csrfMem;
    }
    if (csrfPromise) return csrfPromise;
    csrfPromise = (async () => {
      const r = await fetch("/api/auth/csrf", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error("csrf fetch failed");
      const data = await r.json().catch(() => ({}));
      csrfMem = data.csrf || readCsrfCookie() || "";
      return csrfMem;
    })();
    try {
      return await csrfPromise;
    } finally {
      csrfPromise = null;
    }
  }

  async function secureFetch(url, opts) {
    const options = { ...(opts || {}), credentials: "same-origin", cache: "no-store" };
    const method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      await ensureCsrf();
      options.headers = { ...(options.headers || {}) };
      const tok = getCsrf();
      if (tok) options.headers["X-CSRF-Token"] = tok;
    }
    return fetch(url, options);
  }

  global.readCsrf = getCsrf;
  global.ensureCsrf = ensureCsrf;
  global.secureFetch = secureFetch;
})(typeof window !== "undefined" ? window : globalThis);
