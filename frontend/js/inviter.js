(function () {
  const STATE = {
    view: "dashboard",
    isAdmin: false,
    bots: [],
    flatAccounts: [],
    workspaceAccounts: [],
    parsedItems: [],
    parsedCategoryFilter: "",
    parsedShowArchived: false,
    overview: null,
    slotAuthorized: null,
    accountsBotFilter: "",
    jobTimer: null,
    parseTimer: null,
    serverLogPoll: null,
    serverLogTarget: null,
  };

  const PAGE_META = {
    dashboard: { sub: "EX Inviter", title: "Дашборд" },
    parse: { sub: "EX Inviter", title: "Парс" },
    parsed: { sub: "EX Inviter", title: "Parsed-чаты" },
    invite: { sub: "EX Inviter", title: "Инвайт" },
    accounts: { sub: "EX Inviter", title: "Аккаунты" },
    servers: { sub: "EX Inviter", title: "Серверы" },
  };

  const RESTART_INTERVAL_OPTIONS = [
    { value: 0, label: "Выкл" },
    { value: 6, label: "6 ч" },
    { value: 12, label: "12 ч" },
    { value: 24, label: "24 ч" },
    { value: 48, label: "48 ч" },
  ];

  const TRAFFIC_CATEGORIES = [
    { value: "warm", label: "Тёплый" },
    { value: "cold", label: "Холодный" },
    { value: "crypto", label: "Крипта" },
    { value: "business", label: "Бизнес" },
    { value: "adult", label: "18+" },
    { value: "mixed", label: "Смешанный" },
    { value: "other", label: "Другое" },
  ];

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtRelTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  }

  function fmtRestartInterval(hours) {
    const h = Number(hours) || 0;
    if (h <= 0) return "Выкл";
    const opt = RESTART_INTERVAL_OPTIONS.find((o) => o.value === h);
    return opt ? opt.label : h + " ч";
  }

  function fmtDateTime(value) {
    if (!value) return "—";
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  }

  function categoryLabel(value) {
    const found = TRAFFIC_CATEGORIES.find(function (cat) { return cat.value === value; });
    return (found || TRAFFIC_CATEGORIES[TRAFFIC_CATEGORIES.length - 1]).label;
  }

  function categoryOptions(selected) {
    return TRAFFIC_CATEGORIES.map(function (cat) {
      return '<option value="' + escapeHtml(cat.value) + '"' + (cat.value === selected ? " selected" : "") + ">" +
        escapeHtml(cat.label) + "</option>";
    }).join("");
  }

  function botLabel(id) {
    const b = STATE.bots.find((x) => x.id === id);
    return b ? (b.alias || b.host) : id;
  }

  function botSuite(b) {
    return (b && b.suite) || "sender";
  }

  function isBorrowedBot(b) {
    return botSuite(b) !== "inviter";
  }

  function inviterOwnedBots() {
    return STATE.bots.filter(function (b) { return !isBorrowedBot(b); });
  }

  function borrowedBots() {
    return STATE.bots.filter(function (b) { return isBorrowedBot(b); });
  }

  function botOptionLabel(b) {
    const tag = isBorrowedBot(b) ? "exsender · " : "inviter · ";
    return tag + (b.alias || b.host) + (b.reachable ? "" : " (offline)");
  }

  function suiteBadgeHtml(b) {
    const suite = botSuite(b);
    const label = suite === "inviter" ? "inviter" : "exsender";
    return '<span class="suite-badge ' + suite + '">' + label + "</span>";
  }

  function viewsFromAttr(val) {
    return String(val || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function setChip(el, on, label) {
    if (!el) return;
    el.textContent = label;
    el.classList.toggle("on", on);
    el.classList.remove("off");
    el.classList.toggle("neutral", !on);
  }

  function showAlert(msg, kind) {
    const errEl = $("invAlert");
    const okEl = $("invOk");
    if (kind === "ok") {
      if (okEl) { okEl.textContent = msg; okEl.hidden = !msg; }
      if (errEl) errEl.hidden = true;
      if (msg) setTimeout(function () { if (okEl) okEl.hidden = true; }, 4000);
      return;
    }
    if (errEl) { errEl.textContent = msg; errEl.hidden = !msg; }
    if (okEl) okEl.hidden = true;
  }

  function apiError(data, status) {
    const d = data.detail;
    if (typeof d === "string") return d;
    if (data.error) return String(data.error);
    return "HTTP " + status;
  }

  async function api(method, path, body, retryCsrf) {
    const opts = { method: method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await secureFetch(path, opts);
    const data = await r.json().catch(function () { return {}; });
    if (r.status === 403 && !retryCsrf && data.detail === "csrf validation failed" && typeof refreshCsrf === "function") {
      await refreshCsrf();
      return api(method, path, body, true);
    }
    if (r.status === 401) {
      window.location.href = "/login?next=/inviter";
      throw new Error("Сессия истекла");
    }
    if (r.status === 403 && data.detail === "subscription required") {
      window.location.href = "/profile";
      throw new Error("Нужна активная подписка");
    }
    if (r.status === 403 && data.detail === "admin only") {
      window.location.href = "/login?next=/inviter";
      throw new Error("Нужны права админа");
    }
    if (!r.ok) throw new Error(apiError(data, r.status));
    return data;
  }

  function selectedBot() { return $("invBotSelect")?.value || ""; }
  function selectedAccount() { return $("invAccountSelect")?.value || ""; }

  function botApi(method, sub, body, params) {
    const bid = selectedBot();
    if (!bid) throw new Error("Выбери VDS");
    let url = "/api/inviter/bots/" + encodeURIComponent(bid) + "/" + sub.replace(/^\/+/, "");
    if (params) url += "?" + new URLSearchParams(params).toString();
    return api(method, url, body).catch(function (err) {
      const msg = String(err?.message || "");
      if (msg === "Not Found" || msg.toLowerCase() === "not found" || /^HTTP 404$/i.test(msg)) {
        throw new Error("На выбранной VDS не найден Inviter API. Обнови бота во вкладке «Серверы» (Deploy/Restart).");
      }
      throw err;
    });
  }

  function panelApi(method, bid, sub, body) {
    if (!bid) throw new Error("Выбери VDS");
    const url = "/api/inviter/bots/" + encodeURIComponent(bid) + "/proxy/" + sub.replace(/^\/+/, "");
    return api(method, url, body);
  }

  function updateTopbarBack(view) {
    const back = $("backBtn");
    if (!back) return;
    back.hidden = view === "dashboard";
  }

  function bindSidebar() {
    function setSidebarOpen(open) {
      document.body.classList.toggle("sidebar-open", open);
      const btn = $("menuBtn");
      const backdrop = $("sidebarBackdrop");
      if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (backdrop) {
        backdrop.hidden = !open;
        backdrop.setAttribute("aria-hidden", open ? "false" : "true");
      }
    }
    $("backBtn")?.addEventListener("click", function () {
      setSidebarOpen(false);
      switchView("dashboard");
    });
    $("menuBtn")?.addEventListener("click", function () {
      setSidebarOpen(!document.body.classList.contains("sidebar-open"));
    });
    $("sidebarBackdrop")?.addEventListener("click", function () { setSidebarOpen(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) setSidebarOpen(false);
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 768) setSidebarOpen(false);
    });
  }

  function fixCrossLinks() {
    const origin = location.hostname === "inviter.exsender.top" ? "https://exsender.top" : "";
    const sender = $("navSender");
    if (sender && origin) sender.href = origin + "/app";
  }

  function switchView(name) {
    STATE.view = name;
    document.querySelectorAll("#invNav .nav-item[data-page]").forEach(function (el) {
      el.classList.toggle("active", el.dataset.page === name);
    });
    document.querySelectorAll("[data-view]").forEach(function (el) {
      const views = viewsFromAttr(el.getAttribute("data-view"));
      el.hidden = views.length > 0 && views.indexOf(name) < 0;
    });
    document.querySelectorAll("[data-view-only]").forEach(function (el) {
      const views = viewsFromAttr(el.getAttribute("data-view-only"));
      el.hidden = views.length === 0 || views.indexOf(name) < 0;
    });
    const meta = PAGE_META[name] || PAGE_META.dashboard;
    setText("invTopSub", meta.sub);
    setText("invTopTitle", meta.title);
    updateTopbarBack(name);
    refreshCurrentView();
  }

  async function refreshWorkspaceData() {
    await loadBots();
    await loadWorkspaceAccounts();
    try { await refreshOverview(); } catch (e) { console.warn("overview", e); }
    try { await refreshParse(); } catch (e) { console.warn("parse", e); }
    try { await refreshJob(); } catch (e) { console.warn("job", e); }
  }

  async function refreshCurrentView() {
    try {
      if (STATE.view === "dashboard" || STATE.view === "parse" || STATE.view === "invite") {
        await refreshWorkspaceData();
      } else if (STATE.view === "servers") {
        await loadBots();
        renderServersTable();
        renderBorrowedServers();
      } else if (STATE.view === "accounts") {
        await loadBots();
        await loadFlatAccounts();
        renderAccountsTable();
      } else if (STATE.view === "parsed") {
        await loadWorkspaceAccounts();
        await refreshOverview();
        await loadParsedSources();
      }
    } catch (e) {
      showAlert(e.message || String(e));
    }
  }

  function findSelectedAccount() {
    const bid = selectedBot();
    const aid = selectedAccount();
    if (!bid || !aid) return null;
    return STATE.workspaceAccounts.find(function (a) { return a.id === aid; })
      || STATE.flatAccounts.find(function (a) { return a.botId === bid && a.id === aid; })
      || null;
  }

  function isAccountAuthorized(acc) {
    if (STATE.slotAuthorized !== null && selectedAccount()) return !!STATE.slotAuthorized;
    return !!(acc && acc.authorized);
  }

  function updateSlotStatus() {
    const bid = selectedBot();
    const aid = selectedAccount();
    const dot = $("invStatusDot");
    const slot = $("invStatusSlot");
    const meta = $("invStatusMeta");
    const hint = $("invSlotHint");
    const acc = findSelectedAccount();
    const authorized = isAccountAuthorized(acc);

    const bot = STATE.bots.find(function (b) { return b.id === bid; });
    if (!bid) {
      if (dot) dot.className = "dot-led neutral";
      if (slot) slot.textContent = "—";
      if (meta) meta.textContent = "Выбери VDS";
      if (hint) hint.textContent = "Можно взять VDS из exsender или добавить свой inviter";
      return;
    }
    if (!aid) {
      if (dot) dot.className = "dot-led neutral";
      if (slot) slot.textContent = botLabel(bid);
      if (meta) meta.textContent = (isBorrowedBot(bot) ? "exsender · " : "inviter · ") + "выбери слот";
      if (hint) hint.textContent = isBorrowedBot(bot)
        ? "Аккаунты с этого VDS из основной панели exsender"
        : "Добавь аккаунт во вкладке «Аккаунты»";
      return;
    }
    if (dot) dot.className = "dot-led " + (authorized ? "on" : "off");
    if (slot) slot.textContent = aid;
    if (meta) meta.textContent = botLabel(bid) + (authorized ? " · авторизован" : " · не авторизован");
    if (hint) hint.textContent = authorized ? "Готов к парсу и инвайту" : "Войди в Telegram для слота";
  }

  function clearOverviewStats() {
    STATE.slotAuthorized = null;
    setText("invStatQueue", "—");
    setText("invStatParsed", "—");
    setText("invStatTarget", "—");
    setText("invStatTargetSub", "не выбран");
    updateSlotStatus();
  }

  async function loadBots() {
    const data = await api("GET", "/api/inviter/bots");
    STATE.bots = data.bots || [];
    syncBotSelects();
    return STATE.bots;
  }

  function syncBotSelects() {
    const selects = [
      $("invBotSelect"),
      $("invAccountsBotFilter"),
    ].filter(Boolean);
    selects.forEach(function (sel) {
      const isFilter = sel.id === "invAccountsBotFilter";
      const cur = sel.value;
      sel.innerHTML = isFilter
        ? '<option value="">Все VDS</option>'
        : '<option value="">— выбери VDS —</option>';
      STATE.bots.forEach(function (b) {
        const opt = document.createElement("option");
        opt.value = b.id;
        opt.textContent = botOptionLabel(b);
        sel.appendChild(opt);
      });
      if (cur) sel.value = cur;
    });
    updateSlotStatus();
  }

  async function loadWorkspaceAccounts() {
    const bid = selectedBot();
    const accSel = $("invAccountSelect");
    if (!accSel) return;
    const prev = accSel.value;
    accSel.innerHTML = '<option value="">— слот —</option>';
    STATE.workspaceAccounts = [];
    STATE.slotAuthorized = null;
    if (!bid) {
      updateSlotStatus();
      return;
    }
    const ov = await api("GET", "/api/inviter/bots/" + encodeURIComponent(bid) + "/accounts");
    STATE.workspaceAccounts = (ov.accounts || []).map(function (a) {
      return { id: a.id, authorized: !!a.authorized, hasProxy: !!a.hasProxy };
    });
    STATE.workspaceAccounts.forEach(function (a) {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = a.id + (a.authorized ? "" : " (не авторизован)");
      accSel.appendChild(opt);
    });
    if (prev) accSel.value = prev;
    updateSlotStatus();
  }

  async function loadFlatAccounts() {
    const rows = [];
    for (const b of STATE.bots) {
      if (!b.hasApiToken && b.status === "new") continue;
      try {
        const ov = await api("GET", "/api/inviter/bots/" + encodeURIComponent(b.id) + "/accounts");
        (ov.accounts || []).forEach(function (a) {
          rows.push({
            botId: b.id,
            botLabel: b.alias || b.host,
            botSuite: botSuite(b),
            borrowed: isBorrowedBot(b),
            id: a.id,
            authorized: !!a.authorized,
            hasProxy: !!a.hasProxy,
            proxy: a.proxy || "",
          });
        });
      } catch (e) {
        console.warn("accounts", b.id, e);
      }
    }
    STATE.flatAccounts = rows;
    return rows;
  }

  function accountStatusBadge(a) {
    return a.authorized
      ? '<span class="chip on">готов</span>'
      : '<span class="chip off">нужен вход</span>';
  }

  function renderAccountsTable() {
    const body = $("invAccountsBody");
    if (!body) return;
    let rows = STATE.flatAccounts;
    if (STATE.accountsBotFilter) {
      rows = rows.filter(function (a) { return a.botId === STATE.accountsBotFilter; });
    }
    setText("invAccountsCount", String(rows.length));
    if (!STATE.bots.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-row">Нет VDS — добавь inviter или используй exsender</td></tr>';
      return;
    }
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-row">Нет аккаунтов на доступных VDS</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (a) {
      const suiteBadge = a.borrowed
        ? '<span class="suite-badge sender">exsender</span>'
        : '<span class="suite-badge inviter">inviter</span>';
      const delBtn = a.borrowed
        ? ""
        : '<button class="ra ra-danger" data-acc="delete" title="Удалить">✕</button>';
      return `
        <tr data-bot="${escapeHtml(a.botId)}" data-id="${escapeHtml(a.id)}">
          <td data-label="VDS"><span class="cell-strong">${escapeHtml(a.botLabel)}</span></td>
          <td data-label="Источник">${suiteBadge}</td>
          <td data-label="Слот"><span class="mono">${escapeHtml(a.id)}</span></td>
          <td data-label="Статус">${accountStatusBadge(a)}</td>
          <td data-label="Прокси">${a.hasProxy ? '<span class="mono cell-dim">да</span>' : '<span class="cell-mute">нет</span>'}</td>
          <td data-label="Действия" class="td-actions">
            <div class="row-actions">
              <button class="ra" data-acc="login" title="Войти">⎆</button>
              <button class="ra" data-acc="use" title="Выбрать">✓</button>
              ${delBtn}
            </div>
          </td>
        </tr>`;
    }).join("");
  }

  async function loadParsedSources() {
    const aid = selectedAccount();
    const body = $("invParsedBody");
    if (!aid || !selectedBot()) {
      STATE.parsedItems = [];
      if (body) body.innerHTML = '<tr><td colspan="6" class="empty-row">Выбери VDS и слот аккаунта</td></tr>';
      setText("invParsedCount", "0");
      renderSafetyPanel();
      return;
    }
    const data = await botApi("GET", "parsed", undefined, {
      accountId: aid,
      includeArchived: STATE.parsedShowArchived ? "1" : "0",
    });
    STATE.parsedItems = data.items || [];
    renderParsedTable();
    renderSafetyPanel();
  }

  function filteredParsedItems() {
    return STATE.parsedItems.filter(function (item) {
      return !STATE.parsedCategoryFilter ||
        (item.traffic_category || "other") === STATE.parsedCategoryFilter;
    });
  }

  function parsedRow(item) {
    const sourceId = String(item.source_chat_id || "");
    const archived = !!item.archived_at;
    const category = item.traffic_category || "other";
    const queueCount = Number(item.queue_count || 0);
    return `
      <tr data-source="${escapeHtml(sourceId)}" class="${archived ? "row-archived" : ""}">
        <td data-label="Источник">
          <div class="account-meta">
            <span class="account-name">${escapeHtml(item.source_chat_title || sourceId)}</span>
            <span class="account-sub mono">${escapeHtml(sourceId)}</span>
            ${archived ? '<span class="chip neutral">архив</span>' : ""}
          </div>
        </td>
        <td data-label="Категория">
          <select class="input inv-category-select" data-field="category" aria-label="Категория трафика">
            ${categoryOptions(category)}
          </select>
        </td>
        <td data-label="Заметка">
          <textarea class="input inv-note-input" data-field="note" rows="2" maxlength="500" placeholder="Например: крипта, RU, тёплая база">${escapeHtml(item.note || "")}</textarea>
        </td>
        <td data-label="В очереди"><span class="chip ${queueCount > 0 ? "on" : "neutral"}">${queueCount}</span></td>
        <td data-label="Парс"><span class="cell-dim">${escapeHtml(fmtDateTime(item.parsed_at))}</span></td>
        <td data-label="Действия" class="td-actions">
          <div class="row-actions inv-parsed-row-actions">
            <button type="button" class="btn-primary" data-parsed="save">Сохранить</button>
            <button type="button" class="btn-ghost" data-parsed="clear">Очистить очередь</button>
            <button type="button" class="btn-ghost" data-parsed="archive">${archived ? "Вернуть" : "Архив"}</button>
          </div>
        </td>
      </tr>`;
  }

  function renderParsedTable() {
    const body = $("invParsedBody");
    if (!body) return;
    const rows = filteredParsedItems();
    setText("invParsedCount", String(rows.length));
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-row">Отпаршенных источников пока нет</td></tr>';
      return;
    }
    body.innerHTML = rows.map(parsedRow).join("");
  }

  function renderSafetyPanel() {
    const panel = $("invSafetyPanel");
    if (!panel) return;
    const warnings = [];
    const aid = selectedAccount();
    const ov = STATE.overview || {};
    const queueCount = Number(ov.queueCount || 0);
    const target = ov.target || {};
    const delay = parseFloat($("invDelay")?.value || "3") || 0;
    const limit = parseInt($("invLimit")?.value || "0", 10) || 0;

    if (!selectedBot() || !aid) {
      warnings.push("Выбери VDS и слот аккаунта перед запуском.");
    } else if (STATE.slotAuthorized === false) {
      warnings.push("Слот не авторизован в Telegram — сначала войди в аккаунт.");
    }
    if (!target.ref && !target.title) {
      warnings.push("Целевой чат не выбран — инвайт не стартует без target.");
    }
    if (delay > 0 && delay < 3) {
      warnings.push("Задержка меньше 3 сек повышает риск Flood/лимитов Telegram.");
    }
    if (queueCount > 300 && limit === 0) {
      warnings.push("В очереди " + queueCount + " пользователей, а лимит = 0. Для безопасного запуска лучше поставить батч-лимит.");
    }
    const uncategorizedHeavy = STATE.parsedItems.filter(function (item) {
      return Number(item.queue_count || 0) >= 100 &&
        (item.traffic_category || "other") === "other" &&
        !(item.note || "").trim();
    });
    if (uncategorizedHeavy.length) {
      warnings.push("Есть крупные источники без категории/заметки: " + uncategorizedHeavy.length + ". Лучше пометить качество трафика во вкладке Parsed.");
    }

    if (!warnings.length) {
      panel.hidden = true;
      panel.innerHTML = "";
      return;
    }
    panel.hidden = false;
    panel.innerHTML = '<b>Рекомендации по безопасности</b><ul>' +
      warnings.map(function (w) { return "<li>" + escapeHtml(w) + "</li>"; }).join("") +
      "</ul>";
  }

  function serverStatusBadge(s) {
    switch (s.status) {
      case "running": return '<span class="badge badge-paid">RUN</span>';
      case "deploying": return '<span class="badge badge-pending">DEPLOY</span>';
      case "stopped": return '<span class="badge badge-failed">STOP</span>';
      case "error": return '<span class="badge badge-failed">ERR</span>';
      default: return '<span class="badge badge-pending">NEW</span>';
    }
  }

  function restartIntervalSelectHtml(s) {
    const cur = Number(s.restartIntervalHours);
    const selected = Number.isFinite(cur) ? cur : 12;
    const opts = RESTART_INTERVAL_OPTIONS.map(function (o) {
      return '<option value="' + o.value + '"' + (o.value === selected ? " selected" : "") + ">" + o.label + "</option>";
    }).join("");
    const disabled = s.status === "new" ? " disabled" : "";
    return '<select class="input restart-interval-select" data-srv="restartInterval" data-id="' + escapeHtml(s.id) + '" style="width:auto;min-width:72px;padding:4px 8px;font-size:12px;"' + disabled + ">" + opts + "</select>";
  }

  function serverRowActions(s) {
    const isNew = s.status === "new";
    const dis = isNew ? " disabled" : "";
    return `
      <div class="row-actions">
        <button class="ra ra-start" data-srv="deploy" data-id="${escapeHtml(s.id)}" title="Деплой">↑</button>
        <button class="ra" data-srv="restart" data-id="${escapeHtml(s.id)}" title="Рестарт"${dis}>↻</button>
        <button class="ra ra-stop" data-srv="stop" data-id="${escapeHtml(s.id)}" title="Стоп"${dis}>■</button>
        <button class="ra" data-srv="log" data-id="${escapeHtml(s.id)}" title="Лог">≡</button>
        <button class="ra ra-danger" data-srv="remove" data-id="${escapeHtml(s.id)}" title="Удалить">✕</button>
      </div>`;
  }

  function renderBorrowedServers() {
    const body = $("invBorrowedBody");
    if (!body) return;
    const rows = borrowedBots();
    setText("invBorrowedCount", String(rows.length));
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-row">Нет VDS exsender в реестре</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (s) {
      const label = s.alias || s.host;
      const reach = s.reachable ? '<span class="chip on">онлайн</span>' : '<span class="chip off">офлайн</span>';
      return `
        <tr data-id="${escapeHtml(s.id)}">
          <td data-label="Сервер">
            <div class="account-meta">
              <span class="account-name">${escapeHtml(label)}</span>
              <span class="account-sub mono">${escapeHtml(s.id)}</span>
            </div>
          </td>
          <td data-label="Статус">${serverStatusBadge(s)} ${reach}</td>
          <td data-label="Host"><span class="mono cell-dim">${escapeHtml(s.host)}</span></td>
          <td data-label="Действия" class="td-actions">
            <div class="row-actions">
              <button class="ra" data-borrow="use" data-id="${escapeHtml(s.id)}" title="Использовать">✓</button>
            </div>
          </td>
        </tr>`;
    }).join("");
  }

  function renderServersTable() {
    const body = $("invServersBody");
    if (!body) return;
    const rows = inviterOwnedBots();
    setText("invServersCount", String(rows.length));
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-row">Нет inviter VDS — нажми «+ VDS»</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (s) {
      const label = s.alias || s.host;
      const reach = s.reachable ? '<span class="chip on">онлайн</span>' : '<span class="chip off">офлайн</span>';
      return `
        <tr data-id="${escapeHtml(s.id)}">
          <td data-label="Сервер">
            <div class="account-meta">
              <span class="account-name">${escapeHtml(label)}</span>
              <span class="account-sub mono">${escapeHtml(s.sshUser)}@${escapeHtml(s.host)}:${s.sshPort}</span>
            </div>
          </td>
          <td data-label="Статус">${serverStatusBadge(s)} ${reach}</td>
          <td data-label="Установлен"><span class="mono cell-dim">${escapeHtml(s.installDir)}</span></td>
          <td data-label="Деплой">${fmtRelTime(s.lastDeployAt)}</td>
          <td data-label="Авто-перезапуск">${restartIntervalSelectHtml(s)}</td>
          <td data-label="Действия" class="td-actions">${serverRowActions(s)}</td>
        </tr>`;
    }).join("");
  }

  function openModal(title, bodyHtml, footHtml) {
    setText("invModalTitle", title);
    $("invModalBody").innerHTML = bodyHtml;
    $("invModalFoot").innerHTML = footHtml || "";
    const root = $("invModalRoot");
    STATE.modalReturnFocus = document.activeElement;
    root.hidden = false;
    document.body.style.overflow = "hidden";
    requestAnimationFrame(function () {
      root.classList.add("in");
      const panel = $("invModalPanel");
      const focusTarget = panel && (panel.querySelector(
        "input, select, textarea, button:not([data-inv-modal-close])"
      ) || panel.querySelector("[data-inv-modal-close]"));
      if (focusTarget) focusTarget.focus();
    });
  }

  function closeModal() {
    const root = $("invModalRoot");
    root.classList.remove("in");
    document.body.style.overflow = "";
    if (STATE.serverLogPoll) {
      clearInterval(STATE.serverLogPoll);
      STATE.serverLogPoll = null;
    }
    STATE.serverLogTarget = null;
    setTimeout(function () { root.hidden = true; }, 180);
    if (STATE.modalReturnFocus && typeof STATE.modalReturnFocus.focus === "function") {
      STATE.modalReturnFocus.focus();
    }
    STATE.modalReturnFocus = null;
  }

  function modalFoot(submitLabel, danger) {
    const cls = danger ? "btn-danger" : "btn-primary";
    return '<button type="button" class="btn-ghost" data-inv-modal-close>Отмена</button>' +
      '<button type="button" class="' + cls + '" id="invModalSubmit">' + escapeHtml(submitLabel) + "</button>";
  }

  function bindModal() {
    document.body.addEventListener("click", function (e) {
      if (e.target.closest("[data-inv-modal-close]")) closeModal();
    });
    document.addEventListener("keydown", function (e) {
      const root = $("invModalRoot");
      if (!root || root.hidden) return;
      if (e.key === "Escape") { closeModal(); return; }
      if (e.key === "Tab") {
        const panel = $("invModalPanel");
        const focusable = panel.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });
  }

  function openAddServerModal() {
    openModal("Добавить inviter VDS", `
      <form class="form-grid" id="invAddServerForm" autocomplete="off">
        <div class="callout">VDS будет доступен только в inviter, не в основной панели exsender.</div>
        <div class="field-row">
          <label class="field"><span class="field-label">Алиас</span><input class="input" name="alias" placeholder="inviter-1" /></label>
          <label class="field"><span class="field-label">Host / IP</span><input class="input mono" name="host" required placeholder="1.2.3.4" /></label>
        </div>
        <div class="field-row">
          <label class="field"><span class="field-label">SSH порт</span><input class="input" name="sshPort" type="number" value="22" /></label>
          <label class="field"><span class="field-label">SSH user</span><input class="input mono" name="sshUser" value="root" /></label>
        </div>
        <label class="field"><span class="field-label">SSH пароль</span><input class="input mono" name="password" type="password" autocomplete="new-password" /></label>
        <div class="field-row">
          <label class="field"><span class="field-label">Install dir</span><input class="input mono" name="installDir" value="/opt/userbot" /></label>
          <label class="field"><span class="field-label">API порт</span><input class="input" name="apiPort" type="number" value="8765" /></label>
        </div>
      </form>`, modalFoot("Создать и развернуть"));

    $("invModalSubmit").onclick = async function () {
      const f = $("invAddServerForm");
      if (!f.reportValidity()) return;
      const fd = new FormData(f);
      const password = String(fd.get("password") || "").trim();
      if (!password) { showAlert("Пароль SSH обязателен при первом деплое"); return; }
      try {
        const server = await api("POST", "/api/inviter/bots", {
          alias: String(fd.get("alias") || "").trim(),
          host: String(fd.get("host") || "").trim(),
          sshPort: Number(fd.get("sshPort") || 22),
          sshUser: String(fd.get("sshUser") || "root").trim(),
          installDir: String(fd.get("installDir") || "/opt/userbot").trim(),
          apiPort: Number(fd.get("apiPort") || 8765),
        });
        await api("POST", "/api/inviter/bots/" + encodeURIComponent(server.id) + "/deploy", { password: password });
        showAlert("Деплой запущен: " + (server.alias || server.host), "ok");
        closeModal();
        await loadBots();
        renderServersTable();
      } catch (e) { showAlert(e.message); }
    };
  }

  function openAddAccountModal() {
    const owned = inviterOwnedBots();
    if (!owned.length) {
      showAlert("Сначала добавь inviter VDS во вкладке «Серверы» (exsender-аккаунты берутся из основной панели)");
      return;
    }
    const opts = owned.map(function (b) {
      return '<option value="' + escapeHtml(b.id) + '">' + escapeHtml(b.alias || b.host) + "</option>";
    }).join("");
    openModal("Добавить аккаунт inviter", `
      <form class="form-grid" id="invAddAccountForm" autocomplete="off">
        <label class="field"><span class="field-label">VDS</span><select class="input" name="botId">${opts}</select></label>
        <label class="field"><span class="field-label">ID слота</span>
          <input class="input mono" name="id" required pattern="[a-zA-Z][a-zA-Z0-9_-]{0,31}" placeholder="inv1" />
          <span class="field-hint">латиница, 1–32 символа</span>
        </label>
        <label class="field"><span class="field-label">Прокси</span>
          <input class="input mono" name="proxy" placeholder="login:pass@host:port (опционально)" />
        </label>
      </form>`, modalFoot("Создать"));

    $("invModalSubmit").onclick = async function () {
      const f = $("invAddAccountForm");
      if (!f.reportValidity()) return;
      const fd = new FormData(f);
      const bid = String(fd.get("botId") || "");
      try {
        await panelApi("POST", bid, "accounts", {
          id: String(fd.get("id") || "").trim(),
          proxy: String(fd.get("proxy") || "").trim(),
        });
        showAlert("Слот создан", "ok");
        closeModal();
        await loadFlatAccounts();
        renderAccountsTable();
        if (STATE.view === "dashboard" || STATE.view === "parse" || STATE.view === "invite") await loadWorkspaceAccounts();
      } catch (e) { showAlert(e.message); }
    };
  }

  function openAuthModal(botId, accountId) {
    openModal("Вход: " + accountId, `
      <form class="form-grid" id="invAuthForm" autocomplete="off">
        <div class="callout">${escapeHtml(botLabel(botId))} · <b>${escapeHtml(accountId)}</b></div>
        <label class="field"><span class="field-label">Телефон</span><input class="input mono" name="phone" placeholder="+79001234567" /></label>
        <label class="field"><span class="field-label">Код</span><input class="input mono" name="code" placeholder="из Telegram" /></label>
        <label class="field"><span class="field-label">2FA пароль</span><input class="input" name="password" type="password" autocomplete="off" /></label>
      </form>`, modalFoot("Отправить код"));

    const submit = $("invModalSubmit");
    let step = "send";
    submit.textContent = "Отправить код";
    submit.onclick = async function () {
      const fd = new FormData($("invAuthForm"));
      try {
        if (step === "send") {
          await panelApi("POST", botId, "accounts/" + encodeURIComponent(accountId) + "/auth/send_code", {
            phone: String(fd.get("phone") || "").trim(),
          });
          step = "signin";
          submit.textContent = "Войти";
          showAlert("Код отправлен", "ok");
          return;
        }
        await panelApi("POST", botId, "accounts/" + encodeURIComponent(accountId) + "/auth/sign_in", {
          code: String(fd.get("code") || "").trim(),
          password: String(fd.get("password") || "").trim() || undefined,
        });
        showAlert("Аккаунт авторизован", "ok");
        closeModal();
        await loadFlatAccounts();
        renderAccountsTable();
        if (STATE.view === "dashboard" || STATE.view === "parse" || STATE.view === "invite") {
          $("invBotSelect").value = botId;
          await loadWorkspaceAccounts();
          $("invAccountSelect").value = accountId;
          updateSlotStatus();
        }
      } catch (e) { showAlert(e.message); }
    };
  }

  async function openDeployLogModal(botId, title) {
    STATE.serverLogTarget = botId;
    openModal(title || "Журнал деплоя", '<pre class="inv-job-detail" id="invDeployLog" style="min-height:200px;">…</pre>', modalFoot("Закрыть", false));
    $("invModalSubmit").onclick = closeModal;
    async function poll() {
      if (!STATE.serverLogTarget) return;
      try {
        const data = await api("GET", "/api/inviter/bots/" + encodeURIComponent(botId) + "/deploy/log");
        const lines = (data.lines || []).map(function (l) { return l.t + " " + l.m; }).join("\n");
        const el = $("invDeployLog");
        if (el) el.textContent = lines || "(пусто)";
      } catch (_) { /* ignore */ }
    }
    await poll();
    STATE.serverLogPoll = setInterval(poll, 2000);
  }

  async function handleServerAction(act, botId) {
    const s = STATE.bots.find(function (b) { return b.id === botId; });
    if (!s) return;
    if (act === "deploy") {
      const pwd = s.hasSshKey ? "" : prompt("SSH пароль (если нужен):") || "";
      try {
        await api("POST", "/api/inviter/bots/" + encodeURIComponent(botId) + "/deploy", { password: pwd || undefined });
        showAlert("Деплой запущен", "ok");
        openDeployLogModal(botId, s.alias || s.host);
        await loadBots();
        renderServersTable();
      } catch (e) { showAlert(e.message); }
      return;
    }
    if (act === "restart") {
      try {
        await api("POST", "/api/inviter/bots/" + encodeURIComponent(botId) + "/restart", {});
        showAlert("Рестарт", "ok");
      } catch (e) { showAlert(e.message); }
      return;
    }
    if (act === "stop") {
      try {
        await api("POST", "/api/inviter/bots/" + encodeURIComponent(botId) + "/stop", {});
        showAlert("Стоп", "ok");
      } catch (e) { showAlert(e.message); }
      return;
    }
    if (act === "log") {
      openDeployLogModal(botId, s.alias || s.host);
      return;
    }
    if (act === "remove") {
      if (!confirm("Удалить " + (s.alias || s.host) + " из реестра inviter?")) return;
      try {
        await api("DELETE", "/api/inviter/bots/" + encodeURIComponent(botId));
        showAlert("Удалено", "ok");
        await loadBots();
        renderServersTable();
      } catch (e) { showAlert(e.message); }
    }
  }

  async function refreshOverview() {
    const aid = selectedAccount();
    if (!aid || !selectedBot()) {
      STATE.overview = null;
      clearOverviewStats();
      renderParse(IDLE_PARSE);
      renderJob(IDLE_JOB);
      const targetRef = $("invTargetRef");
      if (targetRef) targetRef.value = "";
      renderSafetyPanel();
      return;
    }
    const data = await botApi("GET", "overview", undefined, { accountId: aid });
    STATE.overview = data;
    STATE.slotAuthorized = !!data.authorized;
    const target = data.target || {};
    setText("invStatQueue", String(data.queueCount || 0));
    setText("invStatParsed", String(data.parsedChatsCount || 0));
    setText("invStatTarget", target.title || target.ref || "—");
    setText("invStatTargetSub", target.ref ? String(target.ref) : "не выбран");
    if (target.ref) $("invTargetRef").value = target.ref;
    renderParse(data.parse || IDLE_PARSE);
    renderJob(data.job || IDLE_JOB);
    updateSlotStatus();
    renderSafetyPanel();
  }

  function parsePhaseLabel(phase) {
    switch (phase) {
      case "starting": return "Запуск…";
      case "resolving": return "Получаем чат…";
      case "scanning": return "Сканируем участников…";
      case "saving": return "Сохраняем в очередь…";
      case "done": return "Готово";
      case "error": return "Ошибка";
      default: return "Ожидание";
    }
  }

  function jobPhaseLabel(phase) {
    switch (phase) {
      case "backfill": return "Подготовка: загрузка профилей…";
      case "inviting": return "В работе";
      default: return "В работе";
    }
  }

  const JOB_OK = new Set(["invited", "invited_after_wait", "ok"]);
  const JOB_SKIP = new Set(["already_in_chat", "skipped_profile"]);
  const JOB_STAT_META = {
    invited: { label: "Успешно", cls: "ok" },
    invited_after_wait: { label: "Успешно", cls: "ok" },
    ok: { label: "Успешно", cls: "ok" },
    already_in_chat: { label: "Уже в чате", cls: "muted" },
    privacy_restricted: { label: "Приватность", cls: "warn" },
    premium_required: { label: "Нужен Premium", cls: "warn" },
    peer_flood: { label: "Flood", cls: "bad" },
    skipped_profile: { label: "Пропуск", cls: "muted" },
    resolve_failed: { label: "Не найден", cls: "bad" },
    user_too_many_channels: { label: "Лимит чатов", cls: "warn" },
    chat_write_forbidden: { label: "Нет прав", cls: "bad" },
    no_admin_rights: { label: "Нет админки", cls: "bad" },
    target_chat_private: { label: "Чат закрыт", cls: "bad" },
    invite_timeout: { label: "Таймаут", cls: "warn" },
  };

  let _invPieSeq = 0;

  function summarizeJobStats(stats) {
    let ok = 0;
    let err = 0;
    let skip = 0;
    Object.entries(stats || {}).forEach(function (kv) {
      const n = Number(kv[1]) || 0;
      if (n <= 0) return;
      const k = kv[0];
      if (JOB_OK.has(k)) ok += n;
      else if (JOB_SKIP.has(k) || k.indexOf("blocked_") === 0) skip += n;
      else err += n;
    });
    return { ok: ok, err: err, skip: skip };
  }

  function jobPctLabel(part, total) {
    const n = Number(part) || 0;
    const t = Number(total) || 0;
    if (n <= 0 || t <= 0) return "0%";
    const raw = (n / t) * 100;
    if (raw < 0.05) return "<0.1%";
    if (raw < 1) return raw.toFixed(1) + "%";
    return Math.round(raw) + "%";
  }

  function jobBarPct(part, total) {
    const n = Number(part) || 0;
    const t = Number(total) || 0;
    if (n <= 0 || t <= 0) return 0;
    const raw = (n / t) * 100;
    return Math.max(raw, 1.8);
  }

  function invJobPieSegments(ok, err, left) {
    const segs = [
      { v: ok, cls: "pie-sent" },
      { v: err, cls: "pie-error" },
      { v: left, cls: "pie-unsent" },
    ].filter(function (s) { return s.v > 0; });
    if (!segs.length) return [{ v: 1, cls: "pie-unsent" }];
    const grand = ok + err + left;
    const minFrac = 0.04;
    let boost = 0;
    const boosted = segs.map(function (s) {
      if (s.cls === "pie-unsent") return s;
      if (s.v / grand < minFrac) {
        boost += minFrac * grand - s.v;
        return { v: minFrac * grand, cls: s.cls };
      }
      return s;
    });
    if (boost <= 0) return boosted;
    return boosted.map(function (s) {
      if (s.cls !== "pie-unsent") return s;
      return { v: Math.max(0, s.v - boost), cls: s.cls };
    }).filter(function (s) { return s.v > 0; });
  }

  function invDonutSlice(cx, cy, rO, rI, a0, a1) {
    const rad = function (d) { return ((d - 90) * Math.PI) / 180; };
    const sw = a1 - a0;
    if (sw <= 0.05) return "";
    const lg = sw > 180 ? 1 : 0;
    const x1o = cx + rO * Math.cos(rad(a0));
    const y1o = cy + rO * Math.sin(rad(a0));
    const x2o = cx + rO * Math.cos(rad(a1));
    const y2o = cy + rO * Math.sin(rad(a1));
    const x1i = cx + rI * Math.cos(rad(a1));
    const y1i = cy + rI * Math.sin(rad(a1));
    const x2i = cx + rI * Math.cos(rad(a0));
    const y2i = cy + rI * Math.sin(rad(a0));
    return "M " + x1o + " " + y1o + " A " + rO + " " + rO + " 0 " + lg + " 1 " + x2o + " " + y2o +
      " L " + x1i + " " + y1i + " A " + rI + " " + rI + " 0 " + lg + " 0 " + x2i + " " + y2i + " Z";
  }

  function renderJobPiePanel(summary, progress, total, running, opts) {
    opts = opts || {};
    const phase = opts.phase || "";
    const backfillScanned = Number(opts.backfillScanned) || 0;
    if (running && phase === "backfill") {
      const uid = ++_invPieSeq;
      const scanLabel = backfillScanned > 0 ? String(backfillScanned) : "…";
      return '<div class="msg-pie-panel inv-job-pie">' +
        '<div class="msg-pie-ring"><svg viewBox="0 0 100 100" class="pie-svg" aria-hidden="true">' +
        '<circle cx="50" cy="50" r="42" fill="none" stroke="url(#invPieRem-' + uid + ')" stroke-width="14" class="inv-pie-pulse"></circle>' +
        '<defs><linearGradient id="invPieRem-' + uid + '" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#525252"/><stop offset="100%" stop-color="#262626"/></linearGradient></defs>' +
        '<text x="50" y="48" class="pie-center">' + scanLabel + '</text>' +
        '<text x="50" y="59" class="pie-center-sub">скан</text></svg></div>' +
        '<div class="msg-pie-metrics"><div class="msg-pie-metric msg-pie-metric--unsent">' +
        '<div class="msg-pie-metric-track"><div class="msg-pie-metric-fill inv-pie-pulse" style="width:35%"></div></div>' +
        '<div class="msg-pie-metric-row"><span class="msg-pie-metric-label">Подготовка</span>' +
        '<strong class="msg-pie-metric-value">загрузка профилей</strong></div></div></div></div>';
    }

    const done = Math.max(0, progress);
    const left = Math.max(0, (total || 0) - done);
    const ok = summary.ok;
    const err = summary.err;
    const grand = Math.max(total || ok + err + left, 1);
    const okPctLabel = jobPctLabel(ok, grand);
    const errPctLabel = jobPctLabel(err, grand);
    const leftPctLabel = jobPctLabel(left, grand);
    const donePctLabel = jobPctLabel(done, total);
    const uid = ++_invPieSeq;
    let angle = 0;
    const segs = invJobPieSegments(ok, err, left);
    const tot = segs.reduce(function (s, x) { return s + x.v; }, 0) || 1;
    const paths = segs.map(function (seg) {
      const sweep = (seg.v / tot) * 360;
      const a0 = angle + 1.5;
      const a1 = angle + sweep - 1.5;
      angle += sweep;
      const d = invDonutSlice(50, 50, 42, 28, a0, a1);
      if (!d) return "";
      const fill = seg.cls === "pie-sent" ? "url(#invPieSent-" + uid + ")" :
        seg.cls === "pie-error" ? "url(#invPieErr-" + uid + ")" : "url(#invPieRem-" + uid + ")";
      return '<path fill="' + fill + '" d="' + d + '"></path>';
    }).join("");
    const center = running ? donePctLabel : (err > 0 ? errPctLabel : donePctLabel);
    const sub = running ? "выполнено" : (err > 0 ? "ошибок" : "готово");
    const metrics = [
      { kind: "sent", label: "Успешно", value: String(ok), pct: jobBarPct(ok, grand), pctLabel: okPctLabel },
      { kind: "error", label: "Ошибки", value: String(err), pct: jobBarPct(err, grand), pctLabel: errPctLabel },
      { kind: "unsent", label: "Осталось", value: String(left), pct: left > 0 ? Math.max((left / grand) * 100, 0) : 0, pctLabel: leftPctLabel },
    ];
    const metricsHtml = metrics.map(function (m) {
      return '<div class="msg-pie-metric msg-pie-metric--' + m.kind + '">' +
        '<div class="msg-pie-metric-track"><div class="msg-pie-metric-fill" style="width:' + Math.min(100, m.pct) + '%"></div></div>' +
        '<div class="msg-pie-metric-row"><span class="msg-pie-metric-label">' + m.label +
        '</span><strong class="msg-pie-metric-value">' + m.value + " · " + m.pctLabel + '</strong></div></div>';
    }).join("");
    return '<div class="msg-pie-panel inv-job-pie">' +
      '<div class="msg-pie-ring"><svg viewBox="0 0 100 100" class="pie-svg" aria-hidden="true">' +
      '<defs>' +
      '<linearGradient id="invPieSent-' + uid + '" x1="18%" y1="12%" x2="88%" y2="92%"><stop offset="0%" stop-color="#fff"/><stop offset="100%" stop-color="#9ca3af"/></linearGradient>' +
      '<linearGradient id="invPieErr-' + uid + '" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#fca5a5"/><stop offset="100%" stop-color="#ef4444"/></linearGradient>' +
      '<linearGradient id="invPieRem-' + uid + '" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#525252"/><stop offset="100%" stop-color="#262626"/></linearGradient>' +
      '</defs><g>' + paths + '</g>' +
      '<text x="50" y="48" class="pie-center">' + center + '</text>' +
      '<text x="50" y="59" class="pie-center-sub">' + sub + '</text></svg></div>' +
      '<div class="msg-pie-metrics">' + metricsHtml + "</div></div>";
  }

  function jobStatPills(stats) {
    const entries = Object.entries(stats || {}).filter(function (kv) { return kv[1] > 0; });
    if (!entries.length) return "";
    entries.sort(function (a, b) { return b[1] - a[1]; });
    return entries.map(function (kv) {
      const meta = JOB_STAT_META[kv[0]] || { label: kv[0], cls: "muted" };
      return '<span class="inv-stat-pill ' + meta.cls + '"><b>' + kv[1] + "</b> " + escapeHtml(meta.label) + "</span>";
    }).join("");
  }

  function renderTaskPanel(containerId, opts) {
    const el = $(containerId);
    if (!el) return;

    const running = !!opts.running;
    const progress = Number(opts.progress) || 0;
    const total = Number(opts.total) || 0;
    const err = (opts.error || "").trim();
    const hasTotal = total > 0;
    const pct = hasTotal ? Math.min(100, Math.round((progress / total) * 100)) : 0;
    const hasResult = opts.result && opts.result.status;
    const hasStats = opts.stats && Object.keys(opts.stats).length > 0;
    const idle = !running && !err && progress === 0 && !hasStats && !hasResult;

    el.className = "inv-task-panel" + (running ? " running" : err ? " error" : idle ? " idle" : "");

    if (idle && opts.kind === "job") {
      el.innerHTML = '<div class="inv-task-empty"><span class="inv-task-empty-icon">◎</span><span>Инвайт не запущен — нажми «Запуск» после выбора target</span></div>';
      return;
    }
    if (idle && opts.kind === "parse") {
      el.innerHTML = '<div class="inv-task-empty"><span class="inv-task-empty-icon">⌕</span><span>Парс не запущен — укажи чат и нажми «Парсить»</span></div>';
      return;
    }

    let headHtml = "";
    let jobPieHtml = "";
    if (opts.kind === "job") {
      const statusCls = running ? "run" : err ? "err" : "idle";
      const statusLabel = running ? jobPhaseLabel(opts.phase || "") : err ? "Ошибка" : "Ожидание";
      const summary = summarizeJobStats(opts.stats);
      const legend = summary.err > 0
        ? summary.ok + " ок · " + summary.err + " ошибок · " + pct + "%"
        : progress + " / " + (hasTotal ? total : "—") + " · " + pct + "%";
      headHtml =
        '<div class="inv-task-head">' +
          '<span class="inv-task-badge ' + statusCls + '">' + statusLabel + "</span>" +
          '<span class="inv-task-head-count">' + legend + "</span>" +
        "</div>";
      if (running || hasStats || progress > 0) {
        jobPieHtml = renderJobPiePanel(summary, progress, total, running, {
          phase: opts.phase || "",
          backfillScanned: opts.backfillScanned || 0,
        });
      }
      if (opts.lastResult && running) {
        const meta = JOB_STAT_META[opts.lastResult] || { label: opts.lastResult, cls: "muted" };
        const sec = Number(opts.lastInviteSec) > 0 ? " · " + opts.lastInviteSec + " сек" : "";
        headHtml += '<div class="inv-task-live ' + meta.cls + '">Последнее: <b>' + escapeHtml(meta.label) + "</b>" + sec + "</div>";
      }
    } else {
      const phase = opts.phase || "";
      const title = opts.title || "—";
      const statusCls = running ? "run" : err ? "err" : "idle";
      const statusLabel = running ? parsePhaseLabel(phase) : err ? "Ошибка" : "Ожидание";
      const parsePct = progress > 0 ? Math.min(99, Math.max(6, Math.round(Math.log10(progress + 1) * 30))) : (running ? 6 : 0);
      headHtml =
        '<div class="inv-task-head">' +
          '<span class="inv-task-badge ' + statusCls + '">' + escapeHtml(statusLabel) + "</span>" +
          (title ? '<span class="inv-task-head-sub mono">' + escapeHtml(title) + "</span>" : "") +
        "</div>" +
        '<div class="inv-task-bar-row">' +
          '<div class="inv-task-bar"><div class="inv-task-bar-fill' + (running && progress === 0 ? " pulse" : "") + '" style="width:' + parsePct + '%"></div></div>' +
          '<span class="inv-task-bar-pct">' + (progress > 0 ? progress + " чел." : running ? "…" : "—") + "</span>" +
        "</div>";
    }

    let metricsHtml = "";
    if (opts.kind === "job" && hasStats) {
      metricsHtml = '<div class="inv-task-metrics">' + jobStatPills(opts.stats) + "</div>";
    }

    let statsHtml = "";
    if (opts.kind === "parse" && opts.result && opts.result.status === "ok") {
      statsHtml =
        '<span class="inv-stat-pill ok"><b>+' + (opts.result.added || 0) + "</b> в очередь</span>" +
        '<span class="inv-stat-pill muted"><b>' + (opts.result.duplicated || 0) + "</b> дубли</span>" +
        (opts.result.skipped ? '<span class="inv-stat-pill muted"><b>' + opts.result.skipped + "</b> пропущено</span>" : "") +
        (opts.result.blocked ? '<span class="inv-stat-pill warn"><b>' + opts.result.blocked + "</b> блок</span>" : "");
    } else if (opts.result && opts.result.status === "already_parsed") {
      statsHtml = '<span class="inv-stat-pill muted">Уже парсился ранее</span>';
    }

    el.innerHTML =
      headHtml +
      jobPieHtml +
      metricsHtml +
      (statsHtml ? '<div class="inv-task-stats">' + statsHtml + "</div>" : "") +
      (err ? '<div class="inv-task-error">' + escapeHtml(err) + "</div>" : "");
  }

  function renderParse(data) {
    const running = !!data.running;
    const progress = data.progress || 0;
    const phase = data.phase || "";
    const title = data.sourceTitle || data.sourceRef || "";
    const result = data.result || {};
    const err = data.lastError || "";
    const sub = running
      ? parsePhaseLabel(phase) + (progress > 0 ? " · " + progress + " чел." : "") + (title ? " · " + title : "")
      : (result.status === "ok"
        ? "Готово: +" + (result.added || 0) + " в очередь"
        : result.status === "already_parsed"
          ? "Уже парсился: " + (result.sourceChatTitle || title)
          : err || "Нет активного парса");

    setChip($("invParseChip"), running, running ? "парс идёт" : "парс ждёт");
    setChip($("invParseStatusChip"), running, running ? "парсинг" : "ожидание");
    setChip($("invDashParseChip"), running, running ? "парсинг" : "ожидание");
    setText("invParseStatusSub", sub);
    setText("invDashParseSub", sub);

    const panelOpts = {
      kind: "parse",
      running: running,
      progress: progress,
      phase: phase,
      title: title || data.sourceRef || "",
      result: result,
      error: err,
    };
    renderTaskPanel("invParseLog", panelOpts);
    renderTaskPanel("invDashParseLog", panelOpts);

    const card = $("invParseStatusCard");
    if (card) card.classList.toggle("running", running);

    const dot = $("invStatusDot");
    if (dot && running) dot.className = "dot-led pulse";
  }

  const IDLE_PARSE = { running: false, progress: 0, phase: "", sourceTitle: "", lastError: "", result: {} };
  const IDLE_JOB = { running: false, progress: 0, total: 0, stats: {}, lastError: "" };

  async function refreshParse() {
    if (!selectedBot()) {
      renderParse(IDLE_PARSE);
      return;
    }
    let data;
    try {
      data = await botApi("GET", "parse");
    } catch (e) {
      if (/method not allowed/i.test(String(e.message || e))) {
        renderParse(IDLE_PARSE);
        return;
      }
      throw e;
    }
    renderParse(data);
    if (data.running && !STATE.parseTimer) STATE.parseTimer = setInterval(refreshParse, 2000);
    if (!data.running && STATE.parseTimer) {
      clearInterval(STATE.parseTimer);
      STATE.parseTimer = null;
      if (data.result && data.result.status === "ok") {
        showAlert("Парс OK: +" + data.result.added + " (дубли " + data.result.duplicated + ")", "ok");
      } else if (data.result && data.result.status === "already_parsed") {
        showAlert("Чат уже парсился: " + (data.result.sourceChatTitle || ""));
      } else if (data.lastError) {
        showAlert(data.lastError);
      }
      refreshOverview();
      updateSlotStatus();
    }
  }

  function renderJob(data) {
    const running = !!data.running;
    const progress = data.progress || 0;
    const total = data.total || 0;
    const donePctLabel = jobPctLabel(progress, total);
    setChip($("invJobChip"), running, running ? "инвайт идёт" : "инвайт ждёт");
    setText("invStatProgress", total > 0 ? progress + "/" + total : "—");
    const summary = summarizeJobStats(data.stats);
    const errPctLabel = progress > 0 ? jobPctLabel(summary.err, progress) : "0%";
    setText("invStatProgressSub", running
      ? (data.phase === "backfill"
        ? "подготовка" + (data.backfillScanned ? " · " + data.backfillScanned + " чел." : "…")
        : summary.err > 0
          ? "выполнено " + donePctLabel + " · ошибок " + errPctLabel
          : "выполнено " + donePctLabel)
      : "ожидание запуска");
    const panelOpts = {
      kind: "job",
      running: running,
      phase: data.phase || "",
      backfillScanned: data.backfillScanned || 0,
      progress: progress,
      total: total,
      stats: data.stats || {},
      error: data.lastError || "",
      lastResult: data.lastResult || "",
      lastInviteSec: data.lastInviteSec || 0,
    };
    renderTaskPanel("invJobLog", panelOpts);
    setChip($("invDashInviteChip"), running, running ? "идёт" : "ожидание");
    setText("invDashInviteSub", running ? "Инвайт " + progress + " из " + total : "Job не запущен");
    renderTaskPanel("invDashInviteLog", panelOpts);
    const runBtn = $("invRunBtn");
    const stopBtn = $("invStopBtn");
    if (runBtn) runBtn.disabled = running;
    if (stopBtn) stopBtn.disabled = !running;
  }

  async function refreshJob() {
    if (!selectedBot()) {
      renderJob(IDLE_JOB);
      return;
    }
    let data;
    try {
      data = await botApi("GET", "job");
    } catch (e) {
      if (/method not allowed/i.test(String(e.message || e))) {
        renderJob(IDLE_JOB);
        return;
      }
      throw e;
    }
    renderJob(data);
    if (data.running && !STATE.jobTimer) STATE.jobTimer = setInterval(refreshJob, 1500);
    if (!data.running && STATE.jobTimer) {
      clearInterval(STATE.jobTimer);
      STATE.jobTimer = null;
      refreshOverview();
    }
  }

  async function loadDialogs() {
    const aid = selectedAccount();
    if (!aid) throw new Error("Выбери слот");
    const rows = await botApi("GET", "accounts/" + encodeURIComponent(aid) + "/dialogs");
    const sel = $("invDialogSelect");
    sel.innerHTML = '<option value="">— из списка чатов —</option>';
    rows.forEach(function (d) {
      const opt = document.createElement("option");
      opt.value = String(d.peerId);
      opt.textContent = d.title + " (" + d.peerId + ")";
      sel.appendChild(opt);
    });
    showAlert("Чаты обновлены: " + rows.length, "ok");
  }

  function bindTables() {
    $("invBorrowedBody")?.addEventListener("click", async function (e) {
      const btn = e.target.closest("button[data-borrow='use']");
      if (!btn) return;
      switchView("dashboard");
      $("invBotSelect").value = btn.dataset.id;
      try {
        await loadWorkspaceAccounts();
        updateSlotStatus();
        await refreshOverview();
        showAlert("VDS exsender выбран: " + botLabel(btn.dataset.id), "ok");
      } catch (err) { showAlert(err.message); }
    });
    $("invServersBody")?.addEventListener("click", function (e) {
      const btn = e.target.closest("button[data-srv]");
      if (!btn || btn.disabled) return;
      handleServerAction(btn.dataset.srv, btn.dataset.id);
    });
    $("invServersBody")?.addEventListener("change", async function (e) {
      const sel = e.target.closest("select.restart-interval-select[data-srv='restartInterval']");
      if (!sel || sel.disabled) return;
      const bid = sel.dataset.id;
      const hours = Number(sel.value);
      try {
        await api("PATCH", "/api/inviter/bots/" + encodeURIComponent(bid), { restartIntervalHours: hours });
        showAlert("Авто-перезапуск: " + fmtRestartInterval(hours), "ok");
        await loadBots();
        renderServersTable();
      } catch (err) { showAlert(err.message); }
    });
    $("invAccountsBody")?.addEventListener("click", async function (e) {
      const btn = e.target.closest("button[data-acc]");
      if (!btn) return;
      const tr = btn.closest("tr");
      const botId = tr?.dataset.bot;
      const accountId = tr?.dataset.id;
      if (!botId || !accountId) return;
      if (btn.dataset.acc === "login") {
        openAuthModal(botId, accountId);
        return;
      }
      if (btn.dataset.acc === "use") {
        switchView("dashboard");
        $("invBotSelect").value = botId;
        await loadWorkspaceAccounts();
        $("invAccountSelect").value = accountId;
        updateSlotStatus();
        await refreshOverview();
        await refreshJob();
        showAlert("Выбран " + accountId, "ok");
        return;
      }
      if (btn.dataset.acc === "delete") {
        const row = STATE.flatAccounts.find(function (a) { return a.botId === botId && a.id === accountId; });
        if (row && row.borrowed) {
          showAlert("Аккаунты exsender удаляются в основной панели");
          return;
        }
        if (!confirm("Удалить слот " + accountId + "?")) return;
        try {
          await panelApi("DELETE", botId, "accounts/" + encodeURIComponent(accountId));
          showAlert("Удалён", "ok");
          await loadFlatAccounts();
          renderAccountsTable();
          if (STATE.view === "dashboard" || STATE.view === "parse" || STATE.view === "invite") await loadWorkspaceAccounts();
        } catch (err) { showAlert(err.message); }
      }
    });
    $("invParsedBody")?.addEventListener("click", async function (e) {
      const btn = e.target.closest("button[data-parsed]");
      if (!btn) return;
      const tr = btn.closest("tr[data-source]");
      const sourceId = tr?.dataset.source || "";
      const aid = selectedAccount();
      if (!sourceId || !aid) return;
      const action = btn.dataset.parsed;
      try {
        if (action === "save") {
          const category = tr.querySelector("[data-field='category']")?.value || "other";
          const note = tr.querySelector("[data-field='note']")?.value || "";
          btn.disabled = true;
          await botApi("PATCH", "parsed/" + encodeURIComponent(sourceId), {
            accountId: aid,
            trafficCategory: category,
            note: note,
          });
          showAlert("Источник обновлён", "ok");
          await loadParsedSources();
          return;
        }
        if (action === "clear") {
          if (!confirm("Очистить очередь по этому источнику? Parsed-запись и заметка останутся.")) return;
          btn.disabled = true;
          const res = await botApi("POST", "parsed/" + encodeURIComponent(sourceId) + "/clear_queue", { accountId: aid });
          showAlert("Удалено из очереди: " + (res.removed || 0), "ok");
          await loadParsedSources();
          await refreshOverview();
          return;
        }
        if (action === "archive") {
          const item = STATE.parsedItems.find(function (x) { return String(x.source_chat_id) === sourceId; });
          const archived = !item?.archived_at;
          btn.disabled = true;
          await botApi("POST", "parsed/" + encodeURIComponent(sourceId) + "/archive", {
            accountId: aid,
            archived: archived,
          });
          showAlert(archived ? "Источник в архиве" : "Источник возвращён", "ok");
          await loadParsedSources();
        }
      } catch (err) {
        showAlert(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderNotifications(items) {
    const host = $("invNotifyHost");
    if (!host || !items?.length) return;
    host.innerHTML = items.map(function (n) {
      return '<div class="inv-alert inv-alert-ok inv-notify-banner" data-nid="' + escapeHtml(n.id) + '">' +
        '<span><b>' + escapeHtml(n.title) + '</b> — ' + escapeHtml(n.message) + '</span>' +
        '<button type="button" class="btn-ghost inv-notify-dismiss">OK</button></div>';
    }).join("");
    host.querySelectorAll(".inv-notify-dismiss").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const item = btn.closest("[data-nid]");
        const nid = item?.dataset.nid;
        if (nid) {
          try { await api("POST", "/api/users/notifications/" + encodeURIComponent(nid) + "/read", {}); } catch (_) { /* ignore */ }
        }
        item?.remove();
      });
    });
  }

  function startInviterOnboarding() {
    if (!window.ExsenderOnboarding || ExsenderOnboarding.isDone("inviter")) return;
    ExsenderOnboarding.startTour("inviter", [
      {
        selector: "#invPickerCard",
        title: "VDS и аккаунт",
        text: "Выбери сервер и слот — можно использовать VDS и аккаунты из exsender.",
      },
      {
        selector: '#invNav .nav-item[data-page="parse"]',
        title: "Парс",
        text: "Собери аудиторию из чата-источника в очередь на инвайт.",
      },
      {
        selector: '#invNav .nav-item[data-page="invite"]',
        title: "Инвайт",
        text: "Укажи target-чат, задержку и запусти приглашения.",
      },
      {
        selector: "#invNavProfile",
        title: "Профиль и оплата",
        text: "Тариф, уведомления и рефералка — прямо здесь, без перехода на exsender.top.",
      },
    ]);
  }

  async function init() {
    bindSidebar();
    bindModal();
    bindTables();
    fixCrossLinks();

    if (typeof ensureCsrf === "function") await ensureCsrf();
    const me = await api("GET", "/api/auth/me");
    if (!me.user) {
      window.location.href = "/login?next=/inviter";
      return;
    }
    STATE.isAdmin = me.kind === "admin";
    if (me.csrf && typeof setCsrfToken === "function") setCsrfToken(me.csrf);
    else if (typeof syncCsrfFromCookie === "function") syncCsrfFromCookie();
    else if (typeof refreshCsrf === "function") await refreshCsrf();
    setText("invUser", me.user || "—");
    if (typeof bindImpersonationFromMe === "function") bindImpersonationFromMe(me);

    if (me.kind === "user") {
      try {
        const prof = await api("GET", "/api/users/me");
        renderNotifications(prof.notifications || []);
      } catch (e) { console.warn("inviter profile", e); }
    }

    document.querySelectorAll("#invNav .nav-item[data-page]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        switchView(el.dataset.page);
      });
    });

    $("invLogoutBtn").addEventListener("click", async function () {
      await api("POST", "/api/auth/logout", {});
      window.location.href = "/login";
    });
    $("invRefreshBtn").addEventListener("click", function () {
      refreshCurrentView().then(function () { showAlert("Обновлено", "ok"); }).catch(function (e) { showAlert(e.message); });
    });
    $("invAddServerBtn")?.addEventListener("click", openAddServerModal);
    $("invAddAccountBtn")?.addEventListener("click", openAddAccountModal);
    $("invAccountsBotFilter")?.addEventListener("change", function (e) {
      STATE.accountsBotFilter = e.target.value;
      renderAccountsTable();
    });
    $("invParsedCategoryFilter")?.addEventListener("change", function (e) {
      STATE.parsedCategoryFilter = e.target.value;
      renderParsedTable();
      renderSafetyPanel();
    });
    $("invParsedShowArchived")?.addEventListener("change", function (e) {
      STATE.parsedShowArchived = !!e.target.checked;
      loadParsedSources().catch(function (err) { showAlert(err.message); });
    });
    ["invLimit", "invDelay"].forEach(function (id) {
      const el = $(id);
      if (!el) return;
      el.addEventListener("input", renderSafetyPanel);
      el.addEventListener("change", renderSafetyPanel);
    });

    $("invBotSelect").addEventListener("change", async function () {
      try {
        await loadWorkspaceAccounts();
        await refreshOverview();
        await refreshJob();
        if (STATE.view === "parsed") await loadParsedSources();
      } catch (e) { showAlert(e.message); }
    });
    $("invAccountSelect").addEventListener("change", function () {
      refreshOverview()
        .then(function () { if (STATE.view === "parsed") return loadParsedSources(); })
        .catch(function (e) { showAlert(e.message); });
    });

    $("invParseBtn").addEventListener("click", async function () {
      const btn = $("invParseBtn");
      const prev = btn?.textContent || "Парсить";
      try {
        const aid = selectedAccount();
        if (!aid) throw new Error("Выбери слот");
        const sourceRef = $("invSourceRef").value.trim();
        if (!sourceRef) throw new Error("Укажи ссылку источника");
        if (btn) { btn.disabled = true; btn.textContent = "Запуск…"; }
        const res = await botApi("POST", "parse", { accountId: aid, sourceRef: sourceRef, force: $("invParseForce").checked });
        if (res.status === "started") {
          showAlert("Парс запущен", "ok");
          await refreshParse();
          if (!STATE.parseTimer) STATE.parseTimer = setInterval(refreshParse, 2000);
        }
      } catch (e) { showAlert(e.message); }
      finally { if (btn) { btn.disabled = false; btn.textContent = prev; } }
    });

    document.querySelectorAll("[data-goto]").forEach(function (el) {
      el.addEventListener("click", function () {
        switchView(el.getAttribute("data-goto") || "dashboard");
      });
    });

    $("invTargetBtn").addEventListener("click", async function () {
      try {
        const aid = selectedAccount();
        if (!aid) throw new Error("Выбери слот");
        const targetRef = $("invTargetRef").value.trim();
        if (!targetRef) throw new Error("Укажи target");
        const res = await botApi("POST", "target", { accountId: aid, targetRef: targetRef });
        showAlert("Target: " + (res.title || res.ref), "ok");
        await refreshOverview();
      } catch (e) { showAlert(e.message); }
    });

    $("invDialogSelect").addEventListener("change", async function () {
      const val = $("invDialogSelect").value;
      if (!val) return;
      $("invTargetRef").value = val;
      try {
        await botApi("POST", "target", { accountId: selectedAccount(), targetRef: val });
        showAlert("Target выбран", "ok");
        await refreshOverview();
      } catch (e) { showAlert(e.message); }
    });

    $("invDialogsBtn").addEventListener("click", function () {
      loadDialogs().catch(function (e) { showAlert(e.message); });
    });

    $("invRunBtn").addEventListener("click", async function () {
      const btn = $("invRunBtn");
      const prev = btn ? btn.textContent : "Запуск";
      try {
        const aid = selectedAccount();
        if (!aid) throw new Error("Выбери слот");
        if (btn) { btn.disabled = true; btn.textContent = "Запуск…"; }
        await botApi("POST", "run", {
          accountId: aid,
          limit: parseInt($("invLimit").value || "0", 10) || 0,
          delay: parseFloat($("invDelay").value || "3") || 3,
        });
        showAlert("Инвайт запущен", "ok");
        await refreshJob();
        if (!STATE.jobTimer) STATE.jobTimer = setInterval(refreshJob, 1500);
      } catch (e) {
        showAlert(e.message);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
      }
    });

    $("invStopBtn").addEventListener("click", async function () {
      try {
        const bid = selectedBot();
        if (!bid) throw new Error("Выбери VDS");
        const res = await api("POST", "/api/inviter/bots/" + encodeURIComponent(bid) + "/invite/stop", {});
        if (res && res.ok === false) {
          showAlert(res.message || "Нет активного инвайта");
        } else {
          showAlert("Инвайт остановлен", "ok");
        }
        await refreshJob();
      } catch (e) { showAlert(e.message); }
    });

    switchView("dashboard");
    startInviterOnboarding();
  }

  init().catch(function (e) { showAlert(e.message || String(e)); });
})();
