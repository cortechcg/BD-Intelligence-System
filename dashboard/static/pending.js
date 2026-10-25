/* Pending state. Shows one white plate with the circling Cortech logo while a
   user action is waiting, then hands over to the next page. Progressive: with no
   script the browser's own loading is used. Never touches search inputs.

   Rules: shows only after 150 ms so quick actions never flash; hides itself on
   bfcache return, on a script error, on Escape, and after 30 s at the latest. */
(function () {
  "use strict";
  var DELAY = 150, LOOP = 500, FAILSAFE = 30000, KEY = "cortech.pending";
  var d = document, root = d.documentElement;
  var me = d.currentScript;
  var base = me && me.src ? me.src.replace(/pending\.js(\?.*)?$/, "") : "/static/";
  var LOGO = base + "brand/cortech-logo.png";
  var FILE = /\/(file|download|export)(\/|$)/;

  var timer = null, failTimer = null, el = null;

  function store(set) {
    try { if (set) sessionStorage.setItem(KEY, String(Date.now())); else sessionStorage.removeItem(KEY); } catch (e) {}
  }

  function build(panel) {
    var o = d.createElement("div");
    o.className = "pend" + (panel ? " pend--panel" : "");
    o.setAttribute("role", "status");
    o.setAttribute("aria-live", "polite");
    o.style.setProperty("--pend-phase", "-" + (Date.now() % LOOP) + "ms");
    var sr = d.createElement("span"); sr.className = "pend__sr";
    var arm = d.createElement("span"); arm.className = "pend__arm"; arm.setAttribute("aria-hidden", "true");
    var img = d.createElement("img"); img.className = "pend__logo"; img.src = LOGO; img.alt = "";
    img.width = 132; img.height = 49; img.decoding = "async";
    arm.appendChild(img); o.appendChild(sr); o.appendChild(arm);
    setTimeout(function () { sr.textContent = "Loading"; }, 60);
    return o;
  }

  function mount(panel, handoff) {
    if (el) return;
    el = build(panel);
    if (panel) { panel.classList.add("pend-host"); panel.setAttribute("aria-busy", "true"); }
    (panel || d.body || root).appendChild(el);
    el._panel = panel || null;
    if (handoff) { el.classList.add("pend--instant", "is-on"); return; }
    void el.offsetWidth;
    el.classList.add("is-on");
    if (!panel) store(true);
  }

  function unmount(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    if (node._panel) { node._panel.classList.remove("pend-host"); node._panel.removeAttribute("aria-busy"); }
  }

  function hide(instant) {
    clearTimeout(timer); timer = null;
    clearTimeout(failTimer); failTimer = null;
    var node = el; el = null;
    if (!node) { store(false); return; }
    store(false);
    if (instant) { unmount(node); return; }
    node.classList.remove("pend--instant");
    node.classList.add("is-off"); node.classList.remove("is-on");
    setTimeout(function () { unmount(node); }, 280);
  }

  function show(o) {
    o = o || {};
    if (timer || el) return;
    clearTimeout(failTimer);
    failTimer = setTimeout(function () { hide(true); }, FAILSAFE);
    if (o.now) mount(o.scope || null, false);
    else timer = setTimeout(function () { timer = null; mount(o.scope || null, false); }, DELAY);
  }

  function go(url) { show({ now: true }); window.location.assign(url); }

  // Arrival: if the page we left was showing the plate, keep it up until this page
  // has painted, then fade it out so the user lands where they clicked.
  function arrive() {
    var t = 0;
    try { t = +sessionStorage.getItem(KEY) || 0; sessionStorage.removeItem(KEY); } catch (e) {}
    if (!t || Date.now() - t > FAILSAFE) return;
    mount(null, true);
    var node = el, done = false;
    function out() {
      if (done) return; done = true;
      if (el === node) hide(false);
    }
    function ready() { requestAnimationFrame(function () { requestAnimationFrame(out); }); }
    if (d.readyState === "loading") d.addEventListener("DOMContentLoaded", ready); else ready();
    setTimeout(out, 6000);
  }

  d.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || a.hasAttribute("download") || a.hasAttribute("data-no-pending")) return;
    var t = a.getAttribute("target"); if (t && t !== "_self") return;
    var h = a.getAttribute("href");
    if (!h || h.charAt(0) === "#" || /^(mailto:|tel:|javascript:)/i.test(h)) return;
    var u; try { u = new URL(a.href, location.href); } catch (x) { return; }
    if (u.origin !== location.origin) return;
    if (u.pathname === location.pathname && u.search === location.search && u.hash) return;
    if (FILE.test(u.pathname)) return;
    // Page handlers (tabs, in-place filters) may cancel the click after this runs.
    setTimeout(function () { if (!e.defaultPrevented) show(); }, 0);
  }, false);

  d.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f || f.tagName !== "FORM" || f.hasAttribute("data-no-pending") || (f.method || "").toLowerCase() === "dialog") return;
    if (f.target && f.target !== "_self") return;
    if (f.getAttribute("role") === "search" || f.querySelector('input[type="search"]')) return; // filters stay silent
    setTimeout(function () { if (!e.defaultPrevented) show(); }, 0);
  }, false);

  window.addEventListener("pageshow", function (e) { if (e.persisted) hide(true); });
  window.addEventListener("error", function () { hide(true); });
  d.addEventListener("keydown", function (e) { if (e.key === "Escape" && el) hide(true); });

  window.CortechPending = { show: show, hide: hide, go: go };
  arrive();
})();
