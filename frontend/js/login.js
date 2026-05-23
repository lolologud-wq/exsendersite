(function () {
  const form = document.getElementById("loginForm");
  const errEl = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");

  async function checkSession() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "same-origin" });
      const data = await r.json();
      if (data.user) {
        window.location.replace("/");
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
      const r = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });

      if (r.ok) {
        window.location.replace("/");
        return;
      }

      const data = await r.json().catch(() => ({}));
      errEl.textContent = data.detail === "bad credentials"
        ? "Неверный логин или пароль"
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
})();
