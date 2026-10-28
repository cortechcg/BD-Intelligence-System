/* Knowledge Library: the add panel. Everything here is an enhancement. Without
   it the form posts to /library/upload and the page redirects back. */
(function () {
  "use strict";
  var MAX_FILES = 10, MAX_BYTES = 25 * 1024 * 1024;
  var files = [];

  function $(sel, root) { return (root || document).querySelector(sel); }
  function fmt(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MiB" : Math.max(1, Math.round(n / 1024)) + " KiB"; }
  function extOk(name) { return /\.(pdf|docx)$/i.test(name); }
  function dirty() { window.__cortechFormDirty = true; }

  function panel() { return $("[data-lib-panel]"); }
  function form() { return $("[data-lib-form]"); }

  function openPanel(kind) {
    var p = panel(); if (!p) return;
    p.classList.add("is-open");
    document.querySelectorAll("[data-lib-open]").forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
    var close = $("[data-lib-close]", p); if (close) close.hidden = false;
    if (kind) { var r = $('input[name="kind"][value="' + kind + '"]', p); if (r) { r.checked = true; syncKind(); } }
    dirty();
    p.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    var first = $('input[name="kind"]:checked', p); if (first) first.focus({ preventScroll: true });
  }
  function closePanel() {
    var p = panel(); if (!p) return;
    p.classList.remove("is-open");
    document.querySelectorAll("[data-lib-open]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    var b = $("[data-lib-open]"); if (b) b.focus();
  }
  function syncKind() {
    var f = form(); if (!f) return;
    var k = $('input[name="kind"]:checked', f);
    f.setAttribute("data-kind", k ? k.value : "proposal");
  }

  function renderFiles() {
    var list = $("[data-lib-files]"); if (!list) return;
    list.innerHTML = "";
    files.forEach(function (f, i) {
      var li = document.createElement("li");
      var n = document.createElement("span"); n.className = "n"; n.textContent = f.name;
      var s = document.createElement("span"); s.className = "s"; s.textContent = fmt(f.size);
      li.appendChild(n); li.appendChild(s);
      var problem = !extOk(f.name) ? "Not a PDF or DOCX" : f.size > MAX_BYTES ? "Over 25 MiB" : f.size === 0 ? "Empty file" : "";
      if (problem) { var b = document.createElement("span"); b.className = "bad"; b.textContent = problem; li.appendChild(b); }
      var rm = document.createElement("button"); rm.type = "button"; rm.textContent = "Remove";
      rm.setAttribute("aria-label", "Remove " + f.name);
      rm.addEventListener("click", function () { files.splice(i, 1); syncInput(); renderFiles(); });
      li.appendChild(rm);
      list.appendChild(li);
    });
  }
  function syncInput() {
    var input = $("#lib-files"); if (!input) return;
    try {
      var dt = new DataTransfer(); files.forEach(function (f) { dt.items.add(f); }); input.files = dt.files;
    } catch (e) { /* older browsers keep the native selection */ }
  }
  function addFiles(list) {
    Array.prototype.forEach.call(list, function (f) {
      if (files.length < MAX_FILES && !files.some(function (x) { return x.name === f.name && x.size === f.size; })) files.push(f);
    });
    if (list.length && files.length >= MAX_FILES && files.length < list.length) setMsg("Only the first 10 files are kept.", "");
    syncInput(); renderFiles(); dirty();
  }

  function setMsg(text, state, pct) {
    var box = $("[data-lib-status]"); if (!box) return;
    box.hidden = false;
    if (state) box.setAttribute("data-state", state); else box.removeAttribute("data-state");
    $("[data-lib-msg]", box).textContent = text;
    if (typeof pct === "number") {
      $("[data-lib-bar]", box).style.width = pct + "%";
      $(".lib-bar", box).setAttribute("aria-valuenow", String(Math.round(pct)));
    }
  }
  function showResults(results) {
    var ul = $("[data-lib-results]"); ul.innerHTML = "";
    (results || []).forEach(function (r) {
      var li = document.createElement("li");
      var tag = document.createElement("span"); tag.className = r.ok ? "ok" : "no";
      tag.textContent = r.duplicate ? "Skipped" : r.ok ? "Added" : "Not added";
      var t = document.createElement("span");
      t.textContent = (r.filename || r.title || "File") + (r.duplicate ? ": already in the library" : r.ok ? "" : ": " + (r.reason || "could not be added"));
      li.appendChild(tag); li.appendChild(t); ul.appendChild(li);
    });
  }

  function unreadable(list) {
    return Promise.all(list.map(function (x) {
      if (!x.slice || !window.Response) return Promise.resolve(null);
      return new Response(x.slice(0, 1)).arrayBuffer().then(function () { return null; }, function () { return x.name; });
    })).then(function (names) { return names.filter(Boolean); });
  }

  function submit(ev) {
    var f = form(); if (!f || !window.FormData || !window.XMLHttpRequest) return;
    ev.preventDefault();
    var bad = files.filter(function (x) { return !extOk(x.name) || x.size > MAX_BYTES || x.size === 0; });
    if (!files.length) { setMsg("Choose a PDF or DOCX file.", "error", 0); return; }
    if (bad.length) { setMsg("Remove the files marked above, then try again.", "error", 0); return; }
    var btn = $("[data-lib-submit]", f); btn.disabled = true;
    var host = f.closest(".lib-panel") || f;
    var P = window.CortechPending;
    function done() { btn.disabled = false; if (P) P.hide(true); }
    if (P) P.show({ scope: host });
    var results = $("[data-lib-results]"); if (results) results.innerHTML = "";
    var box = $("[data-lib-status]"); if (box) box.hidden = true;  // no stale message from the last try
    var data = new FormData(f);
    data.delete("files");
    files.forEach(function (x) { data.append("files", x, x.name); });
    var xhr = new XMLHttpRequest();
    xhr.open("POST", f.getAttribute("action"));
    xhr.setRequestHeader("Accept", "application/json");
    xhr.onerror = function () {
      done();
      // The browser refuses to send a file that changed on disk after it was
      // chosen (net::ERR_UPLOAD_FILE_CHANGED). Say so instead of blaming the network.
      unreadable(files).then(function (changed) {
        if (changed.length) {
          setMsg("Not sent: " + changed.join(", ") + " changed on disk after you chose it. Remove it, choose it again, then add.", "error", 0);
        } else {
          setMsg("The browser could not reach the server, so nothing was added. Check the connection and add again.", "error", 0);
        }
      });
    };
    xhr.ontimeout = function () { done(); setMsg("The upload took too long and was stopped. Nothing was added. Try fewer or smaller files.", "error", 0); };
    xhr.onload = function () {
      done();
      var body = null; try { body = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 401 || xhr.status === 403) { setMsg("Your session has ended. Sign in again, then retry.", "error"); return; }
      if (!body) { setMsg("The server sent an unexpected reply. Nothing was added.", "error"); return; }
      showResults(body.results || []);
      if (xhr.status >= 400 || !body.ok) {
        var first = (body.results || []).filter(function (r) { return !r.ok && r.reason; })[0];
        setMsg(body.reason || (first ? "Not added: " + first.reason : "Nothing was added (HTTP " + xhr.status + ")."), "error", 100); return;
      }
      var ok = (body.results || []).filter(function (r) { return r.ok; }).length;
      var dup = (body.results || []).filter(function (r) { return r.duplicate; }).length;
      var total = (body.results || []).length;
      var added = ok - dup;
      var note = dup ? " " + dup + " already in the library, skipped." : "";
      setMsg(ok === total ? (added ? "Added " + added + (added === 1 ? " file" : " files") + ". The agent is reading it now." : "Nothing new to add.") + note :
        "Added " + added + " of " + total + "." + note + " See the notes below.", ok === total ? "ok" : "error", 100);
      files = []; syncInput(); renderFiles(); window.__cortechFormDirty = false;
      setTimeout(function () {
        if (ok !== total) return;
        if (P) P.go("/library?tab=processing"); else location.assign("/library?tab=processing");
      }, 1600);
    };
    xhr.send(data);
  }

  function loadConsultants() {
    var dl = $("#lib-consultants"); if (!dl || dl.getAttribute("data-loaded")) return;
    dl.setAttribute("data-loaded", "1");
    fetch("/api/consultants", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.consultants) return;
        d.consultants.forEach(function (c) {
          var o = document.createElement("option"); o.value = c.id; o.label = c.full_name + (c.role_title ? " (" + c.role_title + ")" : "");
          dl.appendChild(o);
        });
      }).catch(function () {});
  }

  function bind() {
    var f = form(); if (!f || f.getAttribute("data-bound")) return;
    f.setAttribute("data-bound", "1");
    document.documentElement.classList.add("js");
    syncKind();
    f.addEventListener("change", function (e) {
      if (e.target.name === "kind") { syncKind(); }
      if (e.target.name === "files") { files = []; addFiles(e.target.files); }
      if (e.target.name === "whose" && e.target.value === "existing") loadConsultants();
    });
    f.addEventListener("submit", submit);
    var drop = $("[data-lib-drop]");
    ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("is-over"); }); });
    ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("is-over"); }); });
    drop.addEventListener("drop", function (e) { if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files); });
    var close = $("[data-lib-close]"); if (close) close.addEventListener("click", closePanel);
    if (location.hash === "#lib-add" || /[?&]add=1/.test(location.search)) openPanel();
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-lib-open]");
    if (t) { e.preventDefault(); openPanel(t.getAttribute("data-lib-kind")); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && panel() && panel().classList.contains("is-open")) closePanel();
  });
  document.addEventListener("cortech:page-swapped", bind);
  bind();
})();
