(function () {
  let usersCache = [];

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

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderUsageCount(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v < 0) return '<span class="adm-tg-mute">—</span>';
    return `<span class="adm-usage-num">${escapeHtml(String(v))}</span>`;
  }

  function renderTelegramCell(u) {
    const tgId = Number(u.telegramUserId || 0);
    const username = String(u.telegramUsername || "").trim().replace(/^@/, "");
    if (!tgId) {
      return '<span class="adm-tg-mute">—</span>';
    }
    if (username) {
      const uname = escapeHtml(username);
      return `<a class="adm-tg-link" href="https://t.me/${uname}" target="_blank" rel="noopener noreferrer">@${uname}</a>`;
    }
    return `<span class="adm-tg-id" title="Telegram ID">id ${escapeHtml(String(tgId))}</span>`;
  }

  function showAlert(msg, kind = "err") {
    const errEl = document.getElementById("admAlert");
    const okEl = document.getElementById("admOk");
    if (kind === "ok") {
      if (okEl) { okEl.textContent = msg; okEl.hidden = !msg; }
      if (errEl) errEl.hidden = true;
      if (msg) setTimeout(() => { if (okEl) okEl.hidden = true; }, 4000);
      return;
    }
    if (errEl) {
      errEl.textContent = msg;
      errEl.hidden = !msg;
    }
    if (okEl) okEl.hidden = true;
  }

  async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await secureFetch(path, opts);
    if (r.status === 401 || r.status === 403) {
      window.location.href = "/login";
      throw new Error("not admin");
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === "string" ? d : (data.error || `HTTP ${r.status}`));
    }
    return data;
  }

  function renderStats(data) {
    document.getElementById("statRevenueTotal").textContent = fmtUsd(data.revenueTotalUsd || 0);
    document.getElementById("statRevenueMonth").textContent = fmtUsd(data.revenueMonthUsd || 0);
    document.getElementById("statInvoicesPaid").textContent = String(data.invoicesPaid ?? 0);
    document.getElementById("statInvoicesPending").textContent = String(data.invoicesPending ?? 0);
    document.getElementById("statUsersTotal").textContent = String(data.usersTotal ?? 0);
    document.getElementById("statUsersActive").textContent = String(data.usersActiveSub ?? 0);
    document.getElementById("statUsersMonth").textContent = String(data.usersRegisteredMonth ?? 0);
    const cryptoEl = document.getElementById("statCrypto");
    if (cryptoEl) {
      cryptoEl.textContent = data.cryptoBotConfigured ? "подключён" : "не настроен";
      cryptoEl.style.color = data.cryptoBotConfigured ? "#86efac" : "#fca5a5";
    }
  }

  function calcGrowthPct(points) {
    if (!points?.length) return 0;
    const mid = Math.max(1, Math.floor(points.length / 2));
    const recent = points.slice(-mid).reduce((s, p) => s + (Number(p.amountUsd) || 0), 0);
    const prior = points.slice(0, mid).reduce((s, p) => s + (Number(p.amountUsd) || 0), 0);
    if (prior <= 0) return recent > 0 ? 100 : 0;
    return ((recent - prior) / prior) * 100;
  }

  function fmtAxisUsd(val) {
    if (val <= 0) return "0";
    if (val >= 100) return String(Math.round(val));
    if (val >= 10) return val.toFixed(0);
    if (val >= 1) return val.toFixed(1);
    return val.toFixed(2);
  }

  function chartLinePath(pts, yMin, yMax) {
    if (pts.length <= 1) {
      return pts.map((p, i) => `${i ? "L" : "M"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    }
    const hasZeroStep = pts.some((p, i) => {
      if (!i) return false;
      const prev = pts[i - 1].v;
      return (prev === 0 && p.v > 0) || (prev > 0 && p.v === 0);
    });
    if (hasZeroStep) {
      return pts.map((p, i) => `${i ? "L" : "M"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    }
    return smoothLinePath(pts, yMin, yMax);
  }

  function smoothLinePath(pts, yMin, yMax) {
    if (!pts.length) return "";
    if (pts.length === 1) return `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
    const clampY = (y) => Math.max(yMin, Math.min(yMax, y));
    let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i += 1) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const cp1x = p1.x + (p2.x - p0.x) / 6;
      let cp1y = p1.y + (p2.y - p0.y) / 6;
      const cp2x = p2.x - (p3.x - p1.x) / 6;
      let cp2y = p2.y - (p3.y - p1.y) / 6;
      cp1y = clampY(cp1y);
      cp2y = clampY(cp2y);
      d += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
    }
    return d;
  }

  function bindRevChartHover(wrap, svg, meta) {
    const tip = wrap.querySelector(".adm-rev-tooltip");
    const cross = svg.querySelector(".adm-rev-cross");
    const dot = svg.querySelector(".adm-rev-dot");
    if (!tip || !cross || !dot || !meta.pts.length) return;

    const show = (idx) => {
      const p = meta.pts[idx];
      const pt = meta.points[idx];
      if (!p || !pt) return;
      cross.setAttribute("x1", String(p.x));
      cross.setAttribute("x2", String(p.x));
      cross.style.opacity = "1";
      dot.setAttribute("cx", String(p.x));
      dot.setAttribute("cy", String(p.y));
      dot.style.opacity = "1";
      tip.innerHTML = `<b>${escapeHtml(fmtUsd(pt.amountUsd))}</b> · ${escapeHtml(pt.date)} · ${pt.count || 0} опл.`;
      tip.style.left = `${(p.x / meta.w) * 100}%`;
      tip.style.top = `${(p.y / meta.h) * 100}%`;
      tip.classList.add("on");
    };

    const hide = () => {
      cross.style.opacity = "0";
      dot.style.opacity = "0";
      tip.classList.remove("on");
    };

    const pick = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const x = ((clientX - rect.left) / rect.width) * meta.w;
      let best = 0;
      let bestDist = Infinity;
      meta.pts.forEach((p, i) => {
        const dist = Math.abs(p.x - x);
        if (dist < bestDist) {
          bestDist = dist;
          best = i;
        }
      });
      show(best);
    };

    svg.addEventListener("mousemove", (e) => pick(e.clientX));
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("touchmove", (e) => {
      if (e.touches[0]) pick(e.touches[0].clientX);
    }, { passive: true });
    svg.addEventListener("touchend", hide);
    show(meta.pts.length - 1);
  }

  function renderChart(points, opts = {}) {
    const host = document.getElementById("admChart");
    if (!host) return;
    if (!points?.length) {
      host.innerHTML = '<div class="adm-rev-empty">Нет данных за период</div>';
      return;
    }
    const amounts = points.map((p) => Number(p.amountUsd) || 0);
    const total = amounts.reduce((s, n) => s + n, 0);
    if (total <= 0) {
      host.innerHTML = '<div class="adm-rev-empty">Нет оплат за последние 30 дней</div>';
      return;
    }

    const growth = calcGrowthPct(points);
    const growthCls = growth > 0 ? "pos" : growth < 0 ? "neg" : "neu";
    const growthTxt = growth > 0
      ? `+${growth.toFixed(2)}%`
      : growth < 0
        ? `${growth.toFixed(2)}%`
        : "0%";
    const monthUsd = Number(opts.revenueMonthUsd) || total;

    const W = 800;
    const H = 240;
    const pad = { t: 18, r: 36, b: 28, l: 8 };
    const innerW = W - pad.l - pad.r;
    const innerH = H - pad.t - pad.b;
    const baseY = pad.t + innerH;
    const max = Math.max(...amounts, 0.01);

    const pts = amounts.map((v, i) => ({
      x: pad.l + (amounts.length <= 1 ? innerW / 2 : (i / (amounts.length - 1)) * innerW),
      y: pad.t + innerH - (v / max) * innerH,
      v,
    }));

    const line = chartLinePath(pts, pad.t, baseY);
    const area = `${line} L ${pts[pts.length - 1].x.toFixed(1)} ${baseY} L ${pts[0].x.toFixed(1)} ${baseY} Z`;

    const gridLines = [0.25, 0.5, 0.75, 1].map((f) => {
      const y = pad.t + innerH * (1 - f);
      const val = max * f;
      return `
        <line x1="${pad.l}" y1="${y.toFixed(1)}" x2="${W - pad.r}" y2="${y.toFixed(1)}"
          stroke="rgba(255,255,255,0.08)" stroke-dasharray="5 6"/>
        <text x="${W - pad.r + 6}" y="${(y + 4).toFixed(1)}" fill="rgba(255,255,255,0.28)"
          font-size="10" font-family="Inter, system-ui, sans-serif">${escapeHtml(fmtAxisUsd(val))}</text>`;
    }).join("");

    const lastDate = points[points.length - 1]?.date?.slice(0, 4) || new Date().getFullYear();

    host.innerHTML = `
      <div class="adm-rev-head">
        <div class="adm-rev-period">${lastDate} · 30 дней</div>
        <div class="adm-rev-metric ${growthCls}">${escapeHtml(growthTxt)}</div>
        <div class="adm-rev-sub">
          Выручка <b style="color:#e5e5e5">${escapeHtml(fmtUsd(monthUsd))}</b> за период.
          ${growth >= 0 ? "Рост" : "Спад"} относительно первой половины месяца.
        </div>
      </div>
      <div class="adm-rev-svg-wrap">
        <div class="adm-rev-tooltip"></div>
        <svg class="adm-rev-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <pattern id="admRevHatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(0)">
              <line x1="2.5" y1="0" x2="2.5" y2="5" stroke="rgba(255,255,255,0.07)" stroke-width="1"/>
            </pattern>
            <linearGradient id="admRevLine" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stop-color="#34d399"/>
              <stop offset="100%" stop-color="#6ee7b7"/>
            </linearGradient>
            <clipPath id="admRevClip"><rect x="${pad.l}" y="${pad.t}" width="${innerW}" height="${innerH + 4}"/></clipPath>
          </defs>
          ${gridLines}
          <g clip-path="url(#admRevClip)">
            <path d="${area}" fill="url(#admRevHatch)" opacity="0.95"/>
            <path d="${area}" fill="url(#admRevLine)" opacity="0.07"/>
          </g>
          <path d="${line}" fill="none" stroke="url(#admRevLine)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          <line class="adm-rev-cross" x1="0" y1="${pad.t}" x2="0" y2="${baseY}"
            stroke="rgba(255,255,255,0.55)" stroke-width="1" opacity="0"/>
          <circle class="adm-rev-dot" cx="0" cy="0" r="5" fill="#fff" stroke="#050505" stroke-width="2" opacity="0"/>
        </svg>
      </div>`;

    bindRevChartHover(
      host.querySelector(".adm-rev-svg-wrap"),
      host.querySelector(".adm-rev-svg"),
      { pts, points, w: W, h: H },
    );
  }

  function renderPayments(items) {
    const body = document.getElementById("admPaymentsBody");
    if (!body) return;
    if (!items?.length) {
      body.innerHTML = '<tr><td colspan="4" class="adm-empty">Оплат пока нет.</td></tr>';
      return;
    }
    body.innerHTML = items.map((p) => `
      <tr>
        <td>${escapeHtml(fmtDate(p.paidAt || p.createdAt))}</td>
        <td>${escapeHtml(p.email)}</td>
        <td>${escapeHtml(p.plan)}</td>
        <td><b>${escapeHtml(fmtUsd(p.amountUsd))}</b></td>
      </tr>`).join("");
  }

  function renderUsers(items) {
    usersCache = items || [];
    const body = document.getElementById("admUsersBody");
    if (!body) return;
    if (!items?.length) {
      body.innerHTML = '<tr><td colspan="10" class="adm-empty">Пользователей пока нет.</td></tr>';
      return;
    }
    body.innerHTML = items.map((u) => {
      const active = u.planActive && !u.blocked;
      let statusCls = "adm-pill-off";
      let statusTxt = "нет плана";
      if (u.blocked) { statusCls = "adm-pill-block"; statusTxt = "заблокирован"; }
      else if (active) { statusCls = "adm-pill-ok"; statusTxt = "активен"; }
      const refBal = Number(u.referralBalanceUsd || 0);
      return `
        <tr data-uid="${escapeHtml(u.id)}">
          <td>${escapeHtml(u.email)}</td>
          <td class="adm-tg-cell">${renderTelegramCell(u)}</td>
          <td class="adm-usage-cell">${renderUsageCount(u.botsUsed)}</td>
          <td class="adm-usage-cell">${renderUsageCount(u.accountsUsed)}</td>
          <td><code>${escapeHtml(u.referralCode || "—")}</code></td>
          <td><b>${escapeHtml(fmtUsd(refBal))}</b></td>
          <td>${escapeHtml(u.plan || "—")}</td>
          <td>${escapeHtml(active ? fmtDate(u.planExpiresAt) : "—")}</td>
          <td><span class="adm-pill ${statusCls}">${statusTxt}</span></td>
          <td class="adm-actions">
            <button type="button" class="btn-ghost adm-btn-sm" data-act="grant" data-uid="${escapeHtml(u.id)}" data-email="${escapeHtml(u.email)}">+план</button>
            <button type="button" class="btn-ghost adm-btn-sm" data-act="ref" data-uid="${escapeHtml(u.id)}" data-email="${escapeHtml(u.email)}" data-balance="${refBal}">+реф</button>
            <button type="button" class="btn-ghost adm-btn-sm" data-act="block" data-uid="${escapeHtml(u.id)}" data-blocked="${u.blocked ? "1" : "0"}">${u.blocked ? "разблок" : "блок"}</button>
            <button type="button" class="btn-danger adm-btn-sm" data-act="delete" data-uid="${escapeHtml(u.id)}" data-email="${escapeHtml(u.email)}" title="Удалить аккаунт">удалить</button>
          </td>
        </tr>`;
    }).join("");
  }

  function renderPromos(items) {
    const body = document.getElementById("admPromosBody");
    if (!body) return;
    if (!items?.length) {
      body.innerHTML = '<tr><td colspan="5" class="adm-empty">Промокодов нет.</td></tr>';
      return;
    }
    body.innerHTML = items.map((p) => `
      <tr>
        <td><code>${escapeHtml(p.code)}</code> ${p.active ? "" : '<span class="adm-pill adm-pill-off">off</span>'}</td>
        <td>${p.discountPct ? p.discountPct + "%" : "—"}</td>
        <td>${p.bonusDays ? "+" + p.bonusDays + " дн." : "—"}</td>
        <td>${p.uses}${p.maxUses ? " / " + p.maxUses : ""}</td>
        <td>
          <button type="button" class="btn-ghost adm-btn-sm" data-promo-toggle="${escapeHtml(p.code)}" data-active="${p.active ? "0" : "1"}">
            ${p.active ? "Выкл" : "Вкл"}
          </button>
        </td>
      </tr>`).join("");
  }

  function renderChangelog(items) {
    const body = document.getElementById("admChangelogBody");
    if (!body) return;
    if (!items?.length) {
      body.innerHTML = '<tr><td colspan="3" class="adm-empty">Записей нет.</td></tr>';
      return;
    }
    body.innerHTML = items.map((e) => `
      <tr>
        <td>${escapeHtml(e.date || fmtDate(e.createdAt))}</td>
        <td><code>${escapeHtml(e.version || "—")}</code></td>
        <td>${escapeHtml(e.title)}</td>
      </tr>`).join("");
  }

  function renderAudit(items) {
    const body = document.getElementById("admAuditBody");
    if (!body) return;
    if (!items?.length) {
      body.innerHTML = '<tr><td colspan="5" class="adm-empty">Записей пока нет.</td></tr>';
      return;
    }
    body.innerHTML = items.map((e) => `
      <tr>
        <td>${escapeHtml(fmtDate(e.createdAt))}</td>
        <td>${escapeHtml(e.admin)}</td>
        <td>${escapeHtml(e.action)}</td>
        <td>${escapeHtml(e.target || "—")}</td>
        <td class="adm-details">${escapeHtml(e.details || "—")}</td>
      </tr>`).join("");
  }

  function findUserIdByEmail(email) {
    const u = usersCache.find((x) => x.email === email.trim().toLowerCase());
    return u?.id || null;
  }

  async function load() {
    showAlert("");
    const data = await api("GET", "/api/admin/stats");
    renderStats(data);
    renderChart(data.revenueChart, { revenueMonthUsd: data.revenueMonthUsd });
    renderPayments(data.recentPayments);
    renderUsers(data.recentUsers);
    renderPromos(data.promos);
    renderAudit(data.auditLog);
    try {
      const cl = await api("GET", "/api/admin/changelog");
      renderChangelog(cl.items);
    } catch (_) { /* ignore */ }
    const full = await api("GET", "/api/admin/users");
    renderUsers(full.users);
  }

  async function init() {
    try {
      const me = await fetch("/api/auth/me", { credentials: "same-origin" }).then((r) => r.json());
      if (me.kind !== "admin") {
        window.location.href = "/login";
        return;
      }
      document.getElementById("admUser").textContent = me.user || "admin";
    } catch {
      window.location.href = "/login";
      return;
    }

    document.getElementById("admRefreshBtn")?.addEventListener("click", () => {
      load().catch((e) => showAlert(e.message));
    });

    document.getElementById("admLogoutBtn")?.addEventListener("click", async () => {
      try { await api("POST", "/api/auth/logout"); } catch (_) { /* ignore */ }
      window.location.href = "/login";
    });

    document.getElementById("admGrantForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = document.getElementById("grantEmail").value.trim();
      const plan = document.getElementById("grantPlan").value;
      const daysRaw = document.getElementById("grantDays").value;
      const days = daysRaw ? parseInt(daysRaw, 10) : 0;
      const uid = findUserIdByEmail(email);
      if (!uid) { showAlert("Пользователь не найден"); return; }
      try {
        await api("POST", `/api/admin/users/${encodeURIComponent(uid)}/plan`, { plan, days });
        showAlert(`Подписка выдана: ${email}`, "ok");
        await load();
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admRefBalanceForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = document.getElementById("refBalanceEmail").value.trim();
      const amount = Number(document.getElementById("refBalanceAmount").value);
      const note = document.getElementById("refBalanceNote").value.trim();
      const uid = findUserIdByEmail(email);
      if (!uid) { showAlert("Пользователь не найден"); return; }
      if (!amount || amount <= 0) { showAlert("Укажи сумму больше 0"); return; }
      try {
        const res = await api("POST", `/api/admin/users/${encodeURIComponent(uid)}/referral-balance`, {
          amountUsd: amount,
          note,
        });
        const bal = res.profile?.referralBalanceUsd ?? amount;
        showAlert(`Начислено ${fmtUsd(amount)} → баланс ${fmtUsd(bal)} (${email})`, "ok");
        e.target.reset();
        await load();
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admPromoForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("POST", "/api/admin/promos", {
          code: document.getElementById("promoCode").value.trim(),
          discountPct: Number(document.getElementById("promoDiscount").value) || 0,
          bonusDays: Number(document.getElementById("promoBonusDays").value) || 0,
          maxUses: Number(document.getElementById("promoMaxUses").value) || 0,
          note: document.getElementById("promoNote").value.trim(),
        });
        e.target.reset();
        showAlert("Промокод создан", "ok");
        await load();
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admNotifyForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const message = document.getElementById("notifyMessage").value.trim();
      const title = document.getElementById("notifyTitle").value.trim();
      const raw = document.getElementById("notifyEmails").value.trim();
      let userIds = "all";
      if (raw) {
        const ids = raw.split(",").map((em) => findUserIdByEmail(em.trim())).filter(Boolean);
        if (!ids.length) { showAlert("Email не найдены"); return; }
        userIds = ids;
      }
      try {
        const res = await api("POST", "/api/admin/notify", { message, title, userIds });
        showAlert(`Отправлено: ${res.sent}`, "ok");
        e.target.reset();
        document.getElementById("notifyTitle").value = "exsender";
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admNotifyClear")?.addEventListener("click", async () => {
      if (!confirm("Удалить все уведомления у всех пользователей?")) return;
      try {
        const res = await api("DELETE", "/api/admin/notify");
        showAlert(`Удалено: ${res.removed}`, "ok");
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admUsersBody")?.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const uid = btn.dataset.uid;
      const act = btn.dataset.act;
      if (act === "grant") {
        document.getElementById("grantEmail").value = btn.dataset.email || "";
        document.getElementById("grantEmail").scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (act === "ref") {
        document.getElementById("refBalanceEmail").value = btn.dataset.email || "";
        document.getElementById("refBalanceAmount").focus();
        document.getElementById("refBalanceEmail").scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (act === "block") {
        const blocked = btn.dataset.blocked !== "1";
        if (blocked && !confirm(`Заблокировать ${btn.dataset.email || uid}?`)) return;
        try {
          await api("POST", `/api/admin/users/${encodeURIComponent(uid)}/block`, { blocked });
          showAlert(blocked ? "Заблокирован" : "Разблокирован", "ok");
          await load();
        } catch (err) { showAlert(err.message); }
        return;
      }
      if (act === "delete") {
        const email = btn.dataset.email || uid;
        const ok = confirm(
          `Удалить аккаунт ${email}?\n\n` +
          "Логин перестанет работать. VDS пользователя исчезнут из панели (сами серверы не удаляются)."
        );
        if (!ok) return;
        try {
          const res = await api("DELETE", `/api/admin/users/${encodeURIComponent(uid)}`);
          showAlert(`Удалён: ${res.email || email}`, "ok");
          await load();
        } catch (err) { showAlert(err.message); }
      }
    });

    document.getElementById("admPromosBody")?.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-promo-toggle]");
      if (!btn) return;
      const code = btn.dataset.promoToggle;
      const active = btn.dataset.active === "1";
      try {
        await api("PATCH", `/api/admin/promos/${encodeURIComponent(code)}`, { active });
        await load();
      } catch (err) { showAlert(err.message); }
    });

    document.getElementById("admChangelogForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("POST", "/api/admin/changelog", {
          version: document.getElementById("clVersion").value.trim(),
          date: document.getElementById("clDate").value.trim(),
          title: document.getElementById("clTitle").value.trim(),
          tags: document.getElementById("clTags").value.trim(),
          body: document.getElementById("clBody").value.trim(),
        });
        showAlert("Запись опубликована", "ok");
        e.target.reset();
        const cl = await api("GET", "/api/admin/changelog");
        renderChangelog(cl.items);
      } catch (err) { showAlert(err.message); }
    });

    await load();
  }

  init().catch((e) => showAlert(e.message));
})();
