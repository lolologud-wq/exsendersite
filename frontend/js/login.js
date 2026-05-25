(function () {
  const form = document.getElementById("loginForm");
  const errEl = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");

  async function checkSession() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "same-origin" });
      const data = await r.json();
      if (data.user) {
        window.location.replace(data.kind === "admin" ? "/admin" : "/app");
      }
    } catch (_) {
      /* ignore */
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.hidden = true;
    errEl.textContent = "";
    btn.disabled = true;

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
        window.location.replace(data.redirect || "/app");
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
    }
  });

  checkSession();
  ensureCsrf().catch(() => {});
})();
