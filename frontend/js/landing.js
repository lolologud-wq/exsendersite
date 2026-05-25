(function () {
  const burger = document.getElementById("lpBurger");
  const menu = document.getElementById("lpMobileMenu");
  if (!burger || !menu) return;

  function setOpen(open) {
    document.body.classList.toggle("lp-menu-open", open);
    burger.setAttribute("aria-expanded", open ? "true" : "false");
    menu.hidden = !open;
  }

  burger.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("lp-menu-open"));
  });

  menu.querySelectorAll("a").forEach((a) => {
    a.addEventListener("click", () => setOpen(false));
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 720) setOpen(false);
  });

  // Show profile link if already logged in
  fetch("/api/auth/me", { credentials: "same-origin" })
    .then((r) => r.json())
    .then((data) => {
      if (!data?.user) return;
      const prof = document.getElementById("lpNavProfile");
      if (prof) prof.hidden = false;
    })
    .catch(() => {});

  // Hero pie — accurate segment sizes (23980 sent / 620 err / 13400 rem)
  const sent = 23980;
  const err = 620;
  const rem = 13400;
  const total = sent + err + rem;
  const sentPct = (sent / total) * 100;
  const errPct = (err / total) * 100;
  const svg = document.getElementById("lpHeroPie");
  if (svg) {
    const circles = svg.querySelectorAll("circle[stroke-dasharray]");
    if (circles.length >= 2) {
      circles[0].setAttribute("stroke-dasharray", `${sentPct.toFixed(1)} ${(100 - sentPct).toFixed(1)}`);
      circles[1].setAttribute("stroke-dasharray", `${errPct.toFixed(1)} ${(100 - errPct).toFixed(1)}`);
      circles[1].setAttribute("stroke-dashoffset", `-${sentPct.toFixed(1)}`);
    }
    const label = svg.querySelector("text");
    if (label) label.textContent = `${Math.round(sentPct)}%`;
  }
})();
