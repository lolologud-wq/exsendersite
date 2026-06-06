(function () {
  let bots = [];
  let accounts = [];
  let jobTimer = null;

  function $(id) { return document.getElementById(id); }

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

  async function api(method, path, body) {
    const opts = { method: method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await secureFetch(path, opts);
    if (r.status === 401 || r.status === 403) {
      window.location.href = "/login";
      throw new Error("not admin");
    }
    const data = await r.json().catch(function () { return {}; });
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === "string" ? d : (data.error || ("HTTP " + r.status)));
    }
    return data;
  }

  function selectedBot() { return $("invBotSelect").value; }
  function selectedAccount() { return $("invAccountSelect").value; }

  function botApi(method, sub, body, params) {
    const bid = selectedBot();
    if (!bid) throw new Error("Выбери бота");
    let url = "/api/inviter/bots/" + encodeURIComponent(bid) + "/" + sub.replace(/^\/+/, "");
    if (params) {
      const qs = new URLSearchParams(params);
      url += "?" + qs.toString();
    }
    return api(method, url, body);
  }

  async function loadBots() {
    const data = await api("GET", "/api/inviter/bots");
    bots = data.bots || [];
    const sel = $("invBotSelect");
    const cur = sel.value;
    sel.innerHTML = '<option value="">— выбери VDS бота —</option>';
    bots.forEach(function (b) {
      const opt = document.createElement("option");
      opt.value = b.id;
      opt.textContent = (b.alias || b.host) + (b.reachable ? "" : " (offline)");
      sel.appendChild(opt);
    });
    if (cur) sel.value = cur;
  }

  async function loadAccounts() {
    const bid = selectedBot();
    const accSel = $("invAccountSelect");
    accSel.innerHTML = '<option value="">— слот аккаунта —</option>';
    accounts = [];
    if (!bid) return;
    const ov = await api("GET", "/api/inviter/bots/" + encodeURIComponent(bid) + "/accounts");
    accounts = (ov.accounts || []);
    accounts.forEach(function (a) {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = a.id + (a.authorized ? "" : " (не авторизован)");
      accSel.appendChild(opt);
    });
  }

  async function refreshOverview() {
    const aid = selectedAccount();
    if (!aid) {
      $("invOverview").textContent = "Выбери слот аккаунта";
      return;
    }
    const data = await botApi("GET", "overview", undefined, { accountId: aid });
    const target = data.target || {};
    $("invOverview").textContent =
      "Очередь: " + (data.queueCount || 0) +
      " · Парсилось чатов: " + (data.parsedChatsCount || 0) +
      " · Target: " + (target.title || target.ref || "—") +
      (data.authorized ? "" : " · аккаунт не авторизован");
    if (target.ref) $("invTargetRef").value = target.ref;
  }

  async function refreshJob() {
    const data = await botApi("GET", "job");
    const lines = [
      "running: " + !!data.running,
      "progress: " + (data.progress || 0) + "/" + (data.total || 0),
      "stats: " + JSON.stringify(data.stats || {}),
      "error: " + (data.lastError || "—"),
    ];
    $("invJobLog").textContent = lines.join("\n");
    if (data.running && !jobTimer) {
      jobTimer = setInterval(refreshJob, 3000);
    }
    if (!data.running && jobTimer) {
      clearInterval(jobTimer);
      jobTimer = null;
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

  async function init() {
    const me = await api("GET", "/api/auth/me");
    if (me.kind !== "admin") {
      window.location.href = "/login";
      return;
    }
    $("invUser").textContent = me.user || "admin";

    $("invLogoutBtn").addEventListener("click", async function () {
      await api("POST", "/api/auth/logout", {});
      window.location.href = "/login";
    });

    $("invBotSelect").addEventListener("change", async function () {
      try {
        await loadAccounts();
        await refreshOverview();
        await refreshJob();
      } catch (e) { showAlert(e.message); }
    });

    $("invAccountSelect").addEventListener("change", async function () {
      try {
        await refreshOverview();
      } catch (e) { showAlert(e.message); }
    });

    $("invRefreshBtn").addEventListener("click", async function () {
      try {
        await loadBots();
        await loadAccounts();
        await refreshOverview();
        await refreshJob();
        showAlert("Обновлено", "ok");
      } catch (e) { showAlert(e.message); }
    });

    $("invParseBtn").addEventListener("click", async function () {
      try {
        const aid = selectedAccount();
        if (!aid) throw new Error("Выбери слот");
        const sourceRef = $("invSourceRef").value.trim();
        if (!sourceRef) throw new Error("Укажи ссылку источника");
        const res = await botApi("POST", "parse", {
          accountId: aid,
          sourceRef: sourceRef,
          force: $("invParseForce").checked,
        });
        if (res.status === "already_parsed") {
          showAlert("Чат уже парсился: " + res.sourceChatTitle);
          return;
        }
        showAlert(
          "Парс OK: +" + res.added + " (дубли " + res.duplicated + ")",
          "ok"
        );
        await refreshOverview();
      } catch (e) { showAlert(e.message); }
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
        const aid = selectedAccount();
        await botApi("POST", "target", { accountId: aid, targetRef: val });
        showAlert("Target выбран из списка", "ok");
        await refreshOverview();
      } catch (e) { showAlert(e.message); }
    });

    $("invDialogsBtn").addEventListener("click", async function () {
      try { await loadDialogs(); } catch (e) { showAlert(e.message); }
    });

    $("invRunBtn").addEventListener("click", async function () {
      try {
        const aid = selectedAccount();
        if (!aid) throw new Error("Выбери слот");
        const limit = parseInt($("invLimit").value || "0", 10) || 0;
        const delay = parseFloat($("invDelay").value || "3") || 3;
        await botApi("POST", "run", { accountId: aid, limit: limit, delay: delay });
        showAlert("Инвайт запущен", "ok");
        await refreshJob();
        if (!jobTimer) jobTimer = setInterval(refreshJob, 3000);
      } catch (e) { showAlert(e.message); }
    });

    $("invStopBtn").addEventListener("click", async function () {
      try {
        await botApi("POST", "stop", {});
        showAlert("Стоп отправлен", "ok");
        await refreshJob();
      } catch (e) { showAlert(e.message); }
    });

    await loadBots();
    await refreshJob();
  }

  init().catch(function (e) {
    showAlert(e.message || String(e));
  });
})();
