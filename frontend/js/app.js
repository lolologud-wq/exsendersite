// =======================================================
//  exsender — frontend
//  Site backend (web/) → proxies to bots on VDS
// =======================================================

const FETCH_OPTS = { credentials: "same-origin", cache: "no-store" };

const EMPTY_OVERVIEW = {
  activeAccountId: null,
  totals: {
    accounts: 0, running: 0, connected: 0, authorized: 0,
    healthy: 0, dead: 0, chats: 0, chatsEnabled: 0,
    messages: 0, messagesQuota: 0, messagesRemaining: 0,
    withProxy: 0, withSource: 0,
  },
  accounts: [],
};

const STATE = {
  overview: null,
  allOverviews: [],
  allChats: [],
  statusFilter: "all",
  search: "",
  pollTimer: null,
  view: "dashboard",
  bots: [],
  selectedBotId: localStorage.getItem("selectedBotId") || "",
  dashboardBotId: localStorage.getItem("dashboardBotId") || "",
  accountsBotFilter: "",
  chatsBotFilter: "",
  chatsAccountFilter: "",
  chatsSearch: "",
  settingsBotId: "",
  settingsAccountKey: "",
  sourcesBotId: "",
  sourcesAccountKey: "",
  sourcesChatContext: null,
  sourcesChannels: [],
  serverLogPollTimer: null,
  serverLogTarget: null,
  selectedAccounts: new Set(),
  activityCache: null,
};

// =======================================================
//  HTTP — site API
// =======================================================
async function siteApi(method, path, body) {
  const opts = { ...FETCH_OPTS, method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.error || data?.detail || `HTTP ${res.status}`;
    if (typeof msg !== "string") msg = JSON.stringify(msg);
    throw new Error(msg);
  }
  return data;
}

function requireBot(botId) {
  const bid = botId || STATE.selectedBotId;
  if (!bid) {
    throw new Error("Выбери VDS");
  }
  return bid;
}

function botProxyUrl(sub, botId) {
  const bid = requireBot(botId);
  return `/api/bots/${encodeURIComponent(bid)}/proxy/${String(sub).replace(/^\//, "")}`;
}

async function botApi(method, sub, body, botId) {
  return siteApi(method, botProxyUrl(sub, botId), body);
}

async function fetchOverviewForBot(botId) {
  const wrap = await siteApi("GET", `/api/bots/${encodeURIComponent(botId)}/overview`);
  if (!wrap.reachable) throw new Error(wrap.error || "бот недоступен");
  return wrap.data || EMPTY_OVERVIEW;
}

const fetchOverview = async () => {
  const bid = STATE.dashboardBotId || STATE.selectedBotId;
  if (!bid) {
    const packs = await fetchAllOverviews();
    return aggregateOverview(packs);
  }
  return fetchOverviewForBot(bid);
};
const fetchAccount       = (aid, botId)        => botApi("GET",    `/accounts/${encodeURIComponent(aid)}`, undefined, botId);
const fetchAccountDialogs = (aid, botId)       => botApi("GET",    `/accounts/${encodeURIComponent(aid)}/dialogs`, undefined, botId);
const fetchAccountChannels = (aid, botId)      => botApi("GET",    `/accounts/${encodeURIComponent(aid)}/channels`, undefined, botId);
const resolvePostLink      = (aid, url, botId) => botApi("POST",   `/accounts/${encodeURIComponent(aid)}/resolve_post`, { url }, botId);
const addChat            = (aid, body, botId)  => botApi("POST",   `/accounts/${encodeURIComponent(aid)}/chats`, body, botId);
const createSlot         = (body, botId)       => botApi("POST",   "/accounts", body, botId);
const deleteSlot         = (aid, botId)        => botApi("DELETE", `/accounts/${encodeURIComponent(aid)}`, undefined, botId);
const activateSlot       = (aid, botId)        => botApi("POST",   `/accounts/${encodeURIComponent(aid)}/activate`, {}, botId);
const patchSlot          = (aid, body, botId)  => botApi("PATCH",  `/accounts/${encodeURIComponent(aid)}`, body, botId);
const setSpam            = (aid, running, botId) => botApi("POST", `/accounts/${encodeURIComponent(aid)}/spam`, { running }, botId);
const patchChat          = (aid, cid, body, botId) => botApi("PATCH", `/accounts/${encodeURIComponent(aid)}/chats/${encodeURIComponent(cid)}`, body, botId);
const removeChat         = (aid, cid, botId)   => botApi("DELETE", `/accounts/${encodeURIComponent(aid)}/chats/${encodeURIComponent(cid)}`, undefined, botId);
const authSendCode       = (aid, phone, botId) => botApi("POST",   `/accounts/${encodeURIComponent(aid)}/auth/send_code`, { phone }, botId);
const authSignIn         = (aid, code, pwd, botId) => botApi("POST", `/accounts/${encodeURIComponent(aid)}/auth/sign_in`, { code, password: pwd || undefined }, botId);
const auth2FA            = (aid, password, botId) => botApi("POST", `/accounts/${encodeURIComponent(aid)}/auth/2fa`, { password }, botId);

async function uploadSessionFile(slotId, file, proxy, botId) {
  const bid = requireBot(botId);
  const fd = new FormData();
  fd.append("slot_id", slotId);
  fd.append("proxy", proxy || "");
  fd.append("session_file", file);
  const res = await fetch(
    `/api/bots/${encodeURIComponent(bid)}/accounts/upload_session`,
    { method: "POST", credentials: "same-origin", body: fd },
  );
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.error || data?.detail || data?.hint || `HTTP ${res.status}`;
    if (typeof msg !== "string") msg = JSON.stringify(msg);
    throw new Error(msg);
  }
  return data;
}

// bots registry (VDS)
const listBots          = ()                 => siteApi("GET",    "/api/bots").then((r) => r.bots || []);
const addBot            = (body)             => siteApi("POST",   "/api/bots", body);
const removeBot         = (bid)              => siteApi("DELETE", `/api/bots/${encodeURIComponent(bid)}`);
const deployBot         = (bid, body)        => siteApi("POST",   `/api/bots/${encodeURIComponent(bid)}/deploy`, body);
const restartBot        = (bid, body)        => siteApi("POST",   `/api/bots/${encodeURIComponent(bid)}/restart`, body || {});
const stopBot           = (bid, body)        => siteApi("POST",   `/api/bots/${encodeURIComponent(bid)}/stop`, body || {});
const uninstallBot      = (bid, body)        => siteApi("POST",   `/api/bots/${encodeURIComponent(bid)}/uninstall`, body || {});
const fetchDeployLog    = (bid)              => siteApi("GET",    `/api/bots/${encodeURIComponent(bid)}/deploy/log`);
const fetchActivity     = (days, botId, account) => {
  let q = `?days=${encodeURIComponent(days || 14)}`;
  if (account) q += `&account=${encodeURIComponent(account)}`;
  return botApi("GET", `/activity${q}`, undefined, botId);
};
const checkProxy        = (proxy, botId)     => botApi("POST",   "/proxy/check", { proxy: proxy || "" }, botId);

function accountKey(botId, accountId) {
  return `${botId}:${accountId}`;
}

function parseAccountKey(key) {
  const i = String(key).indexOf(":");
  if (i < 0) return { botId: "", accountId: key };
  return { botId: key.slice(0, i), accountId: key.slice(i + 1) };
}

function proxyFieldHtml(name = "proxy", placeholder = "login:pass@host:port", extra = "") {
  return `
    <label class="field">
      <span class="field-label">Прокси</span>
      <div class="proxy-field-row">
        <input class="input mono" name="${escapeHtml(name)}" placeholder="${escapeHtml(placeholder)}" ${extra} />
        <button type="button" class="btn-ghost proxy-check-btn">Проверить</button>
      </div>
      <span class="field-hint proxy-check-result" hidden></span>
    </label>`;
}

async function runProxyCheck(inputEl, resultEl, botIdGetter) {
  const proxy = String(inputEl?.value || "").trim();
  const botId = typeof botIdGetter === "function" ? botIdGetter() : botIdGetter;
  if (!botId) { toastErr(new Error("Выбери VDS")); return; }
  if (resultEl) {
    resultEl.hidden = false;
    resultEl.textContent = "Проверяю…";
    resultEl.className = "field-hint proxy-check-result";
  }
  try {
    const res = await checkProxy(proxy, botId);
    if (resultEl) {
      resultEl.textContent = res.message || (res.ok ? "OK" : "Ошибка");
      resultEl.classList.toggle("ok", !!res.ok);
      resultEl.classList.toggle("err", !res.ok);
    }
    if (res.ok) toastOk(res.message || "Прокси OK");
    else toastErr(new Error(res.message || res.error || "Прокси недоступен"));
  } catch (e) {
    if (resultEl) {
      resultEl.textContent = e.message;
      resultEl.classList.add("err");
    }
    toastErr(e);
  }
}

function bindProxyCheckIn(root, botIdGetter) {
  root?.querySelectorAll(".proxy-check-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const field = btn.closest(".field");
      const input = field?.querySelector('input[name="proxy"]') || field?.querySelector(".input.mono");
      const result = field?.querySelector(".proxy-check-result");
      runProxyCheck(input, result, botIdGetter);
    });
  });
}

function botLabel(botId) {
  const b = STATE.bots.find((x) => x.id === botId);
  return b ? (b.alias || b.host) : botId;
}

function botSelectOptions(selectedId, { allowEmpty = false, emptyLabel = "— выбери VDS —" } = {}) {
  let html = allowEmpty ? `<option value="">${escapeHtml(emptyLabel)}</option>` : "";
  for (const b of STATE.bots) {
    const mark = b.reachable ? "●" : "○";
    const label = b.alias || b.host;
    html += `<option value="${escapeHtml(b.id)}"${b.id === selectedId ? " selected" : ""}>${mark} ${escapeHtml(label)}</option>`;
  }
  return html;
}

function aggregateOverview(packs) {
  const totals = { ...EMPTY_OVERVIEW.totals };
  const accounts = [];
  let activeAccountId = null;
  for (const { bot, overview } of packs) {
    const t = overview.totals || {};
    for (const k of Object.keys(totals)) {
      if (typeof totals[k] === "number" && typeof t[k] === "number") totals[k] += t[k];
    }
    for (const a of overview.accounts || []) {
      accounts.push({ ...a, botId: bot.id, botLabel: bot.alias || bot.host });
    }
    if (!activeAccountId && overview.activeAccountId) activeAccountId = overview.activeAccountId;
  }
  return { activeAccountId, totals, accounts };
}

async function fetchAllOverviews() {
  await loadBots();
  const packs = [];
  for (const bot of STATE.bots) {
    if (!bot.hasApiToken && bot.status === "new") continue;
    try {
      const overview = await fetchOverviewForBot(bot.id);
      packs.push({ bot, overview });
    } catch (e) {
      console.warn("overview failed", bot.id, e);
    }
  }
  STATE.allOverviews = packs;
  return packs;
}

function flattenAccounts() {
  const rows = [];
  for (const { bot, overview } of STATE.allOverviews) {
    for (const a of overview.accounts || []) {
      rows.push({
        ...a,
        botId: bot.id,
        botLabel: bot.alias || bot.host,
        activeAccountId: overview.activeAccountId,
      });
    }
  }
  return rows;
}

// =======================================================
//  Helpers
// =======================================================
function fmtNumber(n) { return Number(n || 0).toLocaleString("ru-RU"); }

function fmtMinutes(m) {
  if (m == null) return "—";
  if (m >= 60) {
    const h = Math.floor(m / 60);
    const rest = Math.round(m % 60);
    return rest ? `${h} ч ${rest} мин` : `${h} ч`;
  }
  return `${Math.round(m * 10) / 10} мин`;
}
function fmtJitter(j) {
  if (!j) return "";
  return ` ±${Math.round(j * 100)}%`;
}
function fmtSourceId(id) { return id == null ? null : String(id); }
function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function pluralRu(n, [one, few, many]) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5)   return few;
  if (b === 1)          return one;
  return many;
}

// =======================================================
//  Toasts
// =======================================================
function toast(msg, kind = "info", timeout = 3200) {
  const root = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = msg;
  root.appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  setTimeout(() => {
    el.classList.remove("in");
    setTimeout(() => el.remove(), 250);
  }, timeout);
}
const toastErr = (e) => toast(e?.message || String(e), "err", 4500);
const toastOk  = (m) => toast(m, "ok");

// =======================================================
//  Filtering
// =======================================================
function matchStatusFilter(a) {
  switch (STATE.statusFilter) {
    case "running":  return a.spamRunning;
    case "stopped":  return !a.spamRunning && a.chats.total > 0;
    case "empty":    return a.chats.total === 0;
    case "problems": return a.health ? a.health.tone === "error" : false;
    default:         return true;
  }
}

function applyAccountsFilter(accounts) {
  const q = STATE.search.trim().toLowerCase();
  return accounts.filter((a) => {
    if (STATE.accountsBotFilter && a.botId !== STATE.accountsBotFilter) return false;
    if (q && !a.id.toLowerCase().includes(q) && !(a.botLabel || "").toLowerCase().includes(q)) return false;
    return matchStatusFilter(a);
  });
}

/** @deprecated use applyAccountsFilter */
function applyFilter(accounts) {
  return applyAccountsFilter(accounts);
}

function vdsFieldHtml(selectedId, fieldId = "modalBotId") {
  return `
    <label class="field">
      <span class="field-label">VDS <em>обязательно</em></span>
      <select class="input" id="${fieldId}" name="botId" required>
        ${botSelectOptions(selectedId, { allowEmpty: true, emptyLabel: "— выбери VDS —" })}
      </select>
      <span class="field-hint">Сессия и слот будут созданы на выбранном сервере</span>
    </label>
  `;
}

function defaultBotId() {
  return STATE.accountsBotFilter
    || STATE.dashboardBotId
    || STATE.selectedBotId
    || (STATE.bots.length === 1 ? STATE.bots[0].id : "");
}

function findAccount(aid, botId) {
  for (const { bot, overview } of STATE.allOverviews) {
    if (botId && bot.id !== botId) continue;
    const a = (overview.accounts || []).find((x) => x.id === aid);
    if (a) {
      return {
        ...a,
        botId: bot.id,
        botLabel: bot.alias || bot.host,
        activeAccountId: overview.activeAccountId,
      };
    }
  }
  return null;
}

function accountMessagesSent(a) {
  const persisted = Number(a?.messagesSent) || 0;
  const session = Number(a?.health?.sendsTotal) || 0;
  return Math.max(persisted, session);
}

function overviewMessagesTotal(ov) {
  const fromTotals = Number(ov?.totals?.messages) || 0;
  const fromAccounts = (ov?.accounts || []).reduce((s, a) => s + accountMessagesSent(a), 0);
  return Math.max(fromTotals, fromAccounts);
}

function overviewMessageQuota(ov) {
  const t = ov?.totals || {};
  let quota = Number(t.messagesQuota) || 0;
  let remaining = Number(t.messagesRemaining) || 0;
  if (!quota && Array.isArray(ov?.accounts)) {
    for (const a of ov.accounts) {
      quota += Number(a.messagesQuota) || 0;
      remaining += Number(a.messagesRemaining) || 0;
    }
  }
  const sentInQuota = quota > 0 ? Math.max(0, quota - remaining) : 0;
  return {
    sent: sentInQuota,
    remaining,
    quota,
    totalSent: overviewMessagesTotal(ov),
  };
}

const RING_RADIUS = 42;
const RING_CIRC = 2 * Math.PI * RING_RADIUS;

function renderStats(ov) {
  const t = ov.totals;
  document.getElementById("statRunning").textContent     = fmtNumber(t.healthy ?? t.running);
  const dead = t.dead || 0;
  document.getElementById("statRunningSub").textContent  =
    dead
      ? `из ${fmtNumber(t.accounts)} · ${fmtNumber(dead)} ${pluralRu(dead, ["с проблемой", "с проблемами", "с проблемами"])}`
      : `из ${fmtNumber(t.accounts)} ${pluralRu(t.accounts, ["аккаунта", "аккаунтов", "аккаунтов"])}`;
  document.getElementById("statAccounts").textContent    = fmtNumber(t.accounts);
  document.getElementById("statAccountsSub").textContent = `${fmtNumber(t.withProxy)} с прокси · ${fmtNumber(t.withSource)} с источником`;
  document.getElementById("statChats").textContent       = fmtNumber(t.chatsEnabled);
  document.getElementById("statChatsSub").textContent    = `из ${fmtNumber(t.chats)} привязанных`;
  document.getElementById("statMessages").textContent    = fmtNumber(overviewMessagesTotal(ov));
  document.getElementById("statMessagesSub").textContent = `по ${fmtNumber(t.accounts)} ${pluralRu(t.accounts, ["слоту", "слотам", "слотам"])}`;
}

function renderSidebarStatus(ov) {
  const t = ov.totals;
  const active = ov.accounts.find((a) => a.id === ov.activeAccountId);
  document.getElementById("botActiveSlot").textContent = ov.activeAccountId || "—";
  const dead = t.dead || 0;
  const parts = [
    `${t.healthy || 0} живых`,
    `${dead} ${pluralRu(dead, ["проблема", "проблемы", "проблем"])}`,
  ];
  document.getElementById("botRunningMeta").textContent = parts.join(" · ");
  const dot = document.getElementById("botStatusDot");
  const activeOk = active?.health ? active.health.tone === "ok" : !!active?.spamRunning;
  dot.classList.toggle("on",  activeOk);
  dot.classList.toggle("off", !activeOk);
}

function renderActiveCard(ov) {
  const acc = ov.accounts.find((a) => a.id === ov.activeAccountId) || ov.accounts[0];
  if (!acc) {
    document.getElementById("activeName").textContent     = "Нет данных";
    document.getElementById("activeStatus").textContent   = "—";
    document.getElementById("activeInterval").textContent = "—";
    document.getElementById("activeProxy").textContent    = "—";
    document.getElementById("activeSource").textContent   = "—";
    document.getElementById("activeChip").textContent     = "—";
    renderRing(0, 0);
    return;
  }

  document.getElementById("activeName").textContent = acc.id;

  const chip = document.getElementById("activeChip");
  chip.textContent = acc.spamRunning ? "spam: ON" : "spam: OFF";
  chip.classList.toggle("on",  acc.spamRunning);
  chip.classList.toggle("off", !acc.spamRunning);

  document.getElementById("activeStatus").innerHTML = healthBadge(acc);

  document.getElementById("activeInterval").textContent =
    fmtMinutes(acc.defaultIntervalMin) + fmtJitter(acc.defaultIntervalJitter);

  document.getElementById("activeProxy").textContent =
    acc.hasProxy ? (acc.proxy || "configured") : "не задан";

  const src = fmtSourceId(acc.globalSourceChannelId);
  document.getElementById("activeSource").textContent =
    src ? (acc.globalSourceMessageId != null ? `${src} · msg ${acc.globalSourceMessageId}` : src) : "не задан";

  renderRing(acc.chats.enabled, acc.chats.total);
}

function renderRing(enabled, total) {
  const ringFg = document.getElementById("ringFg");
  const label  = document.getElementById("ringLabel");
  const cap    = document.getElementById("ringCaption");
  if (!ringFg || !label) return;
  const pct = total > 0 ? Math.round((enabled / total) * 100) : 0;
  const offset = RING_CIRC * (1 - pct / 100);
  ringFg.setAttribute("stroke-dasharray", `${RING_CIRC} ${RING_CIRC}`);
  ringFg.setAttribute("stroke-dashoffset", String(offset));
  ringFg.style.opacity = total > 0 ? "1" : "0.35";
  label.textContent = `${pct}%`;
  if (cap) {
    cap.textContent = `${fmtNumber(enabled)} / ${fmtNumber(total)} ${pluralRu(total, ["чат", "чата", "чатов"])}`;
  }
}

function pieSlicePath(cx, cy, r, startDeg, endDeg) {
  const toRad = (deg) => ((deg - 90) * Math.PI) / 180;
  const sweep = endDeg - startDeg;
  if (sweep >= 359.99) {
    return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.001} ${cy - r} Z`;
  }
  const x1 = cx + r * Math.cos(toRad(startDeg));
  const y1 = cy + r * Math.sin(toRad(startDeg));
  const x2 = cx + r * Math.cos(toRad(endDeg));
  const y2 = cy + r * Math.sin(toRad(endDeg));
  const large = sweep > 180 ? 1 : 0;
  return `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
}

function renderPieSvg(segments, opts = {}) {
  const cx = 50;
  const cy = 50;
  const r = 42;
  const hole = 26;
  let angle = 0;
  const total = segments.reduce((s, seg) => s + Math.max(0, Number(seg.value) || 0), 0) || 1;
  const paths = segments
    .filter((seg) => Number(seg.value) > 0)
    .map((seg) => {
      const val = Number(seg.value) || 0;
      const start = angle;
      const end = angle + (val / total) * 360;
      angle = end;
      return `<path class="${seg.cls}" d="${pieSlicePath(cx, cy, r, start, end)}"></path>`;
    })
    .join("");
  const holeCircle = opts.donut
    ? `<circle cx="${cx}" cy="${cy}" r="${hole}" class="pie-hole"></circle>`
    : "";
  const center = opts.center
    ? `<text x="${cx}" y="${opts.sub ? cy - 1 : cy + 4}" class="pie-center">${escapeHtml(opts.center)}</text>${
        opts.sub ? `<text x="${cx}" y="${cy + 12}" class="pie-center-sub">${escapeHtml(opts.sub)}</text>` : ""
      }`
    : "";
  return `<svg viewBox="0 0 100 100" class="pie-svg" aria-hidden="true">${paths}${holeCircle}${center}</svg>`;
}

function renderMessagesPie(ov) {
  const host = document.getElementById("messagesPieHost");
  const emptyEl = document.getElementById("messagesPieEmpty");
  const legend = document.getElementById("messagesPieLegend");
  if (!host) return;

  const { sent, remaining, quota, totalSent } = overviewMessageQuota(ov);

  if (quota <= 0) {
    host.hidden = false;
    if (emptyEl) emptyEl.hidden = true;
    if (legend) legend.textContent = `отправлено: ${fmtNumber(totalSent)}`;
    host.innerHTML = `
      ${renderPieSvg(
        [{ value: totalSent || 1, cls: "pie-sent" }],
        { donut: true, center: fmtNumber(totalSent) },
      )}
      <div class="pie-legend">
        <div class="pie-legend-item"><span class="pie-dot sent"></span>Отправлено <b>${fmtNumber(totalSent)}</b></div>
      </div>`;
    return;
  }

  const total = sent + remaining;
  if (total <= 0) {
    host.hidden = false;
    if (emptyEl) emptyEl.hidden = true;
    if (legend) legend.textContent = "отправлено: 0";
    host.innerHTML = `
      ${renderPieSvg([{ value: 1, cls: "pie-sent" }], { donut: true, center: "0" })}
      <div class="pie-legend">
        <div class="pie-legend-item"><span class="pie-dot sent"></span>Отправлено <b>0</b></div>
      </div>`;
    return;
  }

  host.hidden = false;
  if (emptyEl) emptyEl.hidden = true;

  const sentPct = Math.round((sent / total) * 100);
  const remPct = Math.max(0, 100 - sentPct);
  if (legend) legend.textContent = `${sentPct}% отправлено · ${remPct}% осталось`;

  host.innerHTML = `
    ${renderPieSvg(
      [
        { value: sent, cls: "pie-sent" },
        { value: remaining, cls: "pie-unsent" },
      ],
      { donut: true, center: `${sentPct}%`, sub: `${fmtNumber(sent)}/${fmtNumber(total)}` },
    )}
    <div class="pie-legend">
      <div class="pie-legend-item"><span class="pie-dot sent"></span>Отправлено <b>${fmtNumber(sent)}</b></div>
      <div class="pie-legend-item"><span class="pie-dot unsent"></span>Не отправлено <b>${fmtNumber(remaining)}</b></div>
    </div>`;
}

// =======================================================
//  Activity heatmap
// =======================================================
const HEATMAP_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function heatmapLevel(count, maxVal) {
  if (!count || count <= 0) return 0;
  if (!maxVal || maxVal <= 0) return 1;
  const ratio = count / maxVal;
  if (ratio >= 0.85) return 5;
  if (ratio >= 0.6) return 4;
  if (ratio >= 0.35) return 3;
  if (ratio >= 0.15) return 2;
  return 1;
}

function mergeActivityPayloads(list) {
  const dayMap = new Map();
  let total = 0;
  let max = 0;
  for (const item of list) {
    if (!item?.rows) continue;
    for (const row of item.rows) {
      const prev = dayMap.get(row.date) || Array(24).fill(0);
      const hours = row.hours.map((v, i) => {
        const n = (prev[i] || 0) + (v || 0);
        max = Math.max(max, n);
        return n;
      });
      dayMap.set(row.date, hours);
      total += row.total || 0;
    }
  }
  const rows = [...dayMap.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, hours]) => {
      const d = new Date(date + "T12:00:00");
      const dayTotal = hours.reduce((s, v) => s + v, 0);
      return {
        date,
        weekday: isNaN(d.getTime()) ? 0 : (d.getDay() + 6) % 7,
        hours,
        total: dayTotal,
      };
    });
  return { rows, total, max, days: rows.length };
}

async function fetchMergedActivity(days = 14) {
  await loadBots();
  const payloads = [];
  const bid = STATE.dashboardBotId;
  const bots = bid ? STATE.bots.filter((b) => b.id === bid) : STATE.bots;
  for (const bot of bots) {
    if (!bot.hasApiToken && bot.status === "new") continue;
    try {
      const data = await fetchActivity(days, bot.id);
      if (data?.rows) payloads.push(data);
    } catch (e) {
      console.warn("activity failed", bot.id, e);
    }
  }
  return mergeActivityPayloads(payloads);
}

function renderActivityHeatmap(data) {
  const host = document.getElementById("activityHeatmapHost");
  const legend = document.getElementById("heatmapLegend");
  if (!host) return;

  const rows = data?.rows || [];
  const maxVal = data?.max || 0;
  const total = data?.total || 0;

  if (!rows.length) {
    host.innerHTML = `<div class="chart-empty">Пока нет отправок за выбранный период</div>`;
    if (legend) legend.textContent = "14 дней · по часам";
    return;
  }

  if (legend) {
    legend.textContent = `${fmtNumber(total)} отправок · max ${maxVal}/ч`;
  }

  const hourLabels = Array.from({ length: 24 }, (_, h) =>
    `<span class="hm-h">${h % 6 === 0 ? String(h).padStart(2, "0") : ""}</span>`,
  ).join("");

  host.innerHTML = `
    <div class="heatmap-grid">
      <div class="heatmap-hour-labels"><span></span>${hourLabels}</div>
      ${rows.map((row) => {
        const label = HEATMAP_DOW[row.weekday] || row.date.slice(5);
        return `
          <div class="heatmap-row" title="${escapeHtml(row.date)} · ${row.total} отправок">
            <span class="heatmap-row-label">${escapeHtml(label)}</span>
            ${row.hours.map((c, h) => {
              const lv = heatmapLevel(c, maxVal);
              const tip = `${row.date} ${String(h).padStart(2, "0")}:00 — ${c} msg`;
              return `<span class="heatmap-cell" data-level="${lv}" title="${escapeHtml(tip)}"></span>`;
            }).join("")}
          </div>`;
      }).join("")}
    </div>
    <div class="heatmap-legend">
      <span>меньше</span>
      <div class="heatmap-legend-scale">
        ${[0, 1, 2, 3, 4, 5].map((lv) => `<i data-level="${lv}"></i>`).join("")}
      </div>
      <span>больше</span>
    </div>`;

  host.querySelectorAll(".heatmap-legend-scale i").forEach((el) => {
    const lv = el.dataset.level;
    const sample = host.querySelector(`.heatmap-cell[data-level="${lv}"]`);
    if (sample) el.style.background = getComputedStyle(sample).backgroundColor;
  });
}

async function refreshActivityHeatmap() {
  try {
    const data = await fetchMergedActivity(14);
    STATE.activityCache = data;
    renderActivityHeatmap(data);
  } catch (e) {
    const host = document.getElementById("activityHeatmapHost");
    if (host) host.innerHTML = `<div class="chart-empty">${escapeHtml(e.message)}</div>`;
  }
}

// =======================================================
//  Accounts — bulk, swipe, sessions
// =======================================================
function updateBulkBar() {
  const bar = document.getElementById("accountsBulkBar");
  const countEl = document.getElementById("bulkCount");
  const n = STATE.selectedAccounts.size;
  if (bar) bar.hidden = n === 0;
  if (countEl) countEl.textContent = `${n} ${pluralRu(n, ["выбран", "выбрано", "выбрано"])}`;
}

function toggleAccountSelection(key, on) {
  if (on) STATE.selectedAccounts.add(key);
  else STATE.selectedAccounts.delete(key);
  updateBulkBar();
}

function getSelectedAccountRows() {
  return [...STATE.selectedAccounts].map(parseAccountKey).filter((x) => x.botId && x.accountId);
}

async function bulkSetSpam(running) {
  const rows = getSelectedAccountRows();
  if (!rows.length) return;
  let ok = 0;
  for (const { botId, accountId } of rows) {
    try {
      await setSpam(accountId, running, botId);
      ok++;
    } catch (e) {
      console.warn("bulk spam", accountId, e);
    }
  }
  toastOk(`${running ? "Старт" : "Стоп"}: ${ok}/${rows.length}`);
  STATE.selectedAccounts.clear();
  updateBulkBar();
  refreshAccounts(true);
}

async function bulkDeleteSelected() {
  const rows = getSelectedAccountRows();
  if (!rows.length) return;
  if (!confirm(`Удалить ${rows.length} ${pluralRu(rows.length, ["аккаунт", "аккаунта", "аккаунтов"])}?`)) return;
  let ok = 0;
  for (const { botId, accountId } of rows) {
    try {
      await deleteSlot(accountId, botId);
      ok++;
    } catch (e) {
      console.warn("bulk delete", accountId, e);
    }
  }
  toastOk(`Удалено: ${ok}/${rows.length}`);
  STATE.selectedAccounts.clear();
  updateBulkBar();
  refreshAccounts(true);
}

function renderSessionStatusList() {
  const host = document.getElementById("sessionStatusList");
  if (!host) return;
  const rows = flattenAccounts();
  if (!rows.length) {
    host.innerHTML = `<div class="field-hint">Нет слотов — загрузи .session или создай аккаунт</div>`;
    return;
  }
  host.innerHTML = rows.map((a) => {
    const auth = a.authorized
      ? `<span class="chip on">авторизован</span>`
      : `<span class="chip off">нет входа</span>`;
    const spam = a.spamRunning ? `<span class="chip on">spam</span>` : `<span class="chip">stop</span>`;
    return `
      <div class="session-status-item">
        <div>
          <span class="cell-strong">${escapeHtml(a.id)}</span>
          <span class="mono cell-dim"> · ${escapeHtml(a.botLabel || botLabel(a.botId))}</span>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">${auth}${spam}</div>
      </div>`;
  }).join("");
}

function bindAccountSwipeRows() {
  if (!window.matchMedia("(max-width: 900px)").matches) return;
  document.querySelectorAll("#accountsBody tr.account-row").forEach((row) => {
    if (row.dataset.swipeBound) return;
    row.dataset.swipeBound = "1";
    const id = row.dataset.id;
    const bot = row.dataset.bot;
    const acc = findAccount(id, bot) || { spamRunning: false };

    let actions = row.querySelector(".account-swipe-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "account-swipe-actions";
      actions.innerHTML = `
        <button class="ra ra-${acc.spamRunning ? "stop" : "start"}" data-swipe="spam" title="${acc.spamRunning ? "Стоп" : "Старт"}">
          ${acc.spamRunning
            ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>'
            : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>'}
        </button>
        <button class="ra" data-swipe="settings" title="Настройки">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>
        </button>`;
      row.appendChild(actions);
    }

    actions.querySelector('[data-swipe="spam"]')?.addEventListener("click", (e) => {
      e.stopPropagation();
      handleRowAction("spam", id, bot);
      row.classList.remove("swipe-open");
    });
    actions.querySelector('[data-swipe="settings"]')?.addEventListener("click", (e) => {
      e.stopPropagation();
      handleRowAction("settings", id, bot);
      row.classList.remove("swipe-open");
    });

    let startX = 0;
    let tracking = false;
    row.addEventListener("touchstart", (e) => {
      if (e.target.closest("input, button, .ra, label.switch")) return;
      startX = e.touches[0].clientX;
      tracking = true;
    }, { passive: true });
    row.addEventListener("touchmove", (e) => {
      if (!tracking) return;
      const dx = e.touches[0].clientX - startX;
      if (dx < -40) row.classList.add("swipe-open");
      if (dx > 40) row.classList.remove("swipe-open");
    }, { passive: true });
    row.addEventListener("touchend", () => { tracking = false; });
  });
}

function bindSessionManager() {
  const zone = document.getElementById("sessionDropZone");
  const input = document.getElementById("sessionFileInput");
  const btn = document.getElementById("sessionUploadBtn");
  if (!zone || !input) return;

  const pickFile = (file) => {
    if (!file) return;
    if (!file.name.endsWith(".session")) {
      toastErr(new Error("Нужен файл .session"));
      return;
    }
    const base = file.name.replace(/\.session$/i, "");
    openUploadSessionModal(base, file);
  };

  btn?.addEventListener("click", (e) => {
    e.stopPropagation();
    input.click();
  });
  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    pickFile(input.files?.[0]);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) => {
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
    });
  });
  zone.addEventListener("drop", (e) => {
    pickFile(e.dataTransfer?.files?.[0]);
  });
}

function bindBulkActions() {
  document.getElementById("bulkStart")?.addEventListener("click", () => bulkSetSpam(true));
  document.getElementById("bulkStop")?.addEventListener("click", () => bulkSetSpam(false));
  document.getElementById("bulkDelete")?.addEventListener("click", () => bulkDeleteSelected());
  document.getElementById("bulkClear")?.addEventListener("click", () => {
    STATE.selectedAccounts.clear();
    document.querySelectorAll(".acc-select").forEach((cb) => { cb.checked = false; });
    const all = document.getElementById("accountsSelectAll");
    if (all) all.checked = false;
    updateBulkBar();
  });
  document.getElementById("bulkSelectAll")?.addEventListener("click", () => {
    flattenAccounts().forEach((a) => STATE.selectedAccounts.add(accountKey(a.botId, a.id)));
    renderAccountsTable();
  });
  document.getElementById("accountsSelectAll")?.addEventListener("change", (e) => {
    const on = e.target.checked;
    flattenAccounts().forEach((a) => {
      const k = accountKey(a.botId, a.id);
      if (on) STATE.selectedAccounts.add(k);
      else STATE.selectedAccounts.delete(k);
    });
    renderAccountsTable();
  });
}

// =======================================================
//  Chart (native bars — no external CDN)
// =======================================================
function renderChart(accounts) {
  const host = document.getElementById("accountsChartHost");
  const emptyEl = document.getElementById("chartEmpty");
  const legend = document.getElementById("chartLegend");
  if (!host) return;

  const list = (accounts || [])
    .slice()
    .sort((a, b) => (b.chats?.total || 0) - (a.chats?.total || 0))
    .slice(0, 12);

  if (!list.length) {
    host.hidden = true;
    host.innerHTML = "";
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.textContent = "Нет аккаунтов для графика";
    }
    if (legend) legend.textContent = "включено / всего";
    return;
  }

  host.hidden = false;
  if (emptyEl) emptyEl.hidden = true;

  const maxVal = Math.max(1, ...list.map((a) => a.chats?.total || 0));
  const totalEnabled = list.reduce((s, a) => s + (a.chats?.enabled || 0), 0);
  const totalChats = list.reduce((s, a) => s + (a.chats?.total || 0), 0);
  if (legend) legend.textContent = `${fmtNumber(totalEnabled)} вкл / ${fmtNumber(totalChats)} всего`;

  host.innerHTML = `
    <div class="bar-chart-rows">
      ${list.map((a) => {
        const total = a.chats?.total || 0;
        const on = a.chats?.enabled || 0;
        const off = Math.max(0, total - on);
        const label = a.botLabel ? `${a.id} · ${a.botLabel}` : a.id;
        const onW = total ? (on / maxVal) * 100 : 0;
        const offW = total ? (off / maxVal) * 100 : 0;
        return `
          <div class="bar-chart-row" title="${escapeHtml(label)}: ${on} вкл / ${total} всего">
            <span class="bar-chart-label">${escapeHtml(label)}</span>
            <div class="bar-chart-track">
              ${on ? `<span class="bar-chart-on" style="width:${onW}%"></span>` : ""}
              ${off ? `<span class="bar-chart-off" style="width:${offW}%"></span>` : ""}
            </div>
            <span class="bar-chart-val mono">${on}/${total}</span>
          </div>`;
      }).join("")}
    </div>
    <div class="bar-chart-legend">
      <span><i class="bar-chart-dot on"></i> Включено</span>
      <span><i class="bar-chart-dot off"></i> Отключено</span>
    </div>
  `;
}

// =======================================================
//  Accounts table
// =======================================================
const HEALTH_BADGE_CLASS = {
  ok:    "badge-paid",
  warn:  "badge-pending",
  error: "badge-failed",
};

function fmtRelAgo(ts) {
  if (!ts) return "";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60)   return `${sec}с назад`;
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`;
  return `${Math.floor(sec / 86400)} д назад`;
}

function healthBadge(acc) {
  const h = acc.health;
  if (!h) {
    if (acc.spamRunning) return '<span class="badge badge-paid">работает</span>';
    if (acc.chats.total === 0) return '<span class="badge badge-failed">пусто</span>';
    return '<span class="badge badge-pending">остановлен</span>';
  }
  const cls = HEALTH_BADGE_CLASS[h.tone] || "badge-pending";
  const title = [
    h.lastError ? `${h.lastErrorKind || "error"}: ${h.lastError}` : "",
    h.lastSendAt ? `последняя отправка: ${fmtRelAgo(h.lastSendAt)}` : "",
    h.sendsTotal ? `всего отправок: ${h.sendsTotal}` : "",
  ].filter(Boolean).join(" · ");
  const sub = h.lastError
    ? `<div class="cell-mute" style="margin-top:4px; font-size: 11px; max-width: 220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(h.lastError)}">${escapeHtml(h.lastErrorKind || "error")}: ${escapeHtml(h.lastError)}</div>`
    : (h.lastSendAt
        ? `<div class="cell-mute" style="margin-top:4px; font-size: 11px;">отправка ${escapeHtml(fmtRelAgo(h.lastSendAt))}</div>`
        : "");
  return `<span class="badge ${cls}" title="${escapeHtml(title)}">${escapeHtml(h.label)}</span>${sub}`;
}

// kept for backwards-compat / unused
function statusBadge(acc) { return healthBadge(acc); }

function rowActions(acc) {
  const bid = acc.botId || STATE.selectedBotId || "";
  const isActive = acc.id === acc.activeAccountId;
  const dataBot = ` data-bot="${escapeHtml(bid)}"`;
  const loginBtn = !acc.authorized
    ? `<button class="ra ra-start" data-act="login" data-id="${escapeHtml(acc.id)}"${dataBot} title="Войти через сайт">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/></svg>
      </button>`
    : "";
  return `
    <div class="row-actions">
      ${loginBtn}
      <button class="ra ra-${acc.spamRunning ? "stop" : "start"}" data-act="spam" data-id="${escapeHtml(acc.id)}"${dataBot} title="${acc.spamRunning ? "Стоп" : "Старт"}">
        ${acc.spamRunning
          ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>'
          : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>'}
      </button>
      <button class="ra ${isActive ? "ra-on" : ""}" data-act="activate" data-id="${escapeHtml(acc.id)}"${dataBot} title="${isActive ? "Активен" : "Сделать активным"}">
        <svg viewBox="0 0 24 24" fill="${isActive ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.6">
          <path d="m12 17.3-6.2 3.7 1.6-7L2 9.2l7.1-.6L12 2l2.9 6.6 7.1.6-5.4 4.8 1.6 7Z"/>
        </svg>
      </button>
      <button class="ra" data-act="settings" data-id="${escapeHtml(acc.id)}"${dataBot} title="Настройки аккаунта">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>
      </button>
      <button class="ra" data-act="chats" data-id="${escapeHtml(acc.id)}"${dataBot} title="Чаты">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 5h18M3 12h18M3 19h12"/></svg>
      </button>
      <button class="ra ra-danger" data-act="delete" data-id="${escapeHtml(acc.id)}"${dataBot} title="Удалить">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>
      </button>
    </div>
  `;
}

function renderAccountsTable() {
  const body = document.getElementById("accountsBody");
  if (!body) return;
  const all = flattenAccounts();
  const filtered = applyAccountsFilter(all);
  const countEl = document.getElementById("accountsCount");
  if (countEl) countEl.textContent = `${filtered.length} / ${all.length}`;

  if (!all.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty-row">Нет аккаунтов — добавь через «+ Аккаунт» или загрузи .session</td></tr>`;
    return;
  }
  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty-row">Нет аккаунтов под текущий фильтр</td></tr>`;
    return;
  }

  body.innerHTML = filtered.map((a) => {
    const isActive = a.id === a.activeAccountId;
    const initials = a.id.slice(0, 2).toUpperCase();
    const key = accountKey(a.botId, a.id);
    const checked = STATE.selectedAccounts.has(key);
    return `
      <tr class="account-row ${isActive ? "row-active" : ""}" data-id="${escapeHtml(a.id)}" data-bot="${escapeHtml(a.botId)}">
        <td class="td-check" data-label="">
          <input type="checkbox" class="acc-select" data-key="${escapeHtml(key)}" ${checked ? "checked" : ""} aria-label="Выбрать ${escapeHtml(a.id)}" />
        </td>
        <td data-label="VDS"><span class="cell-strong">${escapeHtml(a.botLabel || botLabel(a.botId))}</span></td>
        <td data-label="Аккаунт">
          <div class="course-cell">
            <div class="course-thumb account-thumb">${escapeHtml(initials)}</div>
            <div class="account-meta">
              <span class="account-name">${escapeHtml(a.id)}${isActive ? ' <span class="tag-active">active</span>' : ""}</span>
              <span class="account-sub">${a.chats.withCustomInterval} с интервалом · ${a.chats.withCustomText} с текстом</span>
            </div>
          </div>
        </td>
        <td data-label="Статус">${statusBadge(a)}</td>
        <td data-label="Чаты"><span class="cell-strong">${fmtNumber(a.chats.enabled)}</span><span class="cell-dim"> / ${fmtNumber(a.chats.total)}</span></td>
        <td data-label="Интервал">${fmtMinutes(a.defaultIntervalMin)}${fmtJitter(a.defaultIntervalJitter)}</td>
        <td data-label="Прокси">${a.hasProxy ? `<span class="mono cell-dim">${escapeHtml(a.proxy || "configured")}</span>` : '<span class="cell-mute">нет</span>'}</td>
        <td data-label="Отправлено"><span class="cell-strong">${fmtNumber(accountMessagesSent(a))}</span></td>
        <td data-label="Действия" class="td-actions">${rowActions(a)}</td>
      </tr>
    `;
  }).join("");

  body.querySelectorAll(".acc-select").forEach((cb) => {
    cb.addEventListener("change", () => toggleAccountSelection(cb.dataset.key, cb.checked));
  });
  const allCb = document.getElementById("accountsSelectAll");
  if (allCb) {
    allCb.checked = filtered.length > 0 && filtered.every((a) => STATE.selectedAccounts.has(accountKey(a.botId, a.id)));
  }
  bindAccountSwipeRows();
  updateBulkBar();
}

// =======================================================
//  Modal
// =======================================================
function openModal({ title, body, footer }) {
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalBody").innerHTML    = body;
  document.getElementById("modalFoot").innerHTML    = footer || "";
  const root = document.getElementById("modalRoot");
  root.hidden = false;
  requestAnimationFrame(() => root.classList.add("in"));
}
function closeModal() {
  const root = document.getElementById("modalRoot");
  root.classList.remove("in");
  setTimeout(() => { root.hidden = true; }, 180);
  if (STATE.serverLogPollTimer) {
    clearInterval(STATE.serverLogPollTimer);
    STATE.serverLogPollTimer = null;
  }
  STATE.serverLogTarget = null;
  document.getElementById("modalPanel")?.classList.remove("modal-wide");
}

function modalActions(submitLabel, opts = {}) {
  const danger = opts.danger ? "btn-danger" : "btn-primary";
  return `
    <button class="btn-ghost" data-modal-close>Отмена</button>
    <button class="${danger}" id="modalSubmit">${escapeHtml(submitLabel)}</button>
  `;
}

// =======================================================
//  Add slot modal
// =======================================================
function openAddSlotModal() {
  if (!STATE.bots.length) {
    toastErr(new Error("Сначала добавь VDS через «Серверы → + VDS»"));
    return;
  }
  const presetBot = defaultBotId();
  openModal({
    title: "Добавить аккаунт",
    body: `
      <form class="form-grid" id="addSlotForm" autocomplete="off">
        ${vdsFieldHtml(presetBot)}
        <label class="field">
          <span class="field-label">ID слота <em>обязательно</em></span>
          <input class="input mono" name="id" placeholder="например, alex42" pattern="[a-zA-Z][a-zA-Z0-9_-]{0,31}" required />
          <span class="field-hint">латиница/цифры/«_»/«-», 1–32 символа, первая буква</span>
        </label>
        ${proxyFieldHtml("proxy", "login:pass@host:port или socks5://…")}
        <div class="field-row">
          <label class="field">
            <span class="field-label">Интервал, мин</span>
            <input class="input" name="intervalMin" type="number" min="0.1" step="0.1" placeholder="5" />
          </label>
          <label class="field">
            <span class="field-label">Jitter, %</span>
            <input class="input" name="intervalJitter" type="number" min="0" max="95" step="1" placeholder="0" />
          </label>
        </div>
        <label class="field">
          <span class="field-label">Текст по умолчанию</span>
          <textarea class="input" name="defaultMessage" rows="3" placeholder="(опционально)"></textarea>
        </label>
        <div class="callout">
          После создания откроется вход через сайт (телефон → код из Telegram).
          Или загрузи готовый <b>.session</b> через кнопку «.session».
        </div>
      </form>
    `,
    footer: modalActions("Создать"),
  });

  bindProxyCheckIn(document.getElementById("modalBody"), () => {
    const f = document.getElementById("addSlotForm");
    return f ? String(new FormData(f).get("botId") || "").trim() : defaultBotId();
  });

  document.getElementById("modalSubmit").addEventListener("click", async () => {
    const f = document.getElementById("addSlotForm");
    const fd = new FormData(f);
    const botId = String(fd.get("botId") || "").trim();
    if (!botId) { toastErr(new Error("Выбери VDS")); return; }
    const slotId = String(fd.get("id") || "").trim();
    if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,31}$/.test(slotId)) {
      toastErr(new Error("ID слота: первая буква латиница, дальше буквы/цифры/_/-"));
      return;
    }
    const body = {
      id: slotId,
      proxy: String(fd.get("proxy") || "").trim() || null,
      intervalMin: fd.get("intervalMin") ? Number(fd.get("intervalMin")) : null,
      intervalJitter: fd.get("intervalJitter") ? Number(fd.get("intervalJitter")) / 100 : null,
      defaultMessage: String(fd.get("defaultMessage") || "").trim() || null,
    };
    Object.keys(body).forEach((k) => body[k] == null && delete body[k]);
    try {
      const res = await createSlot(body, botId);
      toastOk(`Слот «${res.id}» создан на ${botLabel(botId)}`);
      closeModal();
      await refreshCurrentView(true);
      openAuthModal(res.id, botId);
    } catch (e) { toastErr(e); }
  });
}

// =======================================================
//  Edit slot — removed: use Settings tab
// =======================================================

async function goToSettingsTab(botId, accountId) {
  STATE.settingsBotId = botId || "";
  STATE.settingsAccountKey = accountId ? chatAccountKey(botId, accountId) : "";
  if (STATE.view !== "settings") switchView("settings", { skipRefresh: true });
  syncSettingsSelects();
  await loadSettingsForm();
}

async function goToSourcesTab(botId, accountId, chatId) {
  STATE.sourcesBotId = botId || "";
  STATE.sourcesAccountKey = accountId ? chatAccountKey(botId, accountId) : "";
  STATE.sourcesChatContext = chatId ? { botId, accountId, chatId: String(chatId) } : null;
  if (STATE.view !== "sources") switchView("sources", { skipRefresh: true });
  syncSourcesSelects();
  renderSourcesContextUI();
  await refreshSources();
}

// =======================================================
//  Telethon auth via site (phone → code → 2FA)
// =======================================================
function bindModalSubmit(handler) {
  const old = document.getElementById("modalSubmit");
  if (!old) return;
  const btn = old.cloneNode(true);
  old.replaceWith(btn);
  btn.addEventListener("click", handler);
  return btn;
}

function openAuthModal(aid, botId) {
  const bid = botId || defaultBotId();
  if (!bid) { toastErr(new Error("Выбери VDS")); return; }

  const flow = { step: "phone", phone: "" };

  function renderPhone() {
    openModal({
      title: `Вход в Telegram: ${aid}`,
      body: `
        <form class="form-grid" id="authForm" autocomplete="off">
          <div class="callout">VDS: <b>${escapeHtml(botLabel(bid))}</b>. Код придёт в Telegram или SMS.</div>
          <label class="field">
            <span class="field-label">Номер телефона <em>обязательно</em></span>
            <input class="input mono" id="authPhone" type="tel" placeholder="+79001234567" required />
            <span class="field-hint">Международный формат с «+»</span>
          </label>
        </form>
      `,
      footer: modalActions("Отправить код"),
    });
    setTimeout(() => document.getElementById("authPhone")?.focus(), 50);
    bindModalSubmit(async () => {
      const phone = document.getElementById("authPhone")?.value.trim();
      if (!phone) { toastErr(new Error("Введи номер телефона")); return; }
      const btn = document.getElementById("modalSubmit");
      btn.disabled = true;
      try {
        await authSendCode(aid, phone, bid);
        flow.phone = phone;
        flow.step = "code";
        renderCode();
        toastOk("Код отправлен");
      } catch (e) {
        toastErr(e);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderCode() {
    document.getElementById("modalTitle").textContent = `Код для ${aid}`;
    document.getElementById("modalBody").innerHTML = `
      <form class="form-grid" id="authForm" autocomplete="off">
        <div class="callout">Код отправлен на <b>${escapeHtml(flow.phone)}</b> · ${escapeHtml(botLabel(bid))}</div>
        <label class="field">
          <span class="field-label">Код из Telegram / SMS</span>
          <input class="input mono" id="authCode" inputmode="numeric" placeholder="12345" required />
        </label>
        <label class="field">
          <span class="field-label">2FA пароль <em>если включён</em></span>
          <input class="input mono" id="auth2faInline" type="password" autocomplete="current-password" placeholder="можно сразу, если знаешь" />
        </label>
      </form>
    `;
    document.getElementById("modalFoot").innerHTML = `
      <button class="btn-ghost" id="authBackBtn">← Назад</button>
      <button class="btn-ghost" data-modal-close>Отмена</button>
      <button class="btn-primary" id="modalSubmit">Войти</button>
    `;
    document.getElementById("authBackBtn")?.addEventListener("click", () => {
      flow.step = "phone";
      renderPhone();
    });
    setTimeout(() => document.getElementById("authCode")?.focus(), 50);
    bindModalSubmit(async () => {
      const code = document.getElementById("authCode")?.value.trim();
      if (!code) { toastErr(new Error("Введи код")); return; }
      const pwd = document.getElementById("auth2faInline")?.value.trim() || null;
      const btn = document.getElementById("modalSubmit");
      btn.disabled = true;
      try {
        const res = await authSignIn(aid, code, pwd, bid);
        if (res.need2FA) {
          flow.step = "2fa";
          render2FA();
          toast("Нужен пароль 2FA", "info");
          return;
        }
        toastOk(`Вошли как @${res.tgUsername || res.tgUserId || aid}`);
        closeModal();
        refreshCurrentView(true);
      } catch (e) {
        toastErr(e);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function render2FA() {
    document.getElementById("modalTitle").textContent = `2FA: ${aid}`;
    document.getElementById("modalBody").innerHTML = `
      <form class="form-grid" id="authForm" autocomplete="off">
        <div class="callout">На аккаунте включена двухфакторная защита. VDS: <b>${escapeHtml(botLabel(bid))}</b></div>
        <label class="field">
          <span class="field-label">Пароль 2FA</span>
          <input class="input mono" id="auth2faPwd" type="password" autocomplete="current-password" required />
        </label>
      </form>
    `;
    document.getElementById("modalFoot").innerHTML = modalActions("Подтвердить");
    setTimeout(() => document.getElementById("auth2faPwd")?.focus(), 50);
    bindModalSubmit(async () => {
      const password = document.getElementById("auth2faPwd")?.value.trim();
      if (!password) { toastErr(new Error("Введи пароль 2FA")); return; }
      const btn = document.getElementById("modalSubmit");
      btn.disabled = true;
      try {
        const res = await auth2FA(aid, password, bid);
        toastOk(`Вошли как @${res.tgUsername || res.tgUserId || aid}`);
        closeModal();
        refreshCurrentView(true);
      } catch (e) {
        toastErr(e);
      } finally {
        btn.disabled = false;
      }
    });
  }

  renderPhone();
}

// =======================================================
//  Upload .session file
// =======================================================
function openUploadSessionModal(presetSlotId, presetFile) {
  if (!STATE.bots.length) {
    toastErr(new Error("Сначала добавь VDS через «Серверы → + VDS»"));
    return;
  }
  const presetBot = defaultBotId();
  const fileHint = presetFile
    ? `<div class="callout">Файл: <b>${escapeHtml(presetFile.name)}</b> (${Math.round(presetFile.size / 1024)} KiB)</div>`
    : "";
  openModal({
    title: "Загрузить .session",
    body: `
      <form class="form-grid" id="uploadSessionForm" autocomplete="off">
        <div class="callout">
          Файл <code>имя.session</code> с Telethon будет загружен на выбранный VDS.
        </div>
        ${fileHint}
        ${vdsFieldHtml(presetBot)}
        <label class="field">
          <span class="field-label">ID слота <em>обязательно</em></span>
          <input class="input mono" name="slotId" value="${escapeHtml(presetSlotId || "")}" placeholder="acc1" pattern="[a-zA-Z][a-zA-Z0-9_-]{0,31}" required />
        </label>
        ${proxyFieldHtml("proxy", "login:pass@host:port (опционально)")}
        <label class="field" id="sessionFileField" ${presetFile ? "hidden" : ""}>
          <span class="field-label">Файл .session <em>обязательно</em></span>
          <input class="input" name="sessionFile" type="file" accept=".session" ${presetFile ? "" : "required"} />
        </label>
      </form>
    `,
    footer: modalActions("Загрузить"),
  });

  if (presetFile) {
    const form = document.getElementById("uploadSessionForm");
    form.dataset.presetFile = "1";
  }

  bindProxyCheckIn(document.getElementById("modalBody"), () => {
    const f = document.getElementById("uploadSessionForm");
    return f ? String(new FormData(f).get("botId") || "").trim() : presetBot;
  });

  bindModalSubmit(async () => {
    const f = document.getElementById("uploadSessionForm");
    if (!f.reportValidity()) return;
    const fd = new FormData(f);
    const botId = String(fd.get("botId") || "").trim();
    if (!botId) { toastErr(new Error("Выбери VDS")); return; }
    const slotId = String(fd.get("slotId") || "").trim();
    if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,31}$/.test(slotId)) {
      toastErr(new Error("ID слота: первая буква латиница"));
      return;
    }
    const file = presetFile || fd.get("sessionFile");
    if (!file || !file.size) { toastErr(new Error("Выбери .session файл")); return; }
    const btn = document.getElementById("modalSubmit");
    btn.disabled = true;
    try {
      const res = await uploadSessionFile(slotId, file, String(fd.get("proxy") || "").trim(), botId);
      if (res.authorized) {
        toastOk(`@${res.tgUsername || slotId} — сессия на ${botLabel(botId)}`);
      } else {
        toast(res.hint || "Файл загружен, но авторизация не подтверждена", "info", 5000);
      }
      closeModal();
      refreshCurrentView(true);
    } catch (e) {
      toastErr(e);
    } finally {
      btn.disabled = false;
    }
  });
}

// =======================================================
//  Confirm delete
// =======================================================
function openDeleteSlotModal(aid, botId) {
  const acc = findAccount(aid, botId);
  openModal({
    title: `Удалить аккаунт ${aid}?`,
    body: `
      <p class="modal-text">
        Будет удалена запись слота на <b>${escapeHtml(acc?.botLabel || botLabel(botId) || "VDS")}</b>,
        файл сессии Telethon и все привязки чатов. Действие необратимо.
      </p>
    `,
    footer: modalActions("Удалить", { danger: true }),
  });
  document.getElementById("modalSubmit").addEventListener("click", async () => {
    try {
      await deleteSlot(aid, botId || acc?.botId);
      toastOk(`Слот «${aid}» удалён`);
      closeModal();
      refreshCurrentView(true);
    } catch (e) { toastErr(e); }
  });
}

// =======================================================
//  Chats tab (all VDS / accounts)
// =======================================================
function chatAccountKey(botId, accountId) {
  return `${botId}|${accountId}`;
}

function parseChatAccountKey(key) {
  const [botId, accountId] = String(key || "").split("|");
  return { botId, accountId };
}

function updateChatsAccountFilterOptions() {
  const sel = document.getElementById("chatsAccountFilter");
  if (!sel) return;
  const prev = STATE.chatsAccountFilter;
  const rows = flattenAccounts().filter((a) => !STATE.chatsBotFilter || a.botId === STATE.chatsBotFilter);
  let html = `<option value="">Все аккаунты</option>`;
  const seen = new Set();
  for (const a of rows) {
    const val = chatAccountKey(a.botId, a.id);
    if (seen.has(val)) continue;
    seen.add(val);
    html += `<option value="${escapeHtml(val)}"${val === prev ? " selected" : ""}>${escapeHtml(a.id)} · ${escapeHtml(a.botLabel || botLabel(a.botId))}</option>`;
  }
  if (prev && !seen.has(prev)) {
    const { botId, accountId } = parseChatAccountKey(prev);
    const label = `${accountId} · ${botLabel(botId)}`;
    html += `<option value="${escapeHtml(prev)}" selected>${escapeHtml(label)}</option>`;
  }
  sel.innerHTML = html;
}

function syncChatsFilterSelects() {
  const botSel = document.getElementById("chatsBotFilter");
  if (botSel) botSel.value = STATE.chatsBotFilter;
  updateChatsAccountFilterOptions();
  const accSel = document.getElementById("chatsAccountFilter");
  if (accSel) accSel.value = STATE.chatsAccountFilter;
}

function allChatRow(row) {
  const c = row.chat;
  const interval = c.customIntervalMin
    ? `${fmtMinutes(c.customIntervalMin)}${fmtJitter(c.customIntervalJitter)}`
    : '<span class="cell-mute">—</span>';
  const hasCustomText = (c.customMessage && c.customMessage.trim()) || (c.textVariants && c.textVariants.length);
  const limit = c.messageLimit != null
    ? `<span class="mono cell-dim">${c.messagesSent || 0}/${c.messageLimit}</span>`
    : `<span class="mono cell-dim">${c.messagesSent || 0}</span>`;
  const title = c.title && c.title !== c.chatId
    ? `<div class="cell-strong">${escapeHtml(c.title)}</div>`
    : "";
  return `
    <tr data-cid="${escapeHtml(c.chatId)}" data-bot="${escapeHtml(row.botId)}" data-aid="${escapeHtml(row.accountId)}">
      <td data-label="Вкл" class="td-toggle"><label class="switch"><input type="checkbox" data-act="toggle" ${c.enabled ? "checked" : ""} /><span></span></label></td>
      <td data-label="VDS"><span class="cell-strong">${escapeHtml(row.botLabel || botLabel(row.botId))}</span></td>
      <td data-label="Аккаунт"><span class="mono">${escapeHtml(row.accountId)}</span></td>
      <td data-label="Группа">
        ${title}
        <span class="mono cell-dim">${escapeHtml(c.chatId)}</span>
        ${c.configured === false ? '<span class="badge badge-pending" style="margin-left:6px">new</span>' : ""}
      </td>
      <td data-label="Интервал">${interval}</td>
      <td data-label="Текст">${hasCustomText ? '<span class="badge badge-pending">custom</span>' : '<span class="cell-mute">—</span>'}</td>
      <td data-label="Отправлено">${limit}</td>
      <td data-label="Действия" class="td-actions">
        <button class="ra" data-act="cfg" title="Настройки чата">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>
        </button>
        <button class="ra ra-danger" data-act="del" title="Удалить чат" ${c.configured === false ? "disabled" : ""}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>
        </button>
      </td>
    </tr>
  `;
}

function renderAllChatsTable() {
  const body = document.getElementById("allChatsBody");
  if (!body) return;
  const q = STATE.chatsSearch.trim().toLowerCase();
  let rows = STATE.allChats;
  if (STATE.chatsBotFilter) rows = rows.filter((r) => r.botId === STATE.chatsBotFilter);
  if (STATE.chatsAccountFilter) {
    const { botId, accountId } = parseChatAccountKey(STATE.chatsAccountFilter);
    rows = rows.filter((r) => r.botId === botId && r.accountId === accountId);
  }
  if (q) {
    rows = rows.filter((r) =>
      r.chat.chatId.toLowerCase().includes(q)
      || (r.chat.title || "").toLowerCase().includes(q),
    );
  }

  const enabled = rows.filter((r) => r.chat.enabled).length;
  const countEl = document.getElementById("chatsCount");
  if (countEl) countEl.textContent = `${enabled} вкл / ${rows.length} чатов`;

  if (!rows.length) {
    const hint = STATE.chatsAccountFilter || STATE.chatsBotFilter
      ? "Нет групп — аккаунт авторизован? Нажми «Обновить» или добавь чат вручную"
      : "Выбери аккаунт в фильтре или открой чаты из строки аккаунта";
    body.innerHTML = `<tr><td colspan="8" class="empty-row">${hint}</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 500).map(allChatRow).join("");
}

async function refreshChats(force = false) {
  try {
    await fetchAllOverviews();
    syncChatsFilterSelects();
    let accounts = flattenAccounts();
    if (STATE.chatsBotFilter) accounts = accounts.filter((a) => a.botId === STATE.chatsBotFilter);
    if (STATE.chatsAccountFilter) {
      const { botId, accountId } = parseChatAccountKey(STATE.chatsAccountFilter);
      accounts = accounts.filter((a) => a.botId === botId && a.id === accountId);
    }

    const rows = [];
    for (const a of accounts) {
      try {
        const dialogs = await fetchAccountDialogs(a.id, a.botId);
        for (const chat of dialogs) {
          rows.push({
            botId: a.botId,
            botLabel: a.botLabel,
            accountId: a.id,
            chat,
          });
        }
      } catch (e) {
        if (force && e?.message?.includes("404")) {
          toast("На VDS старая версия бота — передеплой через «Серверы → Deploy»", "info", 6000);
        } else if (force) console.warn("dialogs failed", a.id, e);
        try {
          const acc = await fetchAccount(a.id, a.botId);
          for (const chat of (acc.chatList || [])) {
            rows.push({ botId: a.botId, botLabel: a.botLabel, accountId: a.id, chat });
          }
        } catch (e2) {
          if (force) console.warn("account chats failed", a.id, e2);
        }
      }
    }
    STATE.allChats = rows;
    renderAllChatsTable();
  } catch (e) {
    if (force) toastErr(e);
  }
  if (force) spinRefreshBtn();
}

async function goToChatsTab(botId, accountId) {
  STATE.chatsBotFilter = botId || "";
  STATE.chatsAccountFilter = accountId ? chatAccountKey(botId, accountId) : "";
  if (STATE.view !== "chats") switchView("chats", { skipRefresh: true });
  await refreshChats(true);
  syncChatsFilterSelects();
}

function openAddChatModal(preset) {
  const { botId = defaultBotId(), accountId = "" } = preset || {};
  if (!STATE.bots.length) {
    toastErr(new Error("Сначала добавь VDS"));
    return;
  }
  const accRows = flattenAccounts();
  openModal({
    title: "Добавить чат",
    body: `
      <form class="form-grid" id="addChatForm" autocomplete="off">
        ${vdsFieldHtml(botId, "addChatBotId")}
        <label class="field">
          <span class="field-label">Аккаунт <em>обязательно</em></span>
          <select class="input" id="addChatAccount" name="accountId" required>
            <option value="">— выбери аккаунт —</option>
            ${accRows.map((a) => {
              const val = chatAccountKey(a.botId, a.id);
              const sel = (a.botId === botId && a.id === accountId) ? " selected" : "";
              return `<option value="${escapeHtml(val)}"${sel}>${escapeHtml(a.id)} · ${escapeHtml(a.botLabel || botLabel(a.botId))}</option>`;
            }).join("")}
          </select>
        </label>
        <label class="field">
          <span class="field-label">Chat ID <em>обязательно</em></span>
          <input class="input mono" name="chatId" placeholder="-1001234567890" required />
          <span class="field-hint">ID группы из Telegram (отрицательное число для супергрупп)</span>
        </label>
        <label class="check">
          <input type="checkbox" name="enabled" checked />
          <span>Включить сразу</span>
        </label>
        <div class="callout">Список групп из Telegram подгружается автоматически на вкладке «Чаты» — выбери аккаунт в фильтре.</div>
      </form>
    `,
    footer: modalActions("Добавить"),
  });

  document.getElementById("addChatBotId")?.addEventListener("change", (e) => {
    const bid = e.target.value;
    const sel = document.getElementById("addChatAccount");
    if (!sel) return;
    [...sel.options].forEach((o) => {
      if (!o.value) return;
      const { botId: ob } = parseChatAccountKey(o.value);
      o.hidden = bid && ob !== bid;
    });
  });

  document.getElementById("modalSubmit").addEventListener("click", async () => {
    const f = document.getElementById("addChatForm");
    if (!f.reportValidity()) return;
    const fd = new FormData(f);
    const bid = String(document.getElementById("addChatBotId")?.value || fd.get("botId") || "").trim();
    const accKey = String(fd.get("accountId") || "").trim();
    const { botId: abid, accountId: aid } = parseChatAccountKey(accKey);
    const chatId = String(fd.get("chatId") || "").trim();
    if (!bid || !aid || !chatId) { toastErr(new Error("Заполни VDS, аккаунт и Chat ID")); return; }
    if (abid && abid !== bid) { toastErr(new Error("Аккаунт не на выбранном VDS")); return; }
    try {
      await addChat(aid, { chatId, enabled: !!fd.get("enabled") }, bid);
      toastOk(`Чат ${chatId} добавлен`);
      closeModal();
      STATE.chatsBotFilter = bid;
      STATE.chatsAccountFilter = chatAccountKey(bid, aid);
      if (STATE.view !== "chats") switchView("chats");
      else await refreshChats(true);
      syncChatsFilterSelects();
    } catch (e) { toastErr(e); }
  });
}

// =======================================================
//  Account settings tab
// =======================================================
function populateAccountSelect(sel, botId, selectedKey, emptyLabel) {
  if (!sel) return;
  const rows = flattenAccounts().filter((a) => !botId || a.botId === botId);
  let html = `<option value="">${escapeHtml(emptyLabel)}</option>`;
  for (const a of rows) {
    const val = chatAccountKey(a.botId, a.id);
    html += `<option value="${escapeHtml(val)}"${val === selectedKey ? " selected" : ""}>${escapeHtml(a.id)} · ${escapeHtml(a.botLabel || botLabel(a.botId))}</option>`;
  }
  sel.innerHTML = html;
}

function syncSettingsSelects() {
  const botSel = document.getElementById("settingsBotSelect");
  if (botSel) botSel.value = STATE.settingsBotId;
  populateAccountSelect(
    document.getElementById("settingsAccountSelect"),
    STATE.settingsBotId,
    STATE.settingsAccountKey,
    "— аккаунт —",
  );
}

function syncSourcesSelects() {
  const botSel = document.getElementById("sourcesBotSelect");
  if (botSel) botSel.value = STATE.sourcesBotId;
  populateAccountSelect(
    document.getElementById("sourcesAccountSelect"),
    STATE.sourcesBotId,
    STATE.sourcesAccountKey,
    "— аккаунт —",
  );
}

function settingsTarget() {
  const { botId, accountId } = parseChatAccountKey(STATE.settingsAccountKey);
  return { botId: botId || STATE.settingsBotId, accountId };
}

function sourcesTarget() {
  const { botId, accountId } = parseChatAccountKey(STATE.sourcesAccountKey);
  return { botId: botId || STATE.sourcesBotId, accountId };
}

function formatSourceLabel(channelId, messageId, forward) {
  if (channelId == null) return "не задан";
  const post = messageId != null ? ` · msg ${messageId}` : " · последний пост";
  const mode = forward ? "forward" : "копия";
  return `${channelId}${post} (${mode})`;
}

async function loadSettingsForm() {
  const f = document.getElementById("settingsForm");
  const hint = document.getElementById("settingsSourceHint");
  if (!f) return;
  const { botId, accountId } = settingsTarget();
  if (!botId || !accountId) {
    f.reset();
    if (hint) hint.textContent = "Выбери VDS и аккаунт в шапке.";
    return;
  }
  try {
    const acc = await fetchAccount(accountId, botId);
    f.elements.defaultMessage.value = acc.defaultMessage || "";
    f.elements.proxy.value = acc.rawProxy || "";
    f.elements.defaultIntervalMin.value = acc.defaultIntervalMin ?? 5;
    f.elements.defaultIntervalJitter.value = Math.round((acc.defaultIntervalJitter || 0) * 100);
    f.elements.defaultSourceForward.checked = !!acc.defaultSourceForward;
    if (hint) {
      hint.innerHTML = `Общий источник: <b>${escapeHtml(formatSourceLabel(acc.globalSourceChannelId, acc.globalSourceMessageId, acc.defaultSourceForward))}</b> — меняется во вкладке «Источники».`;
    }
  } catch (e) {
    toastErr(e);
  }
}

async function saveSettingsForm(ev) {
  ev?.preventDefault();
  const { botId, accountId } = settingsTarget();
  if (!botId || !accountId) { toastErr(new Error("Выбери VDS и аккаунт")); return; }
  const f = document.getElementById("settingsForm");
  const fd = new FormData(f);
  const body = {
    proxy: String(fd.get("proxy") || "").trim(),
    defaultIntervalMin: Number(fd.get("defaultIntervalMin")),
    defaultIntervalJitter: Number(fd.get("defaultIntervalJitter") || 0) / 100,
    defaultSourceForward: !!fd.get("defaultSourceForward"),
    defaultMessage: String(fd.get("defaultMessage") || ""),
  };
  try {
    await patchSlot(accountId, body, botId);
    toastOk("Настройки сохранены");
    loadSettingsForm();
  } catch (e) { toastErr(e); }
}

async function refreshSettings() {
  await fetchAllOverviews();
  syncSettingsSelects();
  await loadSettingsForm();
}

function renderSourcesContextUI() {
  const ctx = STATE.sourcesChatContext;
  const banner = document.getElementById("sourcesChatBanner");
  const hint = document.getElementById("sourcesPostHint");
  const clearChat = document.getElementById("sourcesClearChatBtn");
  const clearCtx = document.getElementById("sourcesClearContextBtn");
  if (ctx) {
    if (banner) {
      banner.style.display = "";
      banner.innerHTML = `Режим чата: <b>${escapeHtml(ctx.chatId)}</b> · ${escapeHtml(ctx.accountId)} @ ${escapeHtml(botLabel(ctx.botId))}. Каналы ниже назначаются <b>этому чату</b>.`;
    }
    if (hint) hint.textContent = "Ссылка и каналы применяются к выбранному чату.";
    clearChat?.removeAttribute("hidden");
    clearCtx?.removeAttribute("hidden");
  } else {
    if (banner) banner.style.display = "none";
    if (hint) hint.textContent = "Без режима чата — назначается общий источник аккаунта.";
    clearChat?.setAttribute("hidden", "");
    clearCtx?.setAttribute("hidden", "");
  }
}

async function renderSourcesStatus(acc) {
  const el = document.getElementById("sourcesStatus");
  if (!el || !acc) return;
  let chatLine = "";
  const ctx = STATE.sourcesChatContext;
  if (ctx) {
    try {
      const full = await fetchAccount(ctx.accountId, ctx.botId);
      const ch = (full.chatList || []).find((x) => x.chatId === ctx.chatId);
      if (ch?.sourceChannelId != null) {
        chatLine = `<br/>Источник чата: <b>${escapeHtml(formatSourceLabel(ch.sourceChannelId, ch.sourceMessageId, ch.sourceForward))}</b>`;
      } else {
        chatLine = "<br/>Источник чата: <i>не задан (используется общий или текст)</i>";
      }
    } catch { /* ignore */ }
  }
  el.innerHTML = `Общий источник: <b>${escapeHtml(formatSourceLabel(acc.globalSourceChannelId, acc.globalSourceMessageId, acc.defaultSourceForward))}</b>${chatLine}`;
}

async function refreshSources() {
  await fetchAllOverviews();
  syncSourcesSelects();
  renderSourcesContextUI();
  const body = document.getElementById("sourcesBody");
  const { botId, accountId } = sourcesTarget();
  if (!body) return;
  if (!botId || !accountId) {
    body.innerHTML = `<tr><td colspan="3" class="empty-row">Выбери VDS и аккаунт в шапке</td></tr>`;
    document.getElementById("sourcesCount").textContent = "0 каналов";
    const st = document.getElementById("sourcesStatus");
    if (st) st.textContent = "Выбери VDS и аккаунт в шапке.";
    return;
  }
  let acc;
  try {
    acc = await fetchAccount(accountId, botId);
    await renderSourcesStatus(acc);
    const channels = await fetchAccountChannels(accountId, botId);
    STATE.sourcesChannels = channels;
    document.getElementById("sourcesCount").textContent = `${channels.length} каналов`;
    const inChatMode = !!STATE.sourcesChatContext;
    if (!channels.length) {
      body.innerHTML = `<tr><td colspan="3" class="empty-row">Нет broadcast-каналов — используй ID вручную или t.me</td></tr>`;
      return;
    }
    body.innerHTML = channels.map((ch) => `
      <tr data-cid="${escapeHtml(ch.channelId)}">
        <td data-label="Канал"><span class="cell-strong">${escapeHtml(ch.title)}</span></td>
        <td data-label="ID"><span class="mono cell-dim">${escapeHtml(ch.channelId)}</span></td>
        <td data-label="Назначить" class="td-actions">
          <div class="row-actions">
            ${inChatMode
              ? `<button class="ra" data-src="chat" data-cid="${escapeHtml(ch.channelId)}" title="Источник для чата">чат</button>`
              : `<button class="ra" data-src="global" data-cid="${escapeHtml(ch.channelId)}" title="Общий источник">общий</button>`}
          </div>
        </td>
      </tr>
    `).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="3" class="empty-row">${escapeHtml(e.message)}</td></tr>`;
    if (STATE.view === "sources") toastErr(e);
  }
}

async function applySourceFromPost(url) {
  const { botId, accountId } = sourcesTarget();
  if (!botId || !accountId) { toastErr(new Error("Выбери аккаунт")); return; }
  const ctx = STATE.sourcesChatContext;
  try {
    const res = await resolvePostLink(accountId, url, botId);
    if (ctx) {
      await patchChat(ctx.accountId, ctx.chatId, {
        sourceChannelId: Number(res.channelId),
        sourceMessageId: res.messageId,
      }, ctx.botId);
      toastOk(`Источник чата ${ctx.chatId} обновлён`);
    } else {
      await patchSlot(accountId, {
        globalSourceChannelId: Number(res.channelId),
        globalSourceMessageId: res.messageId,
      }, botId);
      toastOk("Общий источник обновлён");
    }
    refreshSources();
  } catch (e) { toastErr(e); }
}

async function applyManualSource(channelId, messageId) {
  const { botId, accountId } = sourcesTarget();
  if (!botId || !accountId) return;
  const ctx = STATE.sourcesChatContext;
  const mid = messageId === "" ? null : Number(messageId);
  try {
    if (ctx) {
      await patchChat(ctx.accountId, ctx.chatId, {
        sourceChannelId: Number(channelId),
        sourceMessageId: mid,
      }, ctx.botId);
      toastOk(`Источник чата ${ctx.chatId}: ${channelId}`);
    } else {
      await patchSlot(accountId, {
        globalSourceChannelId: Number(channelId),
        globalSourceMessageId: mid,
      }, botId);
      toastOk(`Общий источник: ${channelId}`);
    }
    refreshSources();
  } catch (e) { toastErr(e); }
}

async function setGlobalChannelSource(channelId) {
  await applyManualSource(channelId, "");
}

async function setChatChannelSource(channelId) {
  const ctx = STATE.sourcesChatContext;
  if (!ctx) return;
  try {
    await patchChat(ctx.accountId, ctx.chatId, {
      sourceChannelId: Number(channelId),
      sourceMessageId: null,
    }, ctx.botId);
    toastOk(`Чат ${ctx.chatId}: канал ${channelId}`);
    refreshSources();
  } catch (e) { toastErr(e); }
}

// =======================================================
//  Per-chat settings modal
// =======================================================
function variantsToText(variants) {
  return (variants || []).join("\n---\n");
}

function textToVariants(text) {
  const t = String(text || "").trim();
  if (!t) return [];
  return t.split(/\n---\n/).map((x) => x.trim()).filter(Boolean);
}

async function openChatSettingsModal(botId, accountId, chatId, chat) {
  let c = chat;
  if (!c) {
    try {
      const acc = await fetchAccount(accountId, botId);
      c = (acc.chatList || []).find((x) => x.chatId === String(chatId));
    } catch (e) { toastErr(e); return; }
  }
  if (!c) c = { chatId: String(chatId), enabled: false };

  const title = c.title && c.title !== c.chatId ? `${c.title} · ${c.chatId}` : c.chatId;
  const sourceLabel = c.sourceChannelId != null
    ? formatSourceLabel(c.sourceChannelId, c.sourceMessageId, c.sourceForward)
    : "не задан (общий из «Источники» или текст)";

  openModal({
    title: `Чат: ${title}`,
    body: `
      <form class="form-grid" id="chatSettingsForm" autocomplete="off">
        <div class="callout">${escapeHtml(botLabel(botId))} · ${escapeHtml(accountId)}<br/>
        Текст, интервал и лимиты — здесь. Канал-источник — во вкладке <b>Источники</b>.</div>
        <label class="check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""} /><span>Включён в рассылке</span></label>
        <label class="field"><span class="field-label">Кастомное сообщение</span><textarea class="input" name="customMessage" rows="3" placeholder="пусто = стандартный текст из «Настройки»">${escapeHtml(c.customMessage || "")}</textarea></label>
        <label class="field"><span class="field-label">Несколько текстов (через строку ---)</span><textarea class="input" name="textVariants" rows="4" placeholder="вар1\n---\nвар2">${escapeHtml(variantsToText(c.textVariants))}</textarea></label>
        <label class="field"><span class="field-label">Доп. текст (только при копии, не forward)</span><textarea class="input" name="extraText" rows="2">${escapeHtml(c.extraText || "")}</textarea></label>
        <div class="field-row">
          <label class="field"><span class="field-label">Интервал, мин</span><input class="input" name="customIntervalMin" type="number" min="0.1" step="0.1" value="${c.customIntervalMin ?? ""}" placeholder="стандарт" /></label>
          <label class="field"><span class="field-label">Jitter, %</span><input class="input" name="customIntervalJitter" type="number" min="0" max="95" value="${c.customIntervalJitter != null ? Math.round(c.customIntervalJitter * 100) : ""}" placeholder="стандарт" /></label>
        </div>
        <div class="field-row">
          <label class="field"><span class="field-label">Лимит сообщений</span><input class="input" name="messageLimit" type="number" min="1" value="${c.messageLimit ?? ""}" placeholder="нет" /></label>
          <label class="field"><span class="field-label">Задержка старта, мин</span><input class="input" name="startDelayMin" type="number" min="0" step="0.1" value="${c.startDelayMin ?? ""}" placeholder="нет" /></label>
        </div>
        <label class="check"><input type="checkbox" name="sourceForward" ${c.sourceForward ? "checked" : ""} /><span>Источник: пересылка (forward)</span></label>
        <div class="callout cell-dim">Источник чата: <b>${escapeHtml(sourceLabel)}</b></div>
        <div class="callout">Отправлено: <b>${fmtNumber(c.messagesSent || 0)}</b>${c.messageLimit != null ? ` / ${c.messageLimit}` : ""}</div>
      </form>
    `,
    footer: `
      <button class="btn-ghost" data-act="chat-goto-source">Источник…</button>
      <button class="btn-ghost" data-act="chat-reset-text">Сброс текста</button>
      <button class="btn-ghost" data-modal-close>Отмена</button>
      <button class="btn-primary" id="modalSubmit">Сохранить</button>
    `,
  });
  document.getElementById("modalPanel").classList.add("modal-wide");

  document.getElementById("modalRoot").querySelector('[data-act="chat-goto-source"]')?.addEventListener("click", () => {
    closeModal();
    goToSourcesTab(botId, accountId, chatId);
  });

  document.getElementById("modalRoot").querySelector('[data-act="chat-reset-text"]')?.addEventListener("click", async () => {
    try {
      await patchChat(accountId, chatId, { customMessage: "-", textVariants: "-" }, botId);
      toastOk("Текст сброшен");
      closeModal();
      refreshCurrentView(true);
    } catch (e) { toastErr(e); }
  });

  bindModalSubmit(async () => {
    const f = document.getElementById("chatSettingsForm");
    const fd = new FormData(f);
    const body = {
      enabled: !!fd.get("enabled"),
      customMessage: String(fd.get("customMessage") || ""),
      textVariants: textToVariants(fd.get("textVariants")),
      extraText: String(fd.get("extraText") || ""),
      sourceForward: !!fd.get("sourceForward"),
    };
    const iv = String(fd.get("customIntervalMin") || "").trim();
    const jt = String(fd.get("customIntervalJitter") || "").trim();
    const lim = String(fd.get("messageLimit") || "").trim();
    const sd = String(fd.get("startDelayMin") || "").trim();
    body.customIntervalMin = iv === "" ? null : Number(iv);
    body.customIntervalJitter = jt === "" ? null : Number(jt) / 100;
    body.messageLimit = lim === "" ? null : Number(lim);
    body.startDelayMin = sd === "" ? null : Number(sd);
    try {
      await patchChat(accountId, chatId, body, botId);
      toastOk("Чат сохранён");
      closeModal();
      refreshCurrentView(true);
    } catch (e) { toastErr(e); }
  });
}

function bindSettingsAndSources() {
  document.getElementById("settingsBotSelect")?.addEventListener("change", (e) => {
    STATE.settingsBotId = e.target.value;
    STATE.settingsAccountKey = "";
    syncSettingsSelects();
    loadSettingsForm();
  });
  document.getElementById("settingsAccountSelect")?.addEventListener("change", (e) => {
    STATE.settingsAccountKey = e.target.value;
    loadSettingsForm();
  });
  document.getElementById("settingsForm")?.addEventListener("submit", saveSettingsForm);
  document.getElementById("settingsProxyCheck")?.addEventListener("click", () => {
    const f = document.getElementById("settingsForm");
    const input = f?.elements?.proxy;
    const result = document.getElementById("settingsProxyResult");
    const { botId } = settingsTarget();
    runProxyCheck(input, result, botId);
  });

  document.getElementById("sourcesBotSelect")?.addEventListener("change", (e) => {
    STATE.sourcesBotId = e.target.value;
    STATE.sourcesAccountKey = "";
    STATE.sourcesChatContext = null;
    syncSourcesSelects();
    renderSourcesContextUI();
    refreshSources();
  });
  document.getElementById("sourcesAccountSelect")?.addEventListener("change", (e) => {
    STATE.sourcesAccountKey = e.target.value;
    refreshSources();
  });
  document.getElementById("sourcesApplyPostBtn")?.addEventListener("click", async () => {
    const url = document.getElementById("sourcesPostUrl")?.value.trim();
    if (!url) return;
    await applySourceFromPost(url);
  });
  document.getElementById("sourcesApplyManualBtn")?.addEventListener("click", async () => {
    const ch = document.getElementById("sourcesManualChannelId")?.value.trim();
    const mid = document.getElementById("sourcesManualMsgId")?.value.trim() ?? "";
    if (!ch) { toastErr(new Error("Укажи ID канала")); return; }
    await applyManualSource(ch, mid);
  });
  document.getElementById("sourcesClearGlobalBtn")?.addEventListener("click", async () => {
    const { botId, accountId } = sourcesTarget();
    if (!botId || !accountId) return;
    try {
      await patchSlot(accountId, { globalSourceChannelId: null, globalSourceMessageId: null }, botId);
      toastOk("Общий источник сброшен");
      refreshSources();
    } catch (e) { toastErr(e); }
  });
  document.getElementById("sourcesClearChatBtn")?.addEventListener("click", async () => {
    const ctx = STATE.sourcesChatContext;
    if (!ctx) return;
    try {
      await patchChat(ctx.accountId, ctx.chatId, { sourceChannelId: null, sourceMessageId: null }, ctx.botId);
      toastOk("Источник чата сброшен");
      refreshSources();
    } catch (e) { toastErr(e); }
  });
  document.getElementById("sourcesClearContextBtn")?.addEventListener("click", () => {
    STATE.sourcesChatContext = null;
    renderSourcesContextUI();
    refreshSources();
  });
  document.getElementById("sourcesBody")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-src]");
    if (!btn) return;
    if (btn.dataset.src === "global") await setGlobalChannelSource(btn.dataset.cid);
    else if (btn.dataset.src === "chat") await setChatChannelSource(btn.dataset.cid);
  });
}

// =======================================================
//  Chats drill-down (legacy alias)
// =======================================================
function openChatsModal(aid, botId) {
  goToChatsTab(botId, aid);
}

// =======================================================
//  Row actions dispatcher
// =======================================================
async function handleRowAction(act, aid, botId) {
  const acc = findAccount(aid, botId) || { id: aid, botId, spamRunning: false };
  switch (act) {
    case "spam": {
      try {
        await setSpam(aid, !acc.spamRunning, botId || acc.botId);
        toastOk(acc.spamRunning ? `${aid}: спам остановлен` : `${aid}: спам запущен`);
        refreshCurrentView(true);
      } catch (e) { toastErr(e); }
      break;
    }
    case "activate": {
      try {
        await activateSlot(aid, botId || acc.botId);
        toastOk(`Активный слот: ${aid}`);
        refreshCurrentView(true);
      } catch (e) { toastErr(e); }
      break;
    }
    case "login":
      openAuthModal(aid, botId || acc.botId);
      break;
    case "settings":
      goToSettingsTab(botId || acc.botId, aid);
      break;
    case "chats":
      goToChatsTab(botId || acc.botId, aid);
      break;
    case "delete":
      openDeleteSlotModal(aid, botId || acc.botId);
      break;
  }
}

// =======================================================
//  Wiring
// =======================================================
function bindFilters() {
  document.querySelectorAll("#accountsStatusFilter .period-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#accountsStatusFilter .period-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      STATE.statusFilter = btn.dataset.status;
      if (STATE.view === "accounts") renderAccountsTable();
    });
  });
  const search = document.getElementById("accountSearch");
  search?.addEventListener("input", () => {
    STATE.search = search.value;
    if (STATE.view === "accounts") renderAccountsTable();
  });
  document.getElementById("refreshBtn")?.addEventListener("click", () => refreshCurrentView(true));
  document.getElementById("addSlotBtn")?.addEventListener("click", openAddSlotModal);
  document.getElementById("uploadSessionBtn")?.addEventListener("click", () => openUploadSessionModal());

  document.getElementById("accountsBotFilter")?.addEventListener("change", (e) => {
    STATE.accountsBotFilter = e.target.value;
    if (STATE.view === "accounts") renderAccountsTable();
  });
  document.getElementById("chatsBotFilter")?.addEventListener("change", (e) => {
    STATE.chatsBotFilter = e.target.value;
    STATE.chatsAccountFilter = "";
    updateChatsAccountFilterOptions();
    if (STATE.view === "chats") refreshChats(true);
  });
  document.getElementById("chatsAccountFilter")?.addEventListener("change", (e) => {
    STATE.chatsAccountFilter = e.target.value;
    if (STATE.view === "chats") refreshChats(true);
  });
  document.getElementById("addChatBtn")?.addEventListener("click", () => {
    const preset = STATE.chatsAccountFilter
      ? parseChatAccountKey(STATE.chatsAccountFilter)
      : { botId: STATE.chatsBotFilter || defaultBotId(), accountId: "" };
    openAddChatModal(preset);
  });
  document.getElementById("chatsSearch")?.addEventListener("input", (e) => {
    STATE.chatsSearch = e.target.value || "";
    if (STATE.view === "chats") renderAllChatsTable();
  });
}

function setSidebarOpen(open) {
  document.body.classList.toggle("sidebar-open", open);
  const btn = document.getElementById("menuBtn");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (backdrop) {
    backdrop.hidden = !open;
    backdrop.setAttribute("aria-hidden", open ? "false" : "true");
  }
}

function bindMobileNav() {
  document.getElementById("menuBtn")?.addEventListener("click", () => {
    setSidebarOpen(!document.body.classList.contains("sidebar-open"));
  });
  document.getElementById("sidebarBackdrop")?.addEventListener("click", () => setSidebarOpen(false));
  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) setSidebarOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) {
      setSidebarOpen(false);
    }
  });
}

function bindNav() {
  document.querySelectorAll(".nav-item").forEach((a) => {
    a.addEventListener("click", (e) => {
      if (!a.dataset.page) return;
      e.preventDefault();
      const v = a.dataset.page;
      if (VIEW_META[v]) {
        switchView(v);
        setSidebarOpen(false);
      } else {
        toast("Раздел в разработке", "info");
      }
    });
  });
}

function bindTable() {
  document.getElementById("accountsBody")?.addEventListener("click", (e) => {
    const btn = e.target.closest("button.ra");
    if (!btn) return;
    handleRowAction(btn.dataset.act, btn.dataset.id, btn.dataset.bot);
  });
}

function bindChatsTable() {
  document.getElementById("allChatsBody")?.addEventListener("click", async (e) => {
    const tr = e.target.closest("tr[data-cid]");
    if (!tr) return;
    const { cid, bot, aid } = tr.dataset;
    const actBtn = e.target.closest("[data-act]");
    if (!actBtn) return;

    if (actBtn.dataset.act === "cfg") {
      const row = STATE.allChats.find((r) => r.botId === bot && r.accountId === aid && r.chat.chatId === cid);
      openChatSettingsModal(bot, aid, cid, row?.chat);
      return;
    }

    if (actBtn.dataset.act === "toggle") {
      const want = actBtn.checked;
      try {
        await patchChat(aid, cid, { enabled: want }, bot);
        const row = STATE.allChats.find((r) => r.botId === bot && r.accountId === aid && r.chat.chatId === cid);
        if (row) {
          row.chat.enabled = want;
          row.chat.configured = true;
        }
        renderAllChatsTable();
      } catch (err) {
        actBtn.checked = !want;
        toastErr(err);
      }
    } else if (actBtn.dataset.act === "del") {
      if (!confirm(`Удалить чат ${cid} из ${aid}?`)) return;
      try {
        await removeChat(aid, cid, bot);
        STATE.allChats = STATE.allChats.filter(
          (r) => !(r.botId === bot && r.accountId === aid && r.chat.chatId === cid),
        );
        renderAllChatsTable();
        toastOk("Чат удалён");
      } catch (err) { toastErr(err); }
    }
  });
}

function bindModal() {
  document.getElementById("modalRoot").addEventListener("click", (e) => {
    if (e.target.closest("[data-modal-close]")) {
      const panel = document.getElementById("modalPanel");
      panel.classList.remove("modal-wide");
      closeModal();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("modalRoot").hidden) closeModal();
  });
}

function updateSyncStamp() {
  const t = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  document.getElementById("lastSyncText").textContent = `обновлено в ${t}`;
}

function spinRefreshBtn() {
  const btn = document.getElementById("refreshBtn");
  btn?.classList.add("spinning");
  setTimeout(() => btn?.classList.remove("spinning"), 600);
}

async function refreshAccounts(force = false) {
  try {
    await fetchAllOverviews();
    renderAccountsTable();
    renderSessionStatusList();
    updateChatsAccountFilterOptions();
  } catch (e) {
    if (force) toastErr(e);
  }
  if (force) spinRefreshBtn();
}

async function refreshCurrentView(force = false) {
  if (STATE.view === "accounts") return refreshAccounts(force);
  if (STATE.view === "chats") return refreshChats(force);
  if (STATE.view === "settings") return refreshSettings();
  if (STATE.view === "sources") return refreshSources();
  if (STATE.view === "servers") return refreshServers();
  return refresh(force);
}

async function refresh(force = false) {
  let data = EMPTY_OVERVIEW;
  try {
    if (STATE.dashboardBotId) {
      data = await fetchOverviewForBot(STATE.dashboardBotId);
    } else {
      const packs = await fetchAllOverviews();
      data = aggregateOverview(packs);
    }
  } catch (e) {
    if (force) toastErr(e);
  }
  STATE.overview = data;
  try {
    renderStats(data);
    renderSidebarStatus(data);
    renderActiveCard(data);
    renderChart(data.accounts || []);
    renderMessagesPie(data);
  } catch (e) {
    console.error("render error:", e);
  }
  updateSyncStamp();
  if (force) spinRefreshBtn();
}

function renderBotSelects() {
  const dash = document.getElementById("dashboardBotSelect");
  if (dash) {
    const prev = STATE.dashboardBotId;
    dash.innerHTML = `<option value="">Все VDS</option>` + STATE.bots.map((b) => {
      const label = b.alias || b.host;
      const st = b.reachable ? "●" : "○";
      return `<option value="${escapeHtml(b.id)}"${b.id === prev ? " selected" : ""}>${st} ${escapeHtml(label)}</option>`;
    }).join("");
    dash.value = STATE.bots.some((b) => b.id === prev) ? prev : "";
    STATE.dashboardBotId = dash.value;
    if (STATE.dashboardBotId) localStorage.setItem("dashboardBotId", STATE.dashboardBotId);
    else localStorage.removeItem("dashboardBotId");
  }

  const accF = document.getElementById("accountsBotFilter");
  if (accF) {
    const prev = STATE.accountsBotFilter;
    accF.innerHTML = `<option value="">Все VDS</option>` + STATE.bots.map((b) => {
      const label = b.alias || b.host;
      const st = b.reachable ? "●" : "○";
      return `<option value="${escapeHtml(b.id)}"${b.id === prev ? " selected" : ""}>${st} ${escapeHtml(label)}</option>`;
    }).join("");
    accF.value = STATE.bots.some((b) => b.id === prev) ? prev : "";
    STATE.accountsBotFilter = accF.value;
  }

  const chatsF = document.getElementById("chatsBotFilter");
  if (chatsF) {
    const prev = STATE.chatsBotFilter;
    chatsF.innerHTML = `<option value="">Все VDS</option>` + STATE.bots.map((b) => {
      const label = b.alias || b.host;
      const st = b.reachable ? "●" : "○";
      return `<option value="${escapeHtml(b.id)}"${b.id === prev ? " selected" : ""}>${st} ${escapeHtml(label)}</option>`;
    }).join("");
    chatsF.value = STATE.bots.some((b) => b.id === prev) ? prev : "";
    STATE.chatsBotFilter = chatsF.value;
  }

  updateChatsAccountFilterOptions();
  renderExtraBotSelects();
}

function renderExtraBotSelects() {
  for (const [id, stateKey] of [
    ["settingsBotSelect", "settingsBotId"],
    ["sourcesBotSelect", "sourcesBotId"],
  ]) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    const prev = STATE[stateKey];
    sel.innerHTML = `<option value="">— VDS —</option>` + STATE.bots.map((b) => {
      const label = b.alias || b.host;
      const st = b.reachable ? "●" : "○";
      return `<option value="${escapeHtml(b.id)}"${b.id === prev ? " selected" : ""}>${st} ${escapeHtml(label)}</option>`;
    }).join("");
    sel.value = STATE.bots.some((b) => b.id === prev) ? prev : "";
    STATE[stateKey] = sel.value;
  }
  syncSettingsSelects();
  syncSourcesSelects();
}

async function loadBots() {
  STATE.bots = await listBots();
  if (!STATE.selectedBotId && STATE.bots.length === 1) {
    STATE.selectedBotId = STATE.bots[0].id;
    localStorage.setItem("selectedBotId", STATE.selectedBotId);
  }
  renderBotSelects();
}

function bindBotSelect() {
  document.getElementById("dashboardBotSelect")?.addEventListener("change", (e) => {
    STATE.dashboardBotId = e.target.value;
    if (STATE.dashboardBotId) localStorage.setItem("dashboardBotId", STATE.dashboardBotId);
    else localStorage.removeItem("dashboardBotId");
    if (STATE.view === "dashboard") refresh(true);
  });
}

// =======================================================
//  Views (Dashboard / Servers)
// =======================================================
const VIEW_META = {
  dashboard: { title: "Дашборд", sub: "Сводка по аккаунтам и VDS" },
  accounts:  { title: "Аккаунты", sub: "Telegram-аккаунты на всех VDS" },
  chats:     { title: "Чаты", sub: "Все чаты по аккаунтам и серверам" },
  sources:   { title: "Источники", sub: "Broadcast-каналы для постов в рассылку" },
  settings:  { title: "Настройки", sub: "Стандартный текст, интервал, прокси, режим forward" },
  servers:   { title: "Серверы", sub: "Развёртывание и управление ботом на VDS" },
};

function switchView(name, opts = {}) {
  if (!VIEW_META[name]) return;
  if (name !== "sources" && STATE.view === "sources") STATE.sourcesChatContext = null;
  STATE.view = name;
  document.querySelectorAll("[data-view]").forEach((el) => {
    const active = el.dataset.view === name;
    el.hidden = !active;
    el.classList.toggle("view-active", active);
  });
  document.querySelectorAll("[data-view-only]").forEach((el) => {
    const allowed = (el.dataset.viewOnly || "").split(/\s+/).filter(Boolean);
    const show = allowed.includes(name);
    el.hidden = !show;
    el.classList.toggle("view-active", show);
  });
  document.getElementById("topbarTitle").textContent = VIEW_META[name].title;
  document.getElementById("topbarSub").textContent   = VIEW_META[name].sub;
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.classList.toggle("active", n.dataset.page === name);
  });
  if (opts.skipRefresh) return;
  if (name === "servers") refreshServers();
  else if (name === "accounts") refreshAccounts(true);
  else if (name === "chats") refreshChats(true);
  else if (name === "settings") refreshSettings();
  else if (name === "sources") refreshSources();
  else if (name === "dashboard") refresh(true);
}

// =======================================================
//  Servers — table
// =======================================================
function serverStatusBadge(s) {
  switch (s.status) {
    case "running":   return '<span class="badge badge-paid">RUNNING</span>';
    case "deploying": return '<span class="badge badge-pending">DEPLOYING</span>';
    case "stopped":   return '<span class="badge badge-pending">STOPPED</span>';
    case "error":     return '<span class="badge badge-failed">ERROR</span>';
    default:          return '<span class="badge badge-pending">NEW</span>';
  }
}

function serverRowActions(s) {
  const isNew = s.status === "new";
  return `
    <div class="row-actions">
      <button class="ra ra-start" data-srv="deploy" data-id="${escapeHtml(s.id)}" title="${isNew ? "Развернуть" : "Передеплоить"}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v12M8 7l4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
      </button>
      <button class="ra" data-srv="restart" data-id="${escapeHtml(s.id)}" title="Перезапустить" ${isNew ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
      </button>
      <button class="ra ra-stop" data-srv="stop" data-id="${escapeHtml(s.id)}" title="Остановить" ${isNew ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
      </button>
      <button class="ra" data-srv="log" data-id="${escapeHtml(s.id)}" title="Журнал">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>
      </button>
      <button class="ra ra-danger" data-srv="uninstall" data-id="${escapeHtml(s.id)}" title="Снести с сервера" ${isNew ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>
      </button>
      <button class="ra ra-danger" data-srv="remove" data-id="${escapeHtml(s.id)}" title="Удалить из реестра">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>
    </div>
  `;
}

function fmtRelTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function renderServersTable() {
  const body = document.getElementById("serversBody");
  const list = STATE.bots;
  document.getElementById("serversCount").textContent = String(list.length);
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty-row">Серверов пока нет — нажми «+ VDS» в шапке</td></tr>`;
    return;
  }
  body.innerHTML = list.map((s) => {
    const apiUrl = `http://${s.host}:${s.apiPort}/api/local/health`;
    const label = s.alias || s.host;
    const initials = (s.alias || s.host).slice(0, 2).toUpperCase();
    const reach = s.reachable
      ? '<span class="chip on">online</span>'
      : '<span class="chip off">offline</span>';
    return `
      <tr data-id="${escapeHtml(s.id)}">
        <td data-label="Сервер">
          <div class="course-cell">
            <div class="course-thumb account-thumb">${escapeHtml(initials)}</div>
            <div class="account-meta">
              <span class="account-name">${escapeHtml(label)}</span>
              <span class="account-sub mono">${escapeHtml(s.sshUser)}@${escapeHtml(s.host)}:${s.sshPort}</span>
            </div>
          </div>
        </td>
        <td data-label="Статус">${serverStatusBadge(s)} ${reach}${s.lastError ? `<div class="cell-mute mono" style="margin-top:4px; font-size: 11px;" title="${escapeHtml(s.lastError)}">${escapeHtml(s.lastError.slice(0, 60))}</div>` : ""}</td>
        <td data-label="Установлен">
          <span class="mono cell-dim">${escapeHtml(s.installDir)}</span>
          ${s.status !== "new" ? `<div style="margin-top:4px;"><a href="${apiUrl}" target="_blank" class="cell-dim" style="font-size: 11px; text-decoration: underline;">API :${s.apiPort}</a></div>` : ""}
        </td>
        <td data-label="Деплой">${fmtRelTime(s.lastDeployAt)}</td>
        <td data-label="Ключ">${s.hasSshKey ? '<span class="chip on">key</span>' : '<span class="cell-mute">—</span>'}</td>
        <td data-label="Действия" class="td-actions">${serverRowActions(s)}</td>
      </tr>
    `;
  }).join("");
}

async function refreshServers() {
  try {
    await loadBots();
    renderServersTable();
  } catch (e) { toastErr(e); }
}

// =======================================================
//  Add VDS modal
// =======================================================
async function openAddServerModal() {
  openModal({
    title: "Добавить VDS",
    body: `
      <form class="form-grid" id="addServerForm" autocomplete="off">
        <div class="callout">
          Бот будет установлен на VDS через SSH, запущен через <b>systemd</b>
          (<code>userbot.service</code>) и будет доступен по HTTP API на порту
          <code>8080</code>. Пароль SSH используется один раз — дальше работает deploy-ключ.
        </div>

        <div class="field-row">
          <label class="field">
            <span class="field-label">Алиас</span>
            <input class="input" name="alias" placeholder="например, vds-1" />
          </label>
          <label class="field">
            <span class="field-label">Host / IP <em>обязательно</em></span>
            <input class="input mono" name="host" required placeholder="1.2.3.4" />
          </label>
        </div>

        <div class="field-row">
          <label class="field">
            <span class="field-label">SSH порт</span>
            <input class="input" name="sshPort" type="number" value="22" min="1" max="65535" />
          </label>
          <label class="field">
            <span class="field-label">SSH user</span>
            <input class="input mono" name="sshUser" value="root" />
          </label>
        </div>

        <label class="field">
          <span class="field-label">SSH пароль <em>обязательно при первом деплое</em></span>
          <input class="input mono" name="password" type="password" autocomplete="new-password" />
        </label>

        <div class="field-row">
          <label class="field">
            <span class="field-label">Install dir</span>
            <input class="input mono" name="installDir" value="/opt/userbot" />
          </label>
          <label class="field">
            <span class="field-label">Порт API бота</span>
            <input class="input" name="apiPort" type="number" value="8080" min="1" max="65535" />
          </label>
        </div>

        <hr style="border:0; border-top: 1px solid var(--line); margin: 6px 0;" />

        <div class="field-row">
          <label class="field">
            <span class="field-label">API_ID <em>обязательно</em></span>
            <input class="input mono" name="apiId" required placeholder="12345678" />
          </label>
          <label class="field">
            <span class="field-label">API_HASH <em>обязательно</em></span>
            <input class="input mono" name="apiHash" required placeholder="abcdef…" />
          </label>
        </div>

        <label class="field">
          <span class="field-label">BOT_TOKEN (control-бот, опционально)</span>
          <input class="input mono" name="tgBotToken" placeholder="опционально" />
        </label>

        <label class="field">
          <span class="field-label">ADMIN_USER_IDS</span>
          <input class="input mono" name="adminUserIds" placeholder="через запятую" />
        </label>
      </form>
    `,
    footer: modalActions("Создать и развернуть"),
  });

  document.getElementById("modalSubmit").addEventListener("click", async () => {
    const f = document.getElementById("addServerForm");
    if (!f.reportValidity()) return;
    const fd = new FormData(f);
    const password = String(fd.get("password") || "").trim();
    if (!password) { toastErr(new Error("Пароль SSH обязателен при первом деплое")); return; }

    let server;
    try {
      server = await addBot({
        alias: String(fd.get("alias") || "").trim(),
        host: String(fd.get("host") || "").trim(),
        sshPort: Number(fd.get("sshPort") || 22),
        sshUser: String(fd.get("sshUser") || "root").trim(),
        installDir: String(fd.get("installDir") || "/opt/userbot").trim(),
        apiPort: Number(fd.get("apiPort") || 8080),
      });
    } catch (e) { toastErr(e); return; }

    try {
      await deployBot(server.id, {
        password,
        apiId: String(fd.get("apiId") || "").trim(),
        apiHash: String(fd.get("apiHash") || "").trim(),
        tgBotToken: String(fd.get("tgBotToken") || "").trim(),
        adminUserIds: String(fd.get("adminUserIds") || "").trim(),
      });
      toastOk(`${server.alias || server.host}: деплой запущен`);
      closeModal();
      STATE.selectedBotId = server.id;
      localStorage.setItem("selectedBotId", server.id);
      switchView("servers");
      await refreshServers();
      renderBotSelects();
      openDeployLogModal(server.id, `Деплой: ${server.alias || server.host}`);
    } catch (e) {
      toastErr(e);
    }
  });
}

// =======================================================
//  Deploy log modal (polls /log)
// =======================================================
function openDeployLogModal(sid, title) {
  STATE.serverLogTarget = sid;
  openModal({
    title: title || "Журнал",
    body: `<pre class="log-view" id="deployLog">…</pre>`,
    footer: `<span class="chip" id="logChip">RUNNING</span><button class="btn-ghost" data-modal-close>Закрыть</button>`,
  });
  document.getElementById("modalPanel").classList.add("modal-wide");

  const tick = async () => {
    if (STATE.serverLogTarget !== sid) return;
    try {
      const snap = await fetchDeployLog(sid);
      const el = document.getElementById("deployLog");
      const chip = document.getElementById("logChip");
      if (!el || !chip) return;
      const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
      el.textContent = (snap.log || []).join("\n");
      if (wasAtBottom) el.scrollTop = el.scrollHeight;
      chip.textContent = snap.status === "running" ? "RUNNING" : snap.status.toUpperCase();
      chip.classList.toggle("on",  snap.status === "success");
      chip.classList.toggle("off", snap.status === "error");
      if (snap.status === "success" || snap.status === "error") {
        clearInterval(STATE.serverLogPollTimer);
        STATE.serverLogPollTimer = null;
        refreshServers();
      }
    } catch (e) { /* keep polling */ }
  };
  tick();
  STATE.serverLogPollTimer = setInterval(tick, 1500);
}

// =======================================================
//  Servers — row actions
// =======================================================
function passwordPrompt(message) {
  return new Promise((resolve) => {
    openModal({
      title: "Нужен SSH-пароль",
      body: `
        <p class="modal-text">${escapeHtml(message || "Введи пароль SSH (или оставь пустым если deploy-ключ ещё рабочий)")}</p>
        <input class="input mono" id="pwdPromptInput" type="password" autocomplete="new-password" />
      `,
      footer: modalActions("OK"),
    });
    setTimeout(() => document.getElementById("pwdPromptInput")?.focus(), 50);
    const root = document.getElementById("modalRoot");
    const finish = (v) => {
      root.removeEventListener("click", onBackdrop);
      resolve(v);
    };
    const onBackdrop = (e) => {
      if (e.target.closest("[data-modal-close]")) finish(null);
    };
    root.addEventListener("click", onBackdrop);
    document.getElementById("modalSubmit").addEventListener("click", () => {
      finish(document.getElementById("pwdPromptInput").value);
      closeModal();
    }, { once: true });
  });
}

async function handleServerAction(act, sid) {
  const s = STATE.bots.find((x) => x.id === sid);
  if (!s) return;

  switch (act) {
    case "deploy": {
      const isQuickRedeploy = s.status !== "new" && s.hasSshKey;
      if (isQuickRedeploy) {
        if (!confirm(`Передеплоить «${s.alias || s.host}»?\nКод обновится на VDS, .env и sessions сохранятся.`)) return;
        try {
          await deployBot(sid, {});
          openDeployLogModal(sid, `Деплой: ${s.alias || s.host}`);
          refreshServers();
        } catch (e) { toastErr(e); }
        break;
      }
      const password = await passwordPrompt(`Пароль SSH для ${s.sshUser}@${s.host}. Если deploy-ключ уже установлен — оставь пустым.`);
      if (password === null) return;
      const creds = await new Promise((resolve) => {
        openModal({
          title: `Развернуть: ${s.alias || s.host}`,
          body: `
            <form class="form-grid" id="redeployForm">
              <div class="callout">При деплое перезаписываются файлы, venv и .env. Sessions и runtime_state на сервере сохраняются.</div>
              <div class="field-row">
                <label class="field"><span class="field-label">API_ID</span><input class="input mono" name="apiId" required /></label>
                <label class="field"><span class="field-label">API_HASH</span><input class="input mono" name="apiHash" required /></label>
              </div>
              <label class="field"><span class="field-label">BOT_TOKEN</span><input class="input mono" name="tgBotToken" /></label>
              <label class="field"><span class="field-label">ADMIN_USER_IDS</span><input class="input mono" name="adminUserIds" /></label>
            </form>
          `,
          footer: modalActions("Развернуть"),
        });
        const root = document.getElementById("modalRoot");
        const onClose = () => { root.removeEventListener("click", onBackdrop); resolve(null); };
        const onBackdrop = (e) => { if (e.target.closest("[data-modal-close]")) { closeModal(); onClose(); } };
        root.addEventListener("click", onBackdrop);
        document.getElementById("modalSubmit").addEventListener("click", () => {
          const f = document.getElementById("redeployForm");
          if (!f.reportValidity()) return;
          const fd = new FormData(f);
          closeModal();
          root.removeEventListener("click", onBackdrop);
          resolve({
            apiId: String(fd.get("apiId") || "").trim(),
            apiHash: String(fd.get("apiHash") || "").trim(),
            tgBotToken: String(fd.get("tgBotToken") || "").trim(),
            adminUserIds: String(fd.get("adminUserIds") || "").trim(),
          });
        }, { once: true });
      });
      if (!creds) return;
      try {
        await deployBot(sid, { password: password || undefined, ...creds });
        openDeployLogModal(sid, `Деплой: ${s.alias || s.host}`);
        refreshServers();
      } catch (e) { toastErr(e); }
      break;
    }
    case "restart": {
      try {
        await restartBot(sid, {});
        openDeployLogModal(sid, `Restart: ${s.alias || s.host}`);
      } catch (e) { toastErr(e); }
      break;
    }
    case "stop": {
      if (!confirm(`Остановить бота exsender на ${s.host}?`)) return;
      try {
        await stopBot(sid, {});
        openDeployLogModal(sid, `Stop: ${s.alias || s.host}`);
      } catch (e) { toastErr(e); }
      break;
    }
    case "log":
      openDeployLogModal(sid, `Журнал: ${s.alias || s.host}`);
      break;
    case "uninstall": {
      if (!confirm(`Полностью снести бота с ${s.host}?\nБудет удалён ${s.installDir} и systemd unit.`)) return;
      try {
        await uninstallBot(sid, {});
        openDeployLogModal(sid, `Uninstall: ${s.alias || s.host}`);
      } catch (e) { toastErr(e); }
      break;
    }
    case "remove": {
      if (!confirm(`Удалить «${s.alias || s.host}» из реестра?\nБот на сервере не останавливается.`)) return;
      try {
        await removeBot(sid);
        if (STATE.selectedBotId === sid) {
          STATE.selectedBotId = "";
          localStorage.removeItem("selectedBotId");
        }
        toastOk("Удалён из реестра");
        await refreshServers();
        renderBotSelects();
      } catch (e) { toastErr(e); }
      break;
    }
  }
}

function bindServersTable() {
  document.getElementById("serversBody").addEventListener("click", (e) => {
    const btn = e.target.closest("button.ra[data-srv]");
    if (!btn || btn.disabled) return;
    handleServerAction(btn.dataset.srv, btn.dataset.id);
  });
}

// =======================================================
//  Init
// =======================================================
async function init() {
  let meUser = "";
  try {
    const me = await siteApi("GET", "/api/auth/me");
    if (!me.user) {
      window.location.href = "/login";
      return;
    }
    meUser = String(me.user || "");
  } catch {
    window.location.href = "/login";
    return;
  }

  const avatarLabel = document.getElementById("userAvatarLabel");
  const avatarEl = document.getElementById("userAvatar");
  if (avatarLabel && meUser) {
    avatarLabel.textContent = meUser.slice(0, 2).toLowerCase();
    if (avatarEl) avatarEl.title = meUser;
  }

  bindNav();
  bindMobileNav();
  bindFilters();
  bindTable();
  bindChatsTable();
  bindServersTable();
  bindModal();
  bindBotSelect();
  bindSettingsAndSources();
  bindBulkActions();
  bindSessionManager();
  document.getElementById("addServerBtn")?.addEventListener("click", openAddServerModal);

  try {
    await loadBots();
  } catch (e) {
    toastErr(e);
  }

  switchView("dashboard", { skipRefresh: true });
  await refresh(true);
  STATE.pollTimer = setInterval(() => {
    if (STATE.view === "dashboard") refresh();
    else if (STATE.view === "accounts") refreshAccounts();
    else if (STATE.view === "chats") refreshChats();
    else if (STATE.view === "settings") refreshSettings();
    else if (STATE.view === "sources") refreshSources();
    else if (STATE.view === "servers") refreshServers();
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
