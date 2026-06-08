(function () {
  const form = document.getElementById("loginForm");
  const errEl = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");
  if (!form || !errEl || !btn) return;

  let nextPath = "";
  try {
    nextPath = new URL(window.location.href).searchParams.get("next") || "";
    if (nextPath && !nextPath.startsWith("/")) nextPath = "";
  } catch (_) { /* ignore */ }

  function defaultRedirect(kind) {
    if (nextPath) return nextPath;
    const host = (window.location.hostname || "").toLowerCase();
    if (host.startsWith("inviter.")) return "/inviter";
    return kind === "admin" ? "/admin" : "/app";
  }

  async function checkSession() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "same-origin" });
      const data = await r.json();
      if (data.user) {
        window.location.replace(defaultRedirect(data.kind));
      }
    } catch (_) {
      /* ignore */
    }
  }

  const btnLabel = btn.querySelector("span") || btn;
  const btnText = btnLabel.textContent;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.hidden = true;
    errEl.textContent = "";
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btnLabel.textContent = "Вход…";

    const login = document.getElementById("login").value.trim();
    const password = document.getElementById("password").value;

    try {
      const r = await secureFetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });

      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        window.location.replace(nextPath || data.redirect || defaultRedirect(data.kind));
        return;
      }
      errEl.textContent = data.detail === "bad credentials"
        ? "Неверный email или пароль"
        : data.detail === "csrf validation failed"
          ? "Ошибка безопасности — обновите страницу (F5) и попробуйте снова"
          : (data.detail || data.error || "Ошибка входа");
      errEl.hidden = false;
    } catch (_) {
      errEl.textContent = "Сервер недоступен";
      errEl.hidden = false;
    } finally {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btnLabel.textContent = btnText;
    }
  });

  checkSession();
  ensureCsrf().catch(() => {});
})();
