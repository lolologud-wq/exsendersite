(function (global) {
  const STORAGE_KEY = "exsender_onboarding_v2";

  function getState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      return {};
    }
  }

  function markDone(tourId) {
    const s = getState();
    s[tourId] = Date.now();
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  }

  function isDone(tourId) {
    return !!getState()[tourId];
  }

  function startTour(tourId, steps, opts) {
    opts = opts || {};
    if (isDone(tourId) && !opts.force) return;
    if (!steps || !steps.length) return;

    let idx = 0;
    let root = null;
    let card = null;
    let backdrop = null;

    function cleanup() {
      root?.remove();
      root = null;
      card = null;
      backdrop = null;
      document.body.classList.remove("ob-tour-active");
    }

    function finish(skipped) {
      cleanup();
      markDone(tourId);
      if (typeof opts.onDone === "function") opts.onDone(!!skipped);
    }

    function place() {
      const step = steps[idx];
      if (!step) return finish(false);
      if (typeof step.before === "function") step.before();
      const target = typeof step.selector === "string"
        ? document.querySelector(step.selector)
        : step.selector;
      if (!target) {
        idx += 1;
        if (idx >= steps.length) return finish(false);
        return place();
      }
      target.scrollIntoView({ block: "nearest", behavior: "smooth" });
      const rect = target.getBoundingClientRect();
      const pad = 8;
      if (backdrop) {
        backdrop.style.top = Math.max(0, rect.top - pad) + "px";
        backdrop.style.left = Math.max(0, rect.left - pad) + "px";
        backdrop.style.width = Math.min(window.innerWidth, rect.width + pad * 2) + "px";
        backdrop.style.height = Math.min(window.innerHeight, rect.height + pad * 2) + "px";
      }
      if (card) {
        card.querySelector(".ob-title").textContent = step.title || "";
        card.querySelector(".ob-text").textContent = step.text || "";
        card.querySelector(".ob-step").textContent = (idx + 1) + " / " + steps.length;
        const nextBtn = card.querySelector(".ob-next");
        nextBtn.textContent = idx >= steps.length - 1 ? "Готово" : "Далее";
        const top = Math.min(window.innerHeight - 180, rect.bottom + 16);
        const left = Math.min(window.innerWidth - 320, Math.max(12, rect.left));
        card.style.top = top + "px";
        card.style.left = left + "px";
      }
    }

    root = document.createElement("div");
    root.className = "ob-root";
    root.innerHTML =
      '<div class="ob-scrim"></div>' +
      '<div class="ob-spot" aria-hidden="true"></div>' +
      '<div class="ob-card" role="dialog" aria-live="polite">' +
      '<div class="ob-step"></div>' +
      '<h4 class="ob-title"></h4>' +
      '<p class="ob-text"></p>' +
      '<div class="ob-actions">' +
      '<button type="button" class="btn-ghost ob-skip">Пропустить</button>' +
      '<button type="button" class="btn-primary ob-next">Далее</button>' +
      "</div></div>";
    document.body.appendChild(root);
    document.body.classList.add("ob-tour-active");
    backdrop = root.querySelector(".ob-spot");
    card = root.querySelector(".ob-card");

    root.querySelector(".ob-skip").addEventListener("click", () => finish(true));
    root.querySelector(".ob-next").addEventListener("click", () => {
      idx += 1;
      if (idx >= steps.length) finish(false);
      else place();
    });
    root.querySelector(".ob-scrim").addEventListener("click", () => finish(true));
    document.addEventListener("keydown", function onKey(e) {
      if (e.key === "Escape") {
        document.removeEventListener("keydown", onKey);
        finish(true);
      }
    });

    place();
  }

  global.ExsenderOnboarding = { startTour, markDone, isDone };
})(typeof window !== "undefined" ? window : globalThis);
