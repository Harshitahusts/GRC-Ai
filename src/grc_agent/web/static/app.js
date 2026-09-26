// Shared frontend behaviour: "coming soon" buttons, search palette, keyboard
// shortcuts, and the login form helpers. No backend calls here.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // ---- Toasts (also used by the notifications poller in base.html)
  const toasts = $("#toasts");
  function toast(text, level = "info", sub = "") {
    if (!toasts) return;
    const el = document.createElement("div");
    el.className = `toast level-${level}`;
    el.setAttribute("role", "status");
    el.textContent = text;
    if (sub) {
      const small = document.createElement("small");
      small.textContent = sub;
      el.append(small);
    }
    toasts.append(el);
    setTimeout(() => el.classList.add("gone"), 5000);
    setTimeout(() => el.remove(), 5600);
  }
  window.grcToast = toast;

  // ---- Planned features: the button exists, the backend doesn't yet.
  let planned = {};
  try { planned = JSON.parse($("#planned-features")?.textContent || "{}"); } catch (e) {}
  document.addEventListener("click", (event) => {
    const el = event.target.closest("[data-soon]");
    if (!el) return;
    event.preventDefault();
    const info = planned[el.dataset.soon] || {};
    if (el.type === "checkbox") el.checked = false;
    toast(`${info.label || "This feature"} is coming soon`, "warning", info.backend ? `Planned: ${info.backend}` : "");
  });
  document.addEventListener("change", (event) => {
    const el = event.target.closest("select[data-soon]");
    if (!el) return;
    const info = planned[el.dataset.soon] || {};
    el.selectedIndex = 0;
    toast(`${info.label || "This filter"} is coming soon`, "warning", info.backend ? `Planned: ${info.backend}` : "");
  });

  // ---- Login form: show/hide password, Caps Lock hint, busy state.
  const login = $("#login-form");
  if (login) {
    const pw = $("#password", login);
    const reveal = $("#reveal", login);
    reveal?.addEventListener("click", () => {
      const show = pw.type === "password";
      pw.type = show ? "text" : "password";
      reveal.setAttribute("aria-pressed", String(show));
      reveal.setAttribute("aria-label", show ? "Hide password" : "Show password");
      $("use", reveal).setAttribute("href", show ? "#i-eye-off" : "#i-eye");
      pw.focus();
    });
    const caps = $("#caps", login);
    const checkCaps = (e) => { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); };
    pw.addEventListener("keydown", checkCaps);
    pw.addEventListener("keyup", checkCaps);
    pw.addEventListener("blur", () => { caps.hidden = true; });
    login.addEventListener("submit", () => {
      const btn = $("button[type=submit]", login);
      btn.disabled = true;
      btn.classList.add("busy");
      $(".label", btn).textContent = "Signing in…";
    });
  }

  // ---- Search palette
  const palette = $("#palette");
  const shortcuts = $("#shortcuts");
  if (palette) {
    const q = $("#palette-q");
    const echo = $("#palette-echo");
    const options = $$("#palette-list li");
    let active = 0;
    const visible = () => options.filter((li) => !li.hidden);
    function mark() {
      visible().forEach((li, i) => li.classList.toggle("active", i === active));
      visible()[active]?.scrollIntoView({ block: "nearest" });
    }
    function filter() {
      const term = q.value.trim().toLowerCase();
      options.forEach((li) => {
        li.hidden = li.dataset.keep === undefined && !!term && !li.textContent.toLowerCase().includes(term);
      });
      $(".palette-search-all").hidden = !term;
      echo.textContent = q.value.trim() || "…";
      active = 0;
      mark();
    }
    window.openPalette = () => {
      if (palette.open) return;
      q.value = "";
      filter();
      palette.showModal();
      q.focus();
    };
    $("#open-palette")?.addEventListener("click", window.openPalette);
    q.addEventListener("input", filter);
    q.addEventListener("keydown", (e) => {
      const list = visible();
      if (e.key === "ArrowDown") { active = Math.min(active + 1, list.length - 1); mark(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); mark(); e.preventDefault(); }
      else if (e.key === "Enter") { list[active]?.querySelector("a, button")?.click(); e.preventDefault(); }
    });
    palette.addEventListener("click", (e) => {
      if (e.target === palette) palette.close();  // click on the backdrop
      const act = e.target.closest("[data-action=theme]");
      if (act) { window.toggleTheme?.(); palette.close(); }
      if (e.target.closest("a")) palette.close();
    });
  }
  $("#open-shortcuts")?.addEventListener("click", () => shortcuts?.showModal());
  shortcuts?.addEventListener("click", (e) => { if (e.target === shortcuts) shortcuts.close(); });

  // ---- Keyboard shortcuts (ignored while typing in a field)
  const GO = { d: "/", e: "/engagements", a: "/assistant", f: "/dataflows", c: "/connectors", n: "/notifications", m: "/data-manager" };
  let pendingG = 0;
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && palette) {
      e.preventDefault();
      window.openPalette();
      return;
    }
    const typing = e.target.closest("input, textarea, select, [contenteditable]");
    if (typing || e.ctrlKey || e.metaKey || e.altKey || !palette || document.querySelector("dialog[open]")) return;
    if (pendingG && GO[e.key]) { location.href = GO[e.key]; pendingG = 0; return; }
    pendingG = 0;
    if (e.key === "g") { pendingG = setTimeout(() => { pendingG = 0; }, 1200); }
    else if (e.key === "/") { e.preventDefault(); window.openPalette(); }
    else if (e.key === "?") { shortcuts?.showModal(); }
    else if (e.key === "n") { location.href = "/engagements#new"; }
  });

  // Close the account menu when clicking elsewhere.
  document.addEventListener("click", (e) => {
    $$("details.user-menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  // ---- Dashboard helpers
  const greet = $("#greeting");
  if (greet) {
    const now = new Date();
    const h = now.getHours();
    const part = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
    const day = now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    greet.textContent = `${part}, ${greet.dataset.user} · ${day}`;
  }
  const setup = $("#getting-started");
  if (setup) {
    const KEY = "grc.setup.hidden";
    try { if (localStorage.getItem(KEY) === "1") setup.hidden = true; } catch (e) {}
    $("#hide-setup")?.addEventListener("click", () => {
      setup.hidden = true;
      try { localStorage.setItem(KEY, "1"); } catch (e) {}
      toast("Getting started hidden. It comes back if you clear site data.", "info");
    });
  }

  // ---- Table filter (Data manager): text box + category chips, all client-side.
  $$("[data-filter-table]").forEach((box) => {
    const table = document.getElementById(box.dataset.filterTable);
    const input = $("input", box);
    const chips = $$("[data-cat]", box);
    const count = $("[data-count]", box);
    let cat = "";
    function apply() {
      const term = input.value.trim().toLowerCase();
      let shown = 0;
      $$("tbody tr", table).forEach((tr) => {
        const ok = (!term || tr.textContent.toLowerCase().includes(term)) && (!cat || tr.dataset.cat === cat);
        tr.hidden = !ok;
        shown += ok;
      });
      if (count) count.textContent = `${shown} shown`;
    }
    input.addEventListener("input", apply);
    chips.forEach((chip) => chip.addEventListener("click", () => {
      cat = chip.dataset.cat;
      chips.forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
      apply();
    }));
  });
})();
