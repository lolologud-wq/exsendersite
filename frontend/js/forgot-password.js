(function () {
  let token = "";
  let pollTimer = null;

  try {
    token = new URL(window.location.href).searchParams.get("token") || "";
  } catch (_) { /* ignore */ }

  const stepEmail = document.getElementById("fpStepEmail");
  const stepBot = document.getElementById("fpStepBot");
  const stepPass = document.getElementById("fpStepPass");
  const errEl = document.getElementById("fpError");
  const passErrEl = document.getElementById("fpPassError");
  const botStatus = document.getElementById("fpBotStatus");

  function showErr(el, msg) {
    if (!el) return;
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  function showStep(name) {
    stepEmail.hidden = name !== "email";
    stepBot.hidden = name !== "bot";
    stepPass.hidden = name !== "pass";
  }

  async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await secureFetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(typeof data.detail === "string" ? data.detail : (data.error || `HTTP ${r.status}`));
    }
    return data;
  }

  async function pollStatus() {
    if (!token) return;
    try {
      const res = await fetch(`/api/auth/password-reset/status?token=${encodeURIComponent(token)}`, {
        credentials: "same-origin",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "status error");
      if (data.status === "verified") {
        if (pollTimer) clearInterval(pollTimer);
        showStep("pass");
        return;
      }
      if (data.status === "expired" || data.status === "completed") {
        if (pollTimer) clearInterval(pollTimer);
        botStatus.textContent = data.status === "completed"
          ? "Пароль уже изменён. Можно войти."
          : "Время истекло — запросите сброс заново.";
      }
    } catch (e) {
      console.warn("reset poll", e);
    }
  }

  document.getElementById("fpEmailForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(errEl, "");
    const email = document.getElementById("fpEmail")?.value.trim();
    try {
      const res = await api("POST", "/api/auth/password-reset/start", { email });
      token = res.token || token;
      if (res.botUrl) {
        document.getElementById("fpBotLink").href = res.botUrl;
        showStep("bot");
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollStatus, 2500);
        pollStatus();
      } else {
        showErr(errEl, res.message || "Проверьте почту и Telegram.");
      }
    } catch (err) {
      showErr(errEl, err.message);
    }
  });

  document.getElementById("fpPassForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(passErrEl, "");
    const p1 = document.getElementById("fpPassword")?.value || "";
    const p2 = document.getElementById("fpPassword2")?.value || "";
    if (p1 !== p2) {
      showErr(passErrEl, "Пароли не совпадают");
      return;
    }
    try {
      await api("POST", "/api/auth/password-reset/complete", { token, password: p1 });
      window.location.href = "/login";
    } catch (err) {
      showErr(passErrEl, err.message);
    }
  });

  ensureCsrf().catch(() => {});

  if (token) {
    pollStatus().then(() => {
      if (!stepPass.hidden) return;
      showStep("bot");
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollStatus, 2500);
    });
  }
})();
