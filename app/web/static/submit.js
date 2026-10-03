// Drag-and-drop file slots (any page) and "Load sample scenario" (submit page).
(function () {
  "use strict";

  function showFile(zone) {
    const input = zone.querySelector("input[type=file]");
    const label = zone.querySelector(".dz-file");
    const f = input.files && input.files[0];
    label.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : label.dataset.empty;
    zone.classList.toggle("has-file", !!f);
  }
  window.voShowFile = showFile;

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

  window.voAttach = async function (sampleId, fileName, input) {
    const dt = new DataTransfer();
    if (fileName) {
      const blob = await (await fetch(`/samples/${encodeURIComponent(sampleId)}/${encodeURIComponent(fileName)}`)).blob();
      dt.items.add(new File([blob], fileName.replace("_v2", ""), { type: blob.type || "application/pdf" }));
    }
    input.files = dt.files;
    showFile(input.closest(".dropzone"));
  };

  const form = document.getElementById("submit-form");
  const select = document.getElementById("sample");
  if (!form || !select) return;
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
        const fileName = sample.files.find((f) => f.replace(/\.[^.]+$/, "") === zone.dataset.slot);
        await window.voAttach(id, fileName, zone.querySelector("input[type=file]"));
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
