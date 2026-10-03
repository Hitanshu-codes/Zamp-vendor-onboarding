// Submit page: drag-and-drop file slots and "Load sample scenario".
(function () {
  "use strict";
  const form = document.getElementById("submit-form");
  if (!form) return;

  function showFile(zone) {
    const input = zone.querySelector("input[type=file]");
    const label = zone.querySelector(".dz-file");
    const f = input.files && input.files[0];
    label.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : label.dataset.empty;
    zone.classList.toggle("has-file", !!f);
  }

  document.querySelectorAll(".dropzone").forEach((zone) => {
    const input = zone.querySelector("input[type=file]");
    input.addEventListener("change", () => showFile(zone));
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("drag")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; showFile(zone); }
    });
  });

  const select = document.getElementById("sample");
  const note = document.getElementById("sample-note");
  select.addEventListener("change", async () => {
    const id = select.value;
    if (!id) return;
    note.textContent = "Loading sample…";
    try {
      const res = await fetch(`/api/samples/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const sample = await res.json();
      form.reset();
      for (const [name, value] of Object.entries(sample.form)) {
        const el = form.elements.namedItem(name);
        if (el) el.value = value;
      }
      for (const zone of document.querySelectorAll(".dropzone")) {
        const slot = zone.dataset.slot;
        const input = zone.querySelector("input[type=file]");
        const fileName = sample.files.find((f) => f.replace(/\.[^.]+$/, "") === slot);
        const dt = new DataTransfer();
        if (fileName) {
          const blob = await (await fetch(`/samples/${encodeURIComponent(id)}/${encodeURIComponent(fileName)}`)).blob();
          dt.items.add(new File([blob], fileName, { type: blob.type || "application/pdf" }));
        }
        input.files = dt.files;
        showFile(zone);
      }
      note.textContent = `${sample.title}. ${sample.notes || ""}`.trim();
    } catch (err) {
      note.textContent = `Could not load the sample (${err.message}).`;
    }
  });

  form.addEventListener("submit", () => {
    const btn = document.getElementById("run-btn");
    btn.disabled = true;
    btn.textContent = "Starting…";
  });
})();
