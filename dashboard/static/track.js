/* Track record: optional enhancement. Without this file every filter, theme
   tile and year link is a plain link or a GET form that the server handles. With
   it, the same controls filter in place and keep the address in sync. */
(function () {
  "use strict";
  var form = document.querySelector("[data-tr-filters]");
  var table = document.querySelector("[data-tr-table]");
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  if (form && table) {
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    var count = document.querySelector("[data-tr-count]");
    var none = document.querySelector("[data-tr-none]");
    var hint = document.querySelector(".tr-tpanels__hint");
    var panels = Array.prototype.slice.call(document.querySelectorAll(".tr-tpanel"));
    var tiles = Array.prototype.slice.call(document.querySelectorAll(".tr-theme"));
    var field = function (k) { return form.querySelector('[data-f="' + k + '"]'); };
    var has = function (cell, v) { return (cell || "").split("|").indexOf(v) !== -1; };

    var syncTheme = function () {
      var slug = field("theme").value;
      panels.forEach(function (p) { p.classList.toggle("is-open", p.id === "tp-" + slug); });
      tiles.forEach(function (t) {
        var on = t.dataset.v === slug;
        t.classList.toggle("is-on", on);
        if (on) t.setAttribute("aria-current", "true"); else t.removeAttribute("aria-current");
      });
      if (hint) hint.hidden = !!slug;
    };

    var apply = function () {
      var q = field("q").value.trim().toLowerCase(), c = field("client").value, a = field("area").value,
          t = field("theme").value, y = field("year").value, w = field("won").checked, n = 0;
      rows.forEach(function (r) {
        var d = r.dataset;
        var ok = (!q || d.q.indexOf(q) !== -1) && (!c || d.client === c) && (!a || has(d.areas, a)) &&
                 (!t || has(d.themes, t)) && (!y || d.year === y) && (!w || d.won === "1");
        r.hidden = !ok;
        if (ok) n += 1;
      });
      count.textContent = n === rows.length ? rows.length + (rows.length === 1 ? " assignment" : " assignments")
                                            : n + " of " + rows.length + (rows.length === 1 ? " assignment" : " assignments");
      none.hidden = n !== 0;
      var params = [];
      [["q", field("q").value.trim()], ["client", c], ["year", y], ["area", a], ["theme", t], ["won", w ? "1" : ""]]
        .forEach(function (kv) { if (kv[1]) params.push(kv[0] + "=" + encodeURIComponent(kv[1])); });
      if (window.history && history.replaceState) {
        history.replaceState(null, "", location.pathname + (params.length ? "?" + params.join("&") : "") + location.hash);
      }
    };

    var toProjects = function () {
      var h = document.getElementById("h-projects"), s = document.getElementById("projects");
      if (s) s.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
      if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
    };

    form.addEventListener("input", function () { apply(); syncTheme(); });
    form.addEventListener("change", function () { apply(); syncTheme(); });
    form.addEventListener("submit", function (e) { e.preventDefault(); apply(); });
    var reset = form.querySelector("[data-reset]");
    if (reset) reset.addEventListener("click", function (e) {
      e.preventDefault();
      form.reset();
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.tagName === "SELECT") el.selectedIndex = 0; else if (el.type === "search") el.value = ""; else if (el.type === "checkbox") el.checked = false;
      });
      apply(); syncTheme();
    });

    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest("a[data-f]");
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      var key = a.dataset.f, val = a.dataset.v, el = field(key);
      if (!el) return;
      e.preventDefault();
      var go = a.hasAttribute("data-go");
      el.value = (key === "theme" && !go && el.value === val) ? "" : val;
      apply(); syncTheme();
      if (key !== "theme" || go) toProjects();
    });
    syncTheme();
  }

  var toggle = document.querySelector("[data-toggle-areas]");
  var list = document.querySelector(".tr-areas");
  if (toggle && list) {
    toggle.hidden = false;
    var total = toggle.textContent.replace(/\D+/g, "");
    toggle.addEventListener("click", function () {
      var open = list.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "Show fewer areas" : "Show all " + total + " areas";
    });
  }
})();
