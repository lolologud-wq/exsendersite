(function () {
  const PLANS_META = {
    trial: { label: "Trial", days: 1 },
    week: { label: "Week", days: 7 },
    month: { label: "Month", days: 30 },
    quarter: { label: "Quarter", days: 90 },
  };

  let pollTimer = null;
  let activeInvoiceId = null;
  let activePromoCode = "";
  let useReferralBalance = true;
  let lastReferralBalance = 0;

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
    const isTrial = profile?.isTrial;

    if (nameEl) {
      if (isTrial) {
        nameEl.textContent = "Пробный период · активен";
      } else {
        nameEl.textContent = active
          ? `${planLabel(plan)} · активен`
          : "Нет активной подписки";
      }
    }
    if (metaEl) {
      if (isTrial && active) {
        const sec = profile.planRemainingSec || 0;
        const lim = profile.trialLimits;
        const limTxt = lim ? ` · лимит ${lim.maxBots} сервер, ${lim.maxAccounts} аккаунта` : "";
        if (sec > 0 && sec < 86400) {
          const h = Math.max(1, Math.ceil(sec / 3600));
          metaEl.textContent = `Осталось ~${h} ч.${limTxt}`;
        } else {
          metaEl.textContent = `Действует до ${fmtDate(exp)}${limTxt}`;
        }
      } else {
        metaEl.textContent = active
          ? `Действует до ${fmtDate(exp)}`
          : "Выбери тариф ниже и оплати через Crypto Bot — доступ откроется автоматически.";
      }
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
    host.innerHTML = items.map((h) => {
      const credit = h.referralCreditUsd || 0;
      const amt = credit > 0 && !h.amountUsd
        ? `реф. баланс ${fmtUsd(credit)}`
        : credit > 0
          ? `${fmtUsd(h.amountUsd)} (−${fmtUsd(credit)})`
          : fmtUsd(h.amountUsd);
      return `
      <div class="pf-history-row">
        <div>
          <div class="pf-history-plan">${escapeHtml(planLabel(h.plan))}</div>
          <div class="pf-history-date">${fmtDate(h.createdAt)}</div>
        </div>
        <div class="pf-history-amt">${amt}</div>
        <span class="pf-pill pf-pill-${h.status === "paid" ? "ok" : "wait"}">${escapeHtml(h.status)}</span>
      </div>`;
    }).join("");
  }

  function openPayModal(data) {
    const modal = document.getElementById("pfPayModal");
    document.getElementById("pfPayTitle").textContent = `Оплата · ${planLabel(data.plan)}`;
    document.getElementById("pfPayPlan").textContent = planLabel(data.plan);
    const credit = data.referralCreditUsd || 0;
    const amtEl = document.getElementById("pfPayAmount");
    if (amtEl) {
      amtEl.textContent = credit > 0
        ? `${fmtUsd(data.amountUsd)} (списано ${fmtUsd(credit)} с баланса)`
        : fmtUsd(data.amountUsd);
    }
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
      if (useReferralBalance && lastReferralBalance >= 0.01) {
        body.useReferralBalance = true;
      }
      const res = await api("POST", "/api/payments/create-invoice", body);
      if (res.paid || res.paidByReferral) {
        showAlert(
          res.referralCreditUsd
            ? `Подписка оплачена реферальным балансом (${fmtUsd(res.referralCreditUsd)})`
            : "Подписка активирована!",
          "ok",
        );
        await loadProfile();
        setTimeout(() => { window.location.href = "/app"; }, 1200);
        return;
      }
      activeInvoiceId = res.invoiceId;
      openPayModal({
        plan: planId,
        amountUsd: res.amountUsd,
        payUrl: res.payUrl,
        referralCreditUsd: res.referralCreditUsd,
      });
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(() => pollInvoice(activeInvoiceId), 4000);
      pollInvoice(activeInvoiceId);
    } catch (e) {
      showAlert(e.message, "err");
    }
  }

  function renderReferral(profile, referral) {
    const sec = document.getElementById("pfReferralSection");
    const codeEl = document.getElementById("pfRefCode");
    const statsEl = document.getElementById("pfRefStats");
    const descEl = document.getElementById("pfRefDesc");
    const badgeEl = document.getElementById("pfRefBadge");
    const perksEl = document.getElementById("pfRefPerks");
    const histWrap = document.getElementById("pfRefHistory");
    const histList = document.getElementById("pfRefHistoryList");
    if (!sec || !profile?.referralCode) return;
    sec.hidden = false;

    const pct = referral?.commissionPct ?? 15;
    const bonusDays = referral?.bonusDaysFirstPay ?? 3;
    if (badgeEl) badgeEl.textContent = `${pct}% с каждой оплаты`;
    if (descEl) {
      descEl.textContent =
        "Делись ссылкой — получай процент с оплат друзей. Баланс трать на тариф целиком или частично.";
    }
    if (perksEl) {
      perksEl.innerHTML = [
        `${pct}% комиссия`,
        `+${bonusDays} дн. за 1-ю оплату`,
        "Оплата тарифа балансом",
      ].map((t) => `<span class="pf-ref-perk">${escapeHtml(t)}</span>`).join("");
    }

    const invited = referral?.invited ?? 0;
    const paid = referral?.paid ?? 0;
    const earned = profile.referralEarnedTotal || 0;
    const balance = profile.referralBalanceUsd || 0;

    if (statsEl) {
      statsEl.innerHTML = `
        <article class="pf-ref-stat">
          <span class="pf-ref-stat-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="8" r="3.2"/><path d="M2.8 20c.6-3.4 3.1-5.4 6.2-5.4S14.6 16.6 15.2 20"/><circle cx="17.5" cy="8.5" r="2.5"/><path d="M16 14.5c2.6.2 4.6 1.7 5.2 4.5"/></svg>
          </span>
          <div class="pf-ref-stat-body">
            <span class="pf-ref-stat-label">Приглашено</span>
            <b class="pf-ref-stat-value">${invited}</b>
          </div>
        </article>
        <article class="pf-ref-stat">
          <span class="pf-ref-stat-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 6 9 17l-5-5"/></svg>
          </span>
          <div class="pf-ref-stat-body">
            <span class="pf-ref-stat-label">Оплатили</span>
            <b class="pf-ref-stat-value">${paid}</b>
          </div>
        </article>
        <article class="pf-ref-stat">
          <span class="pf-ref-stat-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          </span>
          <div class="pf-ref-stat-body">
            <span class="pf-ref-stat-label">Заработано</span>
            <b class="pf-ref-stat-value">${fmtUsd(earned)}</b>
          </div>
        </article>
        <article class="pf-ref-stat pf-ref-stat-balance">
          <span class="pf-ref-stat-icon pf-ref-stat-icon-accent" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>
          </span>
          <div class="pf-ref-stat-body">
            <span class="pf-ref-stat-label">Баланс</span>
            <b class="pf-ref-stat-value pf-ref-stat-value-accent">${fmtUsd(balance)}</b>
          </div>
        </article>`;
    }

    lastReferralBalance = profile.referralBalanceUsd || 0;
    const refBalWrap = document.getElementById("pfUseRefBalanceWrap");
    const refBalLabel = document.getElementById("pfUseRefBalanceLabel");
    const refBalCheck = document.getElementById("pfUseRefBalance");
    if (refBalWrap && lastReferralBalance >= 0.01) {
      refBalWrap.hidden = false;
      if (refBalLabel) {
        refBalLabel.innerHTML = `Списать реферальный баланс <b>(${escapeHtml(fmtUsd(lastReferralBalance))}</b> доступно)`;
      }
      if (refBalCheck) refBalCheck.checked = useReferralBalance;
    } else if (refBalWrap) {
      refBalWrap.hidden = true;
    }

    const link = `${window.location.origin}/register?ref=${profile.referralCode}`;
    if (codeEl) codeEl.value = link;

    const history = referral?.history || [];
    if (histWrap && histList) {
      if (!history.length) {
        histWrap.hidden = true;
      } else {
        histWrap.hidden = false;
        histList.innerHTML = history.map((h) => {
          const isBonus = h.kind === "bonus_days";
          const isAdmin = h.kind === "admin_grant";
          const label = isBonus
            ? "Бонус за первую оплату реферала"
            : isAdmin
              ? "Начисление от администрации"
              : "Комиссия с оплаты";
          const amt = isBonus ? `+${bonusDays} дн.` : fmtUsd(h.commissionUsd);
          const icon = isBonus ? "🎁" : isAdmin ? "✨" : "💵";
          return `
            <div class="pf-ref-hist-row">
              <span class="pf-ref-hist-icon">${icon}</span>
              <div class="pf-ref-hist-main">
                <span class="pf-ref-hist-label">${escapeHtml(label)}</span>
                <span class="pf-ref-hist-date">${fmtDate(h.createdAt)}</span>
              </div>
              <span class="pf-ref-hist-amt">${escapeHtml(amt)}</span>
            </div>`;
        }).join("");
      }
    }

    const copyBtn = document.getElementById("pfRefCopy");
    if (copyBtn && !copyBtn.dataset.bound) {
      copyBtn.dataset.bound = "1";
      copyBtn.addEventListener("click", () => {
        const url = codeEl?.value || "";
        if (!url) return;
        navigator.clipboard?.writeText(url).then(() => {
          showAlert("Ссылка скопирована", "ok");
          const span = copyBtn.querySelector("span");
          if (span) {
            const prev = span.textContent;
            span.textContent = "Скопировано";
            setTimeout(() => { span.textContent = prev; }, 2000);
          }
        });
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
      renderReferral(data.profile, data.referral);
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

  document.getElementById("pfUseRefBalance")?.addEventListener("change", (e) => {
    useReferralBalance = !!e.target.checked;
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
