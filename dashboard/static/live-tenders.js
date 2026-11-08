(function () {
  "use strict";
  var list = document.getElementById("live-list");
  if (!list) return;
  var status = document.getElementById("live-status");
  var count = document.getElementById("live-count");
  var refreshed = document.getElementById("live-last-refresh");
  var empty = document.getElementById("live-empty");
  var retry = document.getElementById("live-retry");
  var seen = new Set(Array.prototype.map.call(list.querySelectorAll("[data-key]"), function (node) { return node.dataset.key; }));
  var timer = null, controller = null, stopped = false, errorDelay = 5000;

  function schedule(delay) {
    clearTimeout(timer);
    if (!stopped && document.visibilityState === "visible") timer = setTimeout(refresh, delay);
  }
  function addText(parent, tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    parent.appendChild(node);
    return node;
  }
  function append(item) {
    var li = document.createElement("li");
    li.className = "live-card";
    li.dataset.key = item.key;
    var main = addText(li, "div", "live-card__main", "");
    var title = document.createElement("h3");
    var detail = document.createElement("a");
    detail.href = "/opportunity?url=" + encodeURIComponent(item.url);
    detail.textContent = item.title || "Untitled tender";
    title.appendChild(detail); main.appendChild(title);
    var source = document.createElement("div"); source.className = "live-card__source";
    source.appendChild(document.createTextNode("Source: "));
    var external = document.createElement("a"); external.href = item.source_url; external.target = "_blank";
    external.rel = "noopener noreferrer"; external.textContent = item.source || "Source page";
    source.appendChild(external); main.appendChild(source);
    var facts = addText(li, "dl", "live-card__facts", "");
    var deadline = addText(facts, "div", "", "");
    addText(deadline, "dt", "", "Deadline"); addText(deadline, "dd", "", item.deadline || "Not recorded");
    var first = addText(facts, "div", "", "");
    addText(first, "dt", "", "First seen");
    var dd = addText(first, "dd", "", "");
    addText(dd, "time", "", item.first_seen_display || (item.first_seen || "Not recorded").slice(0, 10));
    if (item.first_seen_precision === "date") addText(dd, "span", "live-date-precision", " (date)");
    list.appendChild(li);
  }
  function showError(message) {
    status.textContent = message;
    status.classList.add("is-error");
    retry.hidden = false;
  }
  function clearError() {
    status.classList.remove("is-error");
    retry.hidden = true;
  }
  async function refresh() {
    if (stopped || document.visibilityState !== "visible" || controller) return;
    controller = new AbortController();
    list.setAttribute("aria-busy", "true");
    try {
      var since = list.dataset.cursorAt || "";
      var url = "/api/live-tenders" + (since ? "?since=" + encodeURIComponent(since) : "");
      var response = await fetch(url, {credentials: "same-origin", cache: "no-store", signal: controller.signal});
      if (response.status === 401) {
        showError("Your session has expired. Sign in again to resume the live feed.");
        errorDelay = 60000; schedule(errorDelay); return;
      }
      if (!response.ok) throw new Error("feed unavailable");
      var data = await response.json();
      var incoming = Array.isArray(data.items) ? data.items : [];
      incoming.sort(function (a, b) {
        var ta = Date.parse(a.first_seen || "") || 0, tb = Date.parse(b.first_seen || "") || 0;
        return ta - tb || String(a.key || "").localeCompare(String(b.key || ""));
      });
      var added = 0;
      incoming.forEach(function (item) {
        if (!item || !item.key || seen.has(item.key)) return;
        seen.add(item.key); append(item); added += 1;
      });
      if (data.cursor) {
        list.dataset.cursorAt = data.cursor.at || list.dataset.cursorAt || "";
        list.dataset.cursorKey = data.cursor.key || "";
      }
      count.textContent = seen.size + (seen.size === 1 ? " listing" : " listings");
      empty.hidden = seen.size !== 0;
      if (data.checked_at) refreshed.textContent = "Last refresh " + data.checked_at.slice(0, 19).replace("T", " ") + " UTC";
      clearError(); errorDelay = 5000;
      if (added) status.textContent = added + (added === 1 ? " new tender added." : " new tenders added.");
      schedule(5000);
    } catch (error) {
      if (error && error.name === "AbortError") {
        if (!stopped && document.visibilityState === "visible") schedule(0);
        return;
      }
      showError("Connection problem. The feed will retry automatically.");
      errorDelay = Math.min(60000, Math.max(10000, errorDelay * 2));
      schedule(errorDelay);
    } finally {
      list.setAttribute("aria-busy", "false");
      controller = null;
    }
  }
  retry.addEventListener("click", function () { errorDelay = 5000; refresh(); });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      clearTimeout(timer); timer = null;
      if (controller) controller.abort();
    } else { stopped = false; refresh(); }
  });
  window.addEventListener("pagehide", function () {
    stopped = true; clearTimeout(timer); timer = null;
    if (controller) controller.abort();
  }, {once: true});
  if (document.visibilityState === "visible") schedule(5000);
})();
