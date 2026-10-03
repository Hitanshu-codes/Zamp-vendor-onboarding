// Live run view. Consumes the event stream (SSE, with polling fallback) and renders stages, checks and the decision.
// Every untrusted value is inserted with textContent, never innerHTML (INV-4).
(function () {
  "use strict";
  const root = document.getElementById("run");
  if (!root) return;
  const runId = root.dataset.runId;
  let lastSeq = 0;
  let finished = false;

  const RESULT = {
    pass: { cls: "b-ok", icon: "✓", text: "Pass" },
    fail: { cls: "b-bad", icon: "✕", text: "Fail" },
    warn: { cls: "b-warn", icon: "!", text: "Warning" },
    skipped: { cls: "b-neutral", icon: "–", text: "Skipped" },
  };
  const SEVERITY = { fixable: "b-warn", escalate: "b-review", reject: "b-bad", soft: "b-info", info: "b-neutral" };
  const STATUS = {
    approved: { cls: "b-ok", icon: "✓", text: "Approved" },
    pending: { cls: "b-warn", icon: "◷", text: "Pending" },
    rejected: { cls: "b-bad", icon: "✕", text: "Rejected" },
  };
  const ROUTE = {
    none: { cls: "b-neutral", icon: "•", text: "No further action" },
    vendor_followup: { cls: "b-warn", icon: "✉", text: "Ask the vendor" },
    manual_review: { cls: "b-review", icon: "⚑", text: "Manual review" },
  };
  const HIDDEN_KEYS = new Set(["llm_tokens"]);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  }
  function badge(spec) {
    return el("span", { class: `badge ${spec.cls}` }, el("span", { class: "i", "aria-hidden": "true", text: spec.icon }), spec.text);
  }
  function fmtValue(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (typeof v === "object") return JSON.stringify(v, null, 1);
    return String(v);
  }

  function evidenceTable(evidence) {
    const entries = Object.entries(evidence || {}).filter(([k]) => !HIDDEN_KEYS.has(k));
    if (!entries.length) return null;
    const dl = el("dl", { class: "kv evidence" });
    for (const [k, v] of entries) {
      dl.append(el("dt", { text: k.replace(/_/g, " ") }));
      const isBlock = typeof v === "object" && v !== null;
      dl.append(el("dd", {}, isBlock ? el("pre", { class: "ev-pre", text: fmtValue(v) }) : fmtValue(v)));
    }
    return dl;
  }

  function stageEl(key) { return document.getElementById(`stage-${key}`); }
  function setStage(key, state, label) {
    const s = stageEl(key);
    if (!s) return;
    s.dataset.state = state;
    s.querySelector(".step-state").textContent = label;
  }
  function toggle(stage, open) {
    const head = stage.querySelector(".step-head");
    const body = stage.querySelector(".step-body");
    const want = open === undefined ? body.hidden : open;
    body.hidden = !want;
    head.setAttribute("aria-expanded", String(want));
  }
  document.querySelectorAll(".step-head").forEach((h) => h.addEventListener("click", () => toggle(h.closest(".step"))));

  function renderCheck(stageKey, p) {
    const s = stageEl(stageKey);
    if (!s) return;
    const body = s.querySelector(".step-body");
    const isProblem = p.result === "fail" || (p.result === "warn" && p.reason_code);
    const card = el("details", { class: `check check-${p.result}`, open: isProblem ? "" : null });
    const summary = el("summary", { class: "check-head" },
      badge(RESULT[p.result] || RESULT.skipped),
      el("span", { class: "check-label", text: p.label || p.check }),
      p.reason_code ? el("span", { class: `badge ${SEVERITY[p.severity] || "b-neutral"} code`, text: p.reason_code }) : null,
      p.reused_from_version ? el("span", { class: "chip reused", text: `reused from v${p.reused_from_version}` }) : null,
      p.evidence && p.evidence.llm_model ? el("span", { class: "chip llm", text: `LLM · ${p.evidence.llm_latency_ms} ms · schema-validated ✓` }) : null,
    );
    card.append(summary);
    card.append(el("p", { class: "check-expl", text: p.explanation }));
    const meta = el("p", { class: "check-meta" }, `check: ${p.check}`, p.required ? " · required for approval" : " · advisory",
      p.blocked_by ? ` · blocked by ${p.blocked_by}` : "");
    card.append(meta);
    const ev = evidenceTable(p.evidence);
    if (ev) card.append(el("div", { class: "check-ev" }, el("h3", { text: "Evidence" }), ev));
    body.append(card);
    if (isProblem) toggle(s, true);
  }

  function renderInfo(stageKey, p) {
    const s = stageEl(stageKey);
    if (!s || p.stages) return;
    const body = s.querySelector(".step-body");
    const note = el("div", { class: "info-note" }, el("p", { text: p.message }));
    if (p.items && p.items.length) note.append(el("ul", {}, ...p.items.map((t) => el("li", { text: t }))));
    const links = el("p", { class: "info-links" });
    if (p.email_id) links.append(el("a", { href: `/emails/${p.email_id}`, text: "View email" }));
    if (p.delivery && p.delivery.status) links.append(el("span", { class: "chip", text: `delivery: ${p.delivery.status}${p.delivery.provider ? " via " + p.delivery.provider : ""}` }));
    if (p.case_id) links.append(el("a", { href: `/review/${p.case_id}`, text: `Open review case #${p.case_id}` }));
    if (p.resubmit_path) links.append(el("a", { href: p.resubmit_path, text: "Open vendor portal" }));
    if (links.childNodes.length) note.append(links);
    body.append(note);
    if (p.decision) renderDecision(p.decision, p.flags);
  }

  function renderDecision(d, flags) {
    const box = document.getElementById("decision-body");
    box.replaceChildren();
    box.append(el("div", { class: "decision-badges" }, badge(STATUS[d.status]), badge(ROUTE[d.route])));
    if (d.triggered.length) {
      const list = el("ul", { class: "codes" });
      for (const t of d.triggered) {
        list.append(el("li", {}, el("span", { class: `badge ${SEVERITY[t.severity]} code`, text: t.code }),
          el("span", { class: "muted small", text: ` ${t.severity}${t.risk_points ? " +" + t.risk_points : ""} · ${t.check}` })));
      }
      box.append(el("h3", { text: "Triggered reason codes" }), list);
    } else {
      box.append(el("p", { class: "muted", text: "No reason codes triggered." }));
    }
    box.append(el("h3", { text: "Why this outcome" }), el("p", { text: d.precedence_explanation }));
    box.append(el("h3", { text: "Rationale (templated)" }), el("p", { class: "rationale", text: d.rationale_text }));
    box.append(el("p", { class: "small muted", text: `Risk points ${d.risk_points}${flags ? ` / threshold ${flags.risk_threshold}` : ""} · decided by the rules engine, no LLM` }));
  }

  function handle(ev) {
    if (ev.seq <= lastSeq) return;
    lastSeq = ev.seq;
    const p = ev.payload || {};
    if (ev.kind === "stage_started") setStage(ev.stage, "running", "Running…");
    else if (ev.kind === "check_result") renderCheck(ev.stage, p);
    else if (ev.kind === "info") renderInfo(ev.stage, p);
    else if (ev.kind === "error") {
      const s = stageEl(ev.stage);
      if (s) s.querySelector(".step-body").append(el("div", { class: "notice bad", role: "alert", text: `${p.message}${p.error ? " (" + p.error + ")" : ""}` }));
    } else if (ev.kind === "stage_finished") {
      const s = stageEl(ev.stage);
      const c = p.counts || {};
      const label = { done: "Done", warn: "Done with warnings", failed: "Issues found" }[p.state] || "Done";
      setStage(ev.stage, p.state, label);
      if (s) {
        s.querySelector(".step-time").textContent = `${(p.duration_ms / 1000).toFixed(1)} s`;
        const bits = [];
        if (c.pass) bits.push(`${c.pass} passed`);
        if (c.fail) bits.push(`${c.fail} failed`);
        if (c.warn) bits.push(`${c.warn} warning${c.warn > 1 ? "s" : ""}`);
        if (c.skipped) bits.push(`${c.skipped} skipped`);
        if (p.reused) bits.push(`${p.reused} reused`);
        if (bits.length) s.querySelector(".step-summary").textContent = bits.join(" · ");
      }
    } else if (ev.kind === "run_finished") {
      finished = true;
      const wait = document.getElementById("decision-wait");
      if (wait) wait.textContent = "Run finished.";
      setConn("Run finished.");
    }
  }

  function setConn(text) { document.getElementById("conn-status").textContent = text; }

  async function poll() {
    setConn("Live updates via polling.");
    while (!finished) {
      try {
        const res = await fetch(`/api/runs/${runId}/events.json?after=${lastSeq}`);
        const data = await res.json();
        data.events.forEach(handle);
        if (data.finished && !data.events.length) finished = true;
      } catch (e) { setConn("Connection lost, retrying…"); }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  function connect() {
    if (!window.EventSource) return poll();
    const es = new EventSource(`/api/runs/${runId}/events?after=${lastSeq}`);
    let errors = 0;
    setConn(root.dataset.finished === "true" ? "Replaying the recorded run." : "Live.");
    const kinds = ["stage_started", "check_result", "stage_finished", "info", "error", "run_finished"];
    kinds.forEach((k) => es.addEventListener(k, (m) => { errors = 0; handle(JSON.parse(m.data)); }));
    es.addEventListener("end", () => { es.close(); finished = true; });
    es.onerror = () => {
      if (finished) return es.close();
      errors += 1;
      setConn("Reconnecting…");
      if (errors >= 3) { es.close(); poll(); }  // browser auto-reconnects with Last-Event-ID; fall back after repeated failures
    };
  }
  connect();
})();
