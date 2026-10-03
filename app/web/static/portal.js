// Vendor portal demo helper: attach a scenario's corrected (v2) files and form changes.
(function () {
  "use strict";
  const btn = document.getElementById("attach-v2");
  const form = document.getElementById("portal-form");
  if (!btn || !form) return;
  btn.addEventListener("click", async () => {
    const id = document.getElementById("v2-sample").value;
    const res = await fetch(`/api/samples/${encodeURIComponent(id.trim())}`);
    if (!res.ok) { btn.textContent = "Unknown sample"; return; }
    const sample = await res.json();
    if (!sample.resubmission) { btn.textContent = "No corrected files"; return; }
    for (const slot of sample.resubmission.replace_docs) {
      const input = form.querySelector(`input[type=file][name="${slot}"]`);
      if (input) await window.voAttach(sample.id, `${slot}_v2.pdf`, input);
    }
    for (const [name, value] of Object.entries(sample.resubmission.form_changes || {})) {
      const el = form.elements.namedItem(name);
      if (el) el.value = value;
    }
    btn.textContent = "Attached ✓";
  });
})();
