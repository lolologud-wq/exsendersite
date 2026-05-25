(function () {
  const form = document.getElementById("registerForm");
  const errEl = document.getElementById("registerError");
  const btn = document.getElementById("registerBtn");

  // Preserve ?plan=... so profile page can preselect it after redirect.
  let presetPlan = "";
  let presetRef = "";
  try {
    const url = new URL(window.location.href);
    presetPlan = url.searchParams.get("plan") || "";
    presetRef = url.searchParams.get("ref") || "";
  } catch (_) { /* ignore */ }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.hidden = true;
    errEl.textContent = "";
    btn.disabled = true;

    const body = {
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
      name: document.getElementById("name").value.trim(),
      _website: document.getElementById("website")?.value || "",
    };
    if (presetRef) body.ref = presetRef;

    try {
      const r = await secureFetch("/api/auth/register", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (r.ok) {
        const target = presetPlan
          ? `/profile?plan=${encodeURIComponent(presetPlan)}`
          : "/profile";
        window.location.replace(target);
        return;
      }

      const data = await r.json().catch(() => ({}));
      errEl.textContent = data.detail || data.error || "Ошибка регистрации";
      errEl.hidden = false;
    } catch (_) {
      errEl.textContent = "Сервер недоступен";
      errEl.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });
})();
