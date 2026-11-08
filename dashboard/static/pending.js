/* Shared Cortech pending state. This only describes the requested page action
   or the explicitly supplied panel operation; it never polls or guesses job
   health. */
(function () {
  "use strict";
  var DELAY = 150, FAILSAFE = 30000, KEY = "cortech.pending";
  var d = document, root = d.documentElement;
  var me = d.currentScript;
  var base = me && me.src ? me.src.replace(/pending\.js(\?.*)?$/, "") : "/static/";
  var LOGO = base + "brand/cortech-logo.png";
  var FILE = /\/(file|download|export)(\/|$)/;

  var timer = null, failTimer = null, el = null;

  function store(set) {
    try { if (set) sessionStorage.setItem(KEY, String(Date.now())); else sessionStorage.removeItem(KEY); } catch (e) {}
  }

  function build(panel, label) {
    var o = d.createElement("div");
    o.className = "pend" + (panel ? " pend--panel" : "");
    o.setAttribute("role", "status");
    o.setAttribute("aria-live", "polite");
    o.setAttribute("aria-atomic", "true");
    var card = d.createElement("div"); card.className = "pend__card";
    var mark = d.createElement("span"); mark.className = "pend__mark"; mark.setAttribute("aria-hidden", "true");
    var halo = d.createElement("span"); halo.className = "pend__halo";
    var img = d.createElement("img"); img.className = "pend__logo"; img.src = LOGO; img.alt = "";
    img.width = 132; img.height = 49; img.decoding = "async";
    mark.appendChild(halo); mark.appendChild(img);
    var text = d.createElement("span"); text.className = "pend__label";
    text.textContent = label || "Opening page";
    card.appendChild(mark); card.appendChild(text); o.appendChild(card);
    return o;
  }

  function mount(panel, handoff, label) {
    if (el) return;
    el = build(panel, label);
    if (panel) {
      el._panel = panel;
      el._busyTarget = panel;
      el._previousBusy = panel.getAttribute("aria-busy");
      panel.classList.add("pend-host"); panel.setAttribute("aria-busy", "true");
    } else if (d.body) {
      el._busyTarget = d.body;
      el._previousBusy = d.body.getAttribute("aria-busy");
      d.body.setAttribute("aria-busy", "true");
    }
    (panel || d.body || root).appendChild(el);
    if (handoff) { el.classList.add("pend--instant", "is-on"); return; }
    void el.offsetWidth;
    el.classList.add("is-on");
    if (!panel) store(true);
  }

  function unmount(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    if (node._busyTarget) {
      if (node._previousBusy === null) node._busyTarget.removeAttribute("aria-busy");
      else node._busyTarget.setAttribute("aria-busy", node._previousBusy);
    }
    if (node._panel) node._panel.classList.remove("pend-host");
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
    setTimeout(function () { unmount(node); }, 240);
  }

  function show(options) {
    var o = options || {};
    if (timer || el) return;
    clearTimeout(failTimer);
    failTimer = setTimeout(function () { hide(true); }, FAILSAFE);
    if (o.now) mount(o.scope || null, false, o.label);
    else timer = setTimeout(function () { timer = null; mount(o.scope || null, false, o.label); }, DELAY);
  }

  function go(url, label) { show({ now: true, label: label || "Opening page" }); window.location.assign(url); }

  // Navigation handoff is only a visual bridge. It does not claim that a job
  // has started or that a worker is healthy.
  function arrive() {
    var t = 0;
    try { t = +sessionStorage.getItem(KEY) || 0; sessionStorage.removeItem(KEY); } catch (e) {}
    if (!t || Date.now() - t > FAILSAFE) return;
    mount(null, true, "Opening page");
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
    setTimeout(function () { if (!e.defaultPrevented) show({ label: "Opening page" }); }, 0);
  }, false);

  d.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f || f.tagName !== "FORM" || f.hasAttribute("data-no-pending") || (f.method || "").toLowerCase() === "dialog") return;
    if (f.target && f.target !== "_self") return;
    if (f.getAttribute("role") === "search" || f.querySelector('input[type="search"]')) return;
    var label = f.getAttribute("data-pending-label") || "Submitting request";
    setTimeout(function () { if (!e.defaultPrevented) show({ label: label }); }, 0);
  }, false);

  window.addEventListener("pageshow", function (e) { if (e.persisted) hide(true); });
  window.addEventListener("error", function () { hide(true); });
  d.addEventListener("keydown", function (e) { if (e.key === "Escape" && el) hide(true); });

  window.CortechPending = { show: show, hide: hide, go: go };
  arrive();
})();
