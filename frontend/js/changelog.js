(function () {
  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtDate(d) {
    if (!d) return "";
    try {
      return new Date(d + "T12:00:00").toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "long",
        year: "numeric",
      });
    } catch (_) {
      return d;
    }
  }

  function renderEntry(entry) {
    const tags = (entry.tags || [])
      .map((t) => `<span class="cl-tag">${escapeHtml(t)}</span>`)
      .join("");
    return `
      <article class="cl-entry">
        <div class="cl-entry-head">
          <h2 class="cl-entry-title">${escapeHtml(entry.title)}</h2>
          ${entry.version ? `<span class="cl-version">v${escapeHtml(entry.version)}</span>` : ""}
          <span class="cl-entry-meta">${escapeHtml(fmtDate(entry.date))}</span>
        </div>
        ${tags ? `<div class="cl-tags">${tags}</div>` : ""}
        <div class="cl-body-text">${escapeHtml(entry.body || "")}</div>
      </article>`;
  }

  async function load() {
    const feed = document.getElementById("clFeed");
    if (!feed) return;
    try {
      const r = await fetch("/api/changelog");
      const data = await r.json();
      const items = data.items || [];
      if (!items.length) {
        feed.innerHTML = '<div class="cl-empty">Записей пока нет.</div>';
        return;
      }
      feed.innerHTML = items.map(renderEntry).join("");
    } catch (e) {
      feed.innerHTML = '<div class="cl-error">Не удалось загрузить changelog.</div>';
      console.warn(e);
    }
  }

  load();
})();
