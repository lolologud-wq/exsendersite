(function () {
  function bindPfMobileNav() {
    const btn = document.getElementById("pfMenuBtn");
    const nav = document.getElementById("pfMobileNav");
    const backdrop = document.getElementById("pfMobileBackdrop");
    if (!btn || !nav) return;

    function setOpen(open) {
      document.body.classList.toggle("pf-menu-open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (backdrop) {
        backdrop.hidden = !open;
        backdrop.setAttribute("aria-hidden", open ? "false" : "true");
      }
    }

    btn.addEventListener("click", () => {
      setOpen(!document.body.classList.contains("pf-menu-open"));
    });
    backdrop?.addEventListener("click", () => setOpen(false));
    nav.querySelectorAll("a").forEach((a) => {
      a.addEventListener("click", () => setOpen(false));
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("pf-menu-open")) {
        setOpen(false);
      }
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 768) setOpen(false);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindPfMobileNav);
  } else {
    bindPfMobileNav();
  }
})();
