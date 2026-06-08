(function (global) {
  async function stopImpersonation() {
    const r = await secureFetch("/api/auth/impersonate/stop", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || "Не удалось выйти из режима поддержки");
    window.location.href = data.redirect || "/admin";
  }

  function mountImpersonationBanner(email, adminLogin) {
    if (document.getElementById("impersonationBanner")) return;
    const bar = document.createElement("div");
    bar.id = "impersonationBanner";
    bar.className = "imp-banner";
    bar.innerHTML =
      `<span>Режим поддержки: вы вошли как <b>${escapeHtml(email || "пользователь")}</b>` +
      (adminLogin ? ` (админ ${escapeHtml(adminLogin)})` : "") +
      `</span>` +
      `<button type="button" class="btn-ghost" id="impersonationStopBtn">Выйти из режима</button>`;
    const host = document.querySelector(".app") || document.body;
    host.insertBefore(bar, host.firstChild);
    bar.querySelector("#impersonationStopBtn")?.addEventListener("click", () => {
      stopImpersonation().catch((e) => alert(e.message));
    });
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function bindImpersonationFromMe(meData) {
    if (!meData?.impersonatedBy || meData.kind !== "user") return;
    mountImpersonationBanner(meData.profile?.email || meData.user, meData.impersonatedBy);
  }

  global.bindImpersonationFromMe = bindImpersonationFromMe;
  global.stopImpersonation = stopImpersonation;
})(typeof window !== "undefined" ? window : globalThis);
