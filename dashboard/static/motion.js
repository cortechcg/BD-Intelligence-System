/* v3 motion. Progressive: every page is complete without this file. It splits
   the page headline into words, counts whole-number figures up, and fades
   sections in as they scroll into view. Reduced motion skips all of it. */
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var canIO = "IntersectionObserver" in window;

  var HEAD = ".view-head h1, .tr-hero h1, .auth h1, .signin-card h1, [data-split]";
  var HEAD_REST = ".view-head__main > :not(h1), .view-head__aside, .tr-hero__main > :not(h1), .tr-hero__aside";
  var GROUP = ".figures, .tr-figrow, .tiles, .stageboard, .tr-themes, .tr-tl, .tr-lines";
  var BLOCK = ".panel, .section, .tr-section, .tr-roll, .tr-sectors, .tr-index, .table-wrap, .lib-panel.is-open, .lib-card, .lib-empty, .band, .cover, .tabs, .tr-jump, .auth__card, .signin-card";
  var COUNT = ".figure__value, .tile__value, .tr-fig__value, .hero__value, .stageboard__n, .stat__value, .cover__value, .cover__side-value";

  function split(h) {
    if (h.classList.contains("is-split")) return;
    var n = 0;
    (function walk(node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (c) {
        if (c.nodeType === 3) {
          var parts = c.nodeValue.split(/(\s+)/), frag = document.createDocumentFragment();
          parts.forEach(function (p) {
            if (!p) return;
            if (/^\s+$/.test(p)) { frag.appendChild(document.createTextNode(p)); return; }
            var s = document.createElement("span"); s.className = "w"; s.style.setProperty("--i", n++); s.textContent = p;
            frag.appendChild(s);
          });
          c.parentNode.replaceChild(frag, c);
        } else if (c.nodeType === 1) { walk(c); }
      });
    })(h);
    h.classList.add("is-split");
    return n;
  }

  function countUp(el) {
    var txt = el.textContent.trim();
    if (!/^\d{1,3}(,\d{3})*$|^\d+$/.test(txt) || el.children.length || el.classList.contains("is-missing") || el.hasAttribute("data-static")) return;
    var end = parseInt(txt.replace(/,/g, ""), 10);
    if (!end || end > 1e7) return;
    var comma = txt.indexOf(",") !== -1, t0 = null, dur = Math.min(1100, 500 + end * 6);
    el.setAttribute("aria-label", txt);
    function fmt(v) { return comma ? v.toLocaleString("en-US") : String(v); }
    function step(t) {
      if (t0 === null) t0 = t;
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(end * e));
      if (p < 1) requestAnimationFrame(step); else el.textContent = txt;
    }
    el.textContent = fmt(0);
    requestAnimationFrame(step);
  }

  function prep(scope, animate) {
    var heads = scope.querySelectorAll(HEAD), words = 0;
    heads.forEach(function (h) { if (animate) words = Math.max(words, split(h) || 0); else h.classList.add("is-split"); });
    if (!animate || reduce || !canIO) return;

    var base = words ? Math.min(900, 80 + words * 55) : 0, targets = [];
    var restN = 0;
    scope.querySelectorAll(HEAD_REST).forEach(function (el) {
      el.style.setProperty("--d", (base * 0.6 + restN++ * 90) + "ms"); targets.push(el);
    });
    var seen = new Set();
    scope.querySelectorAll(GROUP).forEach(function (g) {
      Array.prototype.forEach.call(g.children, function (c, i) { c.style.setProperty("--d", i * 80 + "ms"); targets.push(c); seen.add(c); });
      seen.add(g);
    });
    scope.querySelectorAll(BLOCK).forEach(function (el) {
      if (seen.has(el) || el.closest(GROUP) || (el.parentElement && el.parentElement.closest(BLOCK))) return;
      targets.push(el);
    });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        en.target.classList.add("rv-in");
        en.target.querySelectorAll(COUNT).forEach(countUp);
        if (en.target.matches(COUNT)) countUp(en.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    targets.forEach(function (el) {
      if (el.classList.contains("rv")) return;
      el.classList.add("rv"); io.observe(el);
    });
    /* Safety net: nothing stays hidden if observation never fires. */
    setTimeout(function () {
      document.querySelectorAll(".rv:not(.rv-in)").forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (r.top < innerHeight) el.classList.add("rv-in");
      });
    }, 2500);
  }

  var mast = document.querySelector(".masthead");
  if (mast) {
    var onScroll = function () { mast.classList.toggle("is-stuck", window.scrollY > 4); };
    addEventListener("scroll", onScroll, { passive: true }); onScroll();
  }

  prep(document, !reduce);
  /* Background refreshes swap the page body: show the new content as it is. */
  document.addEventListener("cortech:page-swapped", function () { prep(document, false); });
})();
