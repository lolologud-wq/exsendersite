(function () {
  const PLANS_META = {
    week: { label: "Week", days: 7 },
    month: { label: "Month", days: 30 },
    quarter: { label: "Quarter", days: 90 },
  };

  let pollTimer = null;
  let activeInvoiceId = null;
  let activePromoCode = "";

  function fmtUsd(n) {
    return `$${Number(n).toFixed(Number(n) % 1 ? 2 : 0)}`;
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString("ru-RU", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }

  function planLabel(id) {
    return PLANS_META[id]?.label || id || "—";
  }

  function showAlert(msg, kind = "info") {
    const el = document.getElementById("pfAlert");
    if (!el) return;
    el.textContent = msg;
    el.className = `pf-alert pf-alert-${kind}`;
    el.hidden = !msg;
  }

  async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await secureFetch(path, opts);
    if (r.status === 401) {
      window.location.href = "/login";
      throw new Error("not authenticated");
    }
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("json") ? await r.json() : null;
    if (!r.ok) {
      const msg = data?.detail || data?.error || `HTTP ${r.status}`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function renderStatus(profile, kind) {
    const nameEl = document.getElementById("pfPlanName");
    const metaEl = document.getElementById("pfPlanMeta");
    const dashLink = document.getElementById("pfDashLink");
    const openApp = document.getElementById("pfOpenApp");

    if (kind === "admin") {
      if (nameEl) nameEl.textContent = "Admin · безлимит";
      if (metaEl) metaEl.textContent = "Полный доступ ко всем VDS и настройкам.";
      if (dashLink) dashLink.hidden = false;
      if (openApp) openApp.hidden = false;
      return;
    }

    const active = profile?.planActive;
    const plan = profile?.plan;
    const exp = profile?.planExpiresAt;

    if (nameEl) {
      nameEl.textContent = active
        ? `${planLabel(plan)} · активен`
        : "Нет активной подписки";
    }
    if (metaEl) {
      metaEl.textContent = active
        ? `Действует до ${fmtDate(exp)}`
        : "Выбери тариф ниже и оплати через Crypto Bot — доступ откроется автоматически.";
    }
    if (dashLink) dashLink.hidden = false;
    if (openApp) {
        openApp.hidden = false;
        if (active) {
          openApp.href = "/app";
          openApp.textContent = "Открыть панель";
          openApp.className = "btn-primary";
        } else {
          openApp.href = "#pfPlansSection";
          openApp.textContent = "Выбрать тариф";
          openApp.className = "btn-ghost";
        }
      }
  }

  function renderPlans(plans, profile, cryptoOk) {
    const host = document.getElementById("pfPlans");
    if (!host) return;

    const activePlan = profile?.planActive ? profile.plan : "";
    host.innerHTML = plans.map((p) => {
      const isActive = p.id === activePlan;
      const pop = p.id === "month" ? " pf-plan-pop" : "";
      return `
        <article class="pf-plan${pop}${isActive ? " pf-plan-active" : ""}">
          ${p.id === "month" ? '<span class="pf-plan-badge">Популярный</span>' : ""}
          <div class="pf-plan-head">
            <span class="pf-plan-name">${escapeHtml(p.label)}</span>
            <span class="pf-plan-days">${p.duration_days} дн.</span>
          </div>
          <div class="pf-plan-price">${fmtUsd(p.priceUsd)}</div>
          <ul class="pf-plan-feats">
            <li>Полный доступ к панели</li>
            <li>Multi-account рассылка</li>
            <li>Crypto Bot · USDT</li>
          </ul>
          <button type="button" class="${p.id === "month" ? "btn-primary" : "btn-ghost"} pf-plan-btn"
            data-plan="${escapeHtml(p.id)}" ${!cryptoOk ? "disabled title=\"Платежи не настроены\"" : ""}>
            ${isActive ? "Продлить" : "Оплатить"}
          </button>
        </article>`;
    }).join("");

    host.querySelectorAll("[data-plan]").forEach((btn) => {
      btn.addEventListener("click", () => startPayment(btn.dataset.plan));
    });
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderHistory(items) {
    const host = document.getElementById("pfHistory");
    if (!host) return;
    if (!items?.length) {
      host.innerHTML = '<div class="pf-empty">Платежей пока нет.</div>';
      return;
    }
    host.innerHTML = items.map((h) => `
      <div class="pf-history-row">
        <div>
          <div class="pf-history-plan">${escapeHtml(planLabel(h.plan))}</div>
          <div class="pf-history-date">${fmtDate(h.createdAt)}</div>
        </div>
        <div class="pf-history-amt">${fmtUsd(h.amountUsd)}</div>
        <span class="pf-pill pf-pill-${h.status === "paid" ? "ok" : "wait"}">${escapeHtml(h.status)}</span>
      </div>
    `).join("");
  }

  function openPayModal(data) {
    const modal = document.getElementById("pfPayModal");
    document.getElementById("pfPayTitle").textContent = `Оплата · ${planLabel(data.plan)}`;
    document.getElementById("pfPayPlan").textContent = planLabel(data.plan);
    document.getElementById("pfPayAmount").textContent = fmtUsd(data.amountUsd);
    document.getElementById("pfPayStatus").textContent = "ожидание оплаты";
    document.getElementById("pfPayStatus").className = "pf-pill pf-pill-wait";
    const link = document.getElementById("pfPayLink");
    link.href = data.payUrl || "#";
    link.hidden = !data.payUrl;
    modal.hidden = false;
  }

  function closePayModal() {
    document.getElementById("pfPayModal").hidden = true;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    activeInvoiceId = null;
  }

  async function pollInvoice(invoiceId) {
    try {
      const res = await api("GET", `/api/payments/check/${encodeURIComponent(invoiceId)}`);
      const stEl = document.getElementById("pfPayStatus");
      if (stEl) {
        stEl.textContent = res.status || "—";
        stEl.className = `pf-pill pf-pill-${res.status === "paid" ? "ok" : "wait"}`;
      }
      if (res.status === "paid") {
        showAlert("Оплата прошла — подписка активирована!", "ok");
        closePayModal();
        await loadProfile();
        setTimeout(() => { window.location.href = "/app"; }, 1200);
      }
    } catch (e) {
      console.warn("poll invoice", e);
    }
  }

  async function startPayment(planId) {
    showAlert("");
    try {
      const body = { plan: planId };
      if (activePromoCode) body.promo = activePromoCode;
      const res = await api("POST", "/api/payments/create-invoice", body);
      activeInvoiceId = res.invoiceId;
      openPayModal({
        plan: planId,
        amountUsd: res.amountUsd,
        payUrl: res.payUrl,
      });
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(() => pollInvoice(activeInvoiceId), 4000);
      pollInvoice(activeInvoiceId);
    } catch (e) {
      showAlert(e.message, "err");
    }
  }

  function renderReferral(profile) {
    const sec = document.getElementById("pfReferralSection");
    const codeEl = document.getElementById("pfRefCode");
    if (!sec || !profile?.referralCode) return;
    sec.hidden = false;
    const link = `${window.location.origin}/register?ref=${profile.referralCode}`;
    if (codeEl) codeEl.textContent = link;
    const copyBtn = document.getElementById("pfRefCopy");
    if (copyBtn && !copyBtn.dataset.bound) {
      copyBtn.dataset.bound = "1";
      copyBtn.addEventListener("click", () => {
        const url = codeEl?.textContent || "";
        navigator.clipboard?.writeText(url).then(() => showAlert("Ссылка скопирована", "ok"));
      });
    }
  }

  function renderNotifications(items) {
    const host = document.getElementById("pfNotifications");
    if (!host || !items?.length) return;
    host.hidden = false;
    host.innerHTML = items.map((n) => `
      <div class="pf-notify-item" data-nid="${escapeHtml(n.id)}">
        <b>${escapeHtml(n.title)}</b>
        <p>${escapeHtml(n.message)}</p>
        <button type="button" class="btn-ghost pf-notify-dismiss">OK</button>
      </div>`).join("");
    host.querySelectorAll(".pf-notify-dismiss").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const item = btn.closest("[data-nid]");
        const nid = item?.dataset.nid;
        if (nid) {
          try { await api("POST", `/api/users/notifications/${encodeURIComponent(nid)}/read`); } catch (_) { /* ignore */ }
        }
        item?.remove();
        if (!host.children.length) host.hidden = true;
      });
    });
  }

  async function loadProfile() {
    const data = await api("GET", "/api/users/me");
    const emailEl = document.getElementById("pfUserEmail");
    if (emailEl) {
      emailEl.textContent = data.kind === "admin"
        ? "admin"
        : (data.profile?.email || "—");
    }
    renderStatus(data.profile, data.kind);
    if (data.kind === "admin") {
      const plansSec = document.getElementById("pfPlansSection");
      if (plansSec) plansSec.hidden = true;
      const refSec = document.getElementById("pfReferralSection");
      if (refSec) refSec.hidden = true;
    } else {
      renderPlans(data.plans || [], data.profile, data.cryptoBotConfigured);
    }
    renderHistory(data.history || []);
    if (data.kind !== "admin") {
      renderReferral(data.profile);
      renderNotifications(data.notifications || []);
    }

    if (!data.cryptoBotConfigured && data.kind === "user") {
      showAlert("Платежи временно недоступны — администратор не настроил CRYPTO_BOT_TOKEN.", "warn");
    }

    // Auto-open payment if ?plan= in URL
    try {
      const plan = new URL(window.location.href).searchParams.get("plan");
      if (plan && data.cryptoBotConfigured && data.kind === "user") {
        const valid = (data.plans || []).some((p) => p.id === plan);
        if (valid) startPayment(plan);
      }
    } catch (_) { /* ignore */ }

    return data;
  }

  document.getElementById("pfLogoutBtn")?.addEventListener("click", async () => {
    try {
      await api("POST", "/api/auth/logout");
    } catch (_) { /* ignore */ }
    window.location.href = "/login";
  });

  document.querySelectorAll("[data-pf-close]").forEach((el) => {
    el.addEventListener("click", closePayModal);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePayModal();
  });

  document.getElementById("pfPromoApply")?.addEventListener("click", async () => {
    const code = document.getElementById("pfPromoInput")?.value.trim();
    const hint = document.getElementById("pfPromoHint");
    if (!code) {
      activePromoCode = "";
      if (hint) hint.hidden = true;
      return;
    }
    try {
      const res = await api("POST", "/api/payments/validate-promo", { code, plan: "month" });
      activePromoCode = res.code;
      if (hint) {
        hint.hidden = false;
        hint.textContent = res.discountPct
          ? `Промокод ${res.code}: скидка ${res.discountPct}%`
          : `Промокод ${res.code}${res.bonusDays ? `: +${res.bonusDays} дн.` : ""}`;
      }
      showAlert("Промокод применён", "ok");
    } catch (e) {
      activePromoCode = "";
      if (hint) hint.hidden = true;
      showAlert(e.message, "err");
    }
  });

  loadProfile().catch((e) => showAlert(e.message, "err"));
})();
