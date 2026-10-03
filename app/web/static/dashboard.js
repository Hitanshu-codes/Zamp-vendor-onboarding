// Dashboard: detail drawer, accessible tabs, and chart hover tooltips.
(function () {
  "use strict";

  // --- tabs (works for the drawer content and the full submission page) ---------------------
  function initTabs(root) {
    const tabs = Array.from(root.querySelectorAll('[role="tab"]'));
    function select(tab) {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = root.querySelector(`#${t.getAttribute("aria-controls")}`);
        if (panel) panel.hidden = !on;
      });
      tab.focus();
    }
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight") select(tabs[(i + 1) % tabs.length]);
        if (e.key === "ArrowLeft") select(tabs[(i - 1 + tabs.length) % tabs.length]);
      });
    });
  }
  document.querySelectorAll("[data-detail]").forEach(initTabs);

  // --- drawer --------------------------------------------------------------------------------
  const drawer = document.getElementById("drawer");
  const backdrop = document.getElementById("drawer-backdrop");
  const body = document.getElementById("drawer-body");
  let lastFocus = null;

  async function openDrawer(id) {
    if (!drawer) return;
    lastFocus = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.getElementById("drawer-open-page").href = `/submissions/${id}`;
    body.innerHTML = '<p class="muted"><span class="spinner" aria-hidden="true"></span> Loading…</p>';  // static markup only
    try {
      const res = await fetch(`/submissions/${id}/drawer`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Server-rendered with Jinja autoescape: every untrusted value is already HTML-escaped.
      body.innerHTML = await res.text();
      initTabs(body);
      const first = body.querySelector('[role="tab"]');
      if (first) first.focus();
    } catch (err) {
      body.textContent = `Could not load this submission (${err.message}).`;
    }
  }
  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    if (lastFocus) lastFocus.focus();
  }
  if (drawer) {
    document.getElementById("drawer-close").addEventListener("click", closeDrawer);
    backdrop.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
    document.querySelectorAll("#subs-table tbody tr").forEach((tr) => {
      const open = (e) => { e.preventDefault(); openDrawer(tr.dataset.sub); };
      tr.addEventListener("click", (e) => { if (!e.ctrlKey && !e.metaKey) open(e); });
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(e); });
    });
  }

  // --- chart tooltips (hover and keyboard focus) ----------------------------------------------
  const tip = document.getElementById("tip");
  if (tip) {
    function show(el) {
      tip.textContent = el.dataset.tip;
      tip.hidden = false;
      const r = el.getBoundingClientRect();
      const top = r.top + window.scrollY - tip.offsetHeight - 8;
      const left = Math.min(window.scrollX + r.left + r.width / 2 - tip.offsetWidth / 2, window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 8);
      tip.style.top = `${Math.max(top, window.scrollY + 4)}px`;
      tip.style.left = `${Math.max(left, 8)}px`;
    }
    document.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("mouseenter", () => show(el));
      el.addEventListener("focus", () => show(el));
      el.addEventListener("mouseleave", () => { tip.hidden = true; });
      el.addEventListener("blur", () => { tip.hidden = true; });
    });
  }
})();
