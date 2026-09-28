// DocFlow AI - small progressive enhancements. The app works without JS.
(function () {
  "use strict";

  // ---------- Upload: file label + drag styling
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector("[data-file-input]");
    var label = zone.querySelector("[data-file-label]");
    input.addEventListener("change", function () {
      if (input.files.length) { label.textContent = input.files[0].name; zone.classList.add("has-file"); }
    });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function () { zone.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function () { zone.classList.remove("drag"); });
    });
  });

  // ---------- Loading overlay while a document is analyzed
  var busy = document.querySelector("[data-busy]");
  document.querySelectorAll("[data-busy-form]").forEach(function (form) {
    form.addEventListener("submit", function () { if (busy) busy.hidden = false; });
  });
  window.addEventListener("pageshow", function () { if (busy) busy.hidden = true; });

  // ---------- Clickable table rows
  document.querySelectorAll("tr[data-href]").forEach(function (tr) {
    tr.addEventListener("click", function (e) {
      if (e.target.tagName !== "A") window.location = tr.dataset.href;
    });
  });

  // ---------- Human-verification progress bar (set via JS: CSP forbids inline styles)
  document.querySelectorAll("[data-bar]").forEach(function (bar) {
    var total = +bar.dataset.total || 0;
    bar.querySelector(".bar-ok").style.width = (total ? 100 * bar.dataset.ok / total : 0) + "%";
    bar.querySelector(".bar-no").style.width = (total ? 100 * bar.dataset.no / total : 0) + "%";
  });

  // ---------- Evidence marks in the original text
  // Evidence is stored as exact character offsets into the document, so marking it
  // is a lookup, not a fuzzy search: what's highlighted is exactly what was stored.
  var docEl = document.querySelector("[data-doc-text]");
  var rows = Array.prototype.slice.call(document.querySelectorAll("[data-field-row]"));
  // Array.from splits by code point, matching Python's string offsets (emoji-safe).
  var original = docEl ? Array.from(docEl.textContent) : [];

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderMarks(activeIdx) {
    if (!docEl) return;
    var n = original.length, owner = new Array(n);
    var rank = { inferred: 1, explicit: 2 };
    rows.forEach(function (row, i) {
      if (row.dataset.start === undefined) return;
      var s = +row.dataset.start, e = Math.min(+row.dataset.end, n);
      for (var k = s; k < e; k++) {
        var cur = owner[k];
        if (cur === undefined || i === activeIdx ||
            (cur !== activeIdx && rank[row.dataset.source] > rank[rows[cur].dataset.source])) owner[k] = i;
      }
    });
    var html = "", k = 0;
    while (k < n) {
      var o = owner[k], j = k;
      while (j < n && owner[j] === o) j++;
      var chunk = escapeHtml(original.slice(k, j).join(""));
      if (o === undefined) html += chunk;
      else html += '<mark class="m-' + rows[o].dataset.source + (o === activeIdx ? " m-active" : "") +
                   '" data-row="' + o + '"' + (o === activeIdx ? ' id="hl"' : "") + ">" + chunk + "</mark>";
      k = j;
    }
    docEl.innerHTML = html;
  }
  renderMarks(-1);

  function activate(idx, scrollDoc) {
    rows.forEach(function (r) { r.classList.remove("is-active"); });
    rows[idx].classList.add("is-active");
    renderMarks(idx);
    var mark = document.getElementById("hl");
    if (mark && scrollDoc) {
      docEl.scrollTop = mark.offsetTop - docEl.clientHeight / 3;
      if (window.innerWidth < 960) mark.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  rows.forEach(function (row, i) {
    var btn = row.querySelector("[data-show-evidence]");
    if (btn) btn.addEventListener("click", function () { activate(i, true); });
  });
  // Click a mark in the document -> jump to the field it supports
  if (docEl) docEl.addEventListener("click", function (e) {
    var m = e.target.closest("mark[data-row]");
    if (!m) return;
    var idx = +m.dataset.row;
    activate(idx, false);
    rows[idx].scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // ---------- Filter rows by provenance
  var filters = document.querySelectorAll("[data-filter]");
  filters.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var f = btn.dataset.filter;
      filters.forEach(function (b) { b.classList.toggle("is-on", b === btn); });
      rows.forEach(function (row) {
        var show = f === "all" || (f === "attention" ? row.classList.contains("needs-attention")
                                                     : row.dataset.source === f);
        row.classList.toggle("is-hidden", !show);
      });
    });
  });

  // ---------- Editing a value implies the reviewer vouches for it -> select Approve
  rows.forEach(function (row) {
    var input = row.querySelector(".field-input");
    if (!input) return;
    var approve = row.querySelector('input[value="approve"]');
    input.addEventListener("input", function () {
      var edited = input.value.trim() !== input.dataset.aiValue;
      row.classList.toggle("is-edited", edited);
      if (edited && input.value.trim()) approve.checked = true;
    });
  });

  // ---------- Bulk approve: ONLY explicit + high confidence + not corrected by the system.
  // Inferred, medium/low confidence and corrected fields always need an individual decision.
  var bulk = document.querySelector("[data-approve-safe]");
  if (bulk) bulk.addEventListener("click", function () {
    var n = 0;
    rows.forEach(function (row) {
      var input = row.querySelector(".field-input");
      if (!input || !input.value.trim()) return;
      if (row.dataset.source !== "explicit" || !row.classList.contains("conf-row-high")) return;
      if (row.querySelector(".sys-note") || row.querySelector('input[value="reject"]').checked) return;
      row.querySelector('input[value="approve"]').checked = true;
      n++;
    });
    bulk.textContent = "Approved " + n + " explicit · high";
  });
})();
