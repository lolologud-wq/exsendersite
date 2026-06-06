(function () {
  const form = document.getElementById("registerForm");
  const verifyPanel = document.getElementById("verifyPanel");
  const errEl = document.getElementById("registerError");
  const verifyErrEl = document.getElementById("verifyError");
  const verifyWaitEl = document.getElementById("verifyWait");
  const verifyBotLink = document.getElementById("verifyBotLink");
  const verifyBotLabel = document.getElementById("verifyBotLabel");
  const verifyBackBtn = document.getElementById("verifyBackBtn");
  const btn = document.getElementById("registerBtn");
  const titleEl = document.getElementById("registerTitle");
  const subEl = document.getElementById("registerSub");

  let presetPlan = "";
  let presetRef = "";
  let pollTimer = null;
  let activeToken = "";

  try {
    const url = new URL(window.location.href);
    presetPlan = url.searchParams.get("plan") || "";
    presetRef = url.searchParams.get("ref") || "";
  } catch (_) { /* ignore */ }

  function showError(el, msg) {
    el.textContent = msg;
    el.hidden = false;
  }

  function clearError(el) {
    el.textContent = "";
    el.hidden = true;
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function animateSwap(hideEl, showEl, onDone) {
    hideEl.classList.remove("register-step--enter");
    hideEl.classList.add("register-step--leave");
    hideEl.addEventListener(
      "animationend",
      () => {
        hideEl.hidden = true;
        hideEl.classList.remove("register-step--leave");
        showEl.hidden = false;
        showEl.classList.add("register-step--enter");
        showEl.addEventListener(
          "animationend",
          () => {
            showEl.classList.remove("register-step--enter");
            if (onDone) onDone();
          },
          { once: true }
        );
      },
      { once: true }
    );
  }

  function botLabelFromUrl(url) {
    try {
      const u = new URL(url);
      const start = u.searchParams.get("start") || "";
      const m = u.pathname.match(/\/([^/]+)$/);
      const name = m ? m[1] : "";
      return name ? `Открыть @${name}` : "Открыть бота в Telegram";
    } catch (_) {
      return "Открыть бота в Telegram";
    }
  }

  function showVerifyStep(data) {
    activeToken = data.token || "";
    const botUrl = data.botUrl || "#";
    verifyBotLink.href = botUrl;
    if (verifyBotLabel) {
      verifyBotLabel.textContent = botLabelFromUrl(botUrl);
    }
    clearError(verifyErrEl);
    const statusText = verifyWaitEl?.querySelector(".register-verify-status-text");
    if (statusText) statusText.textContent = "Ожидаем подтверждение…";
    verifyWaitEl.hidden = false;

    titleEl.textContent = "Подтверждение в Telegram";
    subEl.textContent = "Шаг 2 из 2 — привязка аккаунта";

    animateSwap(form, verifyPanel, () => startPolling());
  }

  function showFormStep() {
    stopPolling();
    activeToken = "";
    clearError(verifyErrEl);
    btn.disabled = false;

    titleEl.textContent = "Создать аккаунт";
    subEl.textContent = "1 день бесплатно · 1 сервер и 2 аккаунта";

    if (verifyPanel.hidden) return;
    animateSwap(verifyPanel, form);
  }

  async function completeRegistration() {
    stopPolling();
    const statusText = verifyWaitEl?.querySelector(".register-verify-status-text");
    if (statusText) statusText.textContent = "Создаём аккаунт…";
    const r = await secureFetch("/api/auth/register/complete", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: activeToken }),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      showError(verifyErrEl, data.detail || data.error || "Не удалось завершить регистрацию");
      verifyWaitEl.hidden = true;
      return;
    }
    const data = await r.json().catch(() => ({}));
    const target = presetPlan
      ? `/profile?plan=${encodeURIComponent(presetPlan)}`
      : (data.redirect || "/app");
    window.location.replace(target);
  }

  async function pollStatus() {
    if (!activeToken) return;
    try {
      const r = await secureFetch(
        `/api/auth/register/status?token=${encodeURIComponent(activeToken)}`,
        { credentials: "same-origin" }
      );
      if (!r.ok) return;
      const data = await r.json().catch(() => ({}));
      if (data.status === "expired") {
        stopPolling();
        showError(verifyErrEl, "Время подтверждения истекло. Начните заново.");
        verifyWaitEl.hidden = true;
        return;
      }
      if (data.verified) {
        await completeRegistration();
      }
    } catch (_) { /* retry on next tick */ }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollStatus, 2000);
    pollStatus();
  }

  verifyBackBtn?.addEventListener("click", () => {
    showFormStep();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError(errEl);
    btn.disabled = true;

    const body = {
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
      name: document.getElementById("name").value.trim(),
      _website: document.getElementById("website")?.value || "",
    };
    if (presetRef) body.ref = presetRef;

    try {
      const r = await secureFetch("/api/auth/register/start", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        showVerifyStep(data);
        return;
      }

      showError(errEl, data.detail || data.error || "Ошибка регистрации");
    } catch (_) {
      showError(errEl, "Сервер недоступен");
    } finally {
      if (!activeToken) btn.disabled = false;
    }
  });
})();
