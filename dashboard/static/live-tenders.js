(function () {
  "use strict";
  var list = document.getElementById("live-list");
  if (!list) return;
  var status = document.getElementById("live-status");
  var count = document.getElementById("live-count");
  var refreshed = document.getElementById("live-last-refresh");
  var refreshPill = document.getElementById("live-refresh-pill");
  var refreshState = document.getElementById("live-refresh-state");
  var empty = document.getElementById("live-empty");
  var retry = document.getElementById("live-retry");
  var seen = new Set(Array.prototype.map.call(list.querySelectorAll("[data-key]"), function (node) { return node.dataset.key; }));
  var MAX_PAGES_PER_TICK = 5;
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
  function updateRefreshTime(value) {
    if (!refreshed || !value) return;
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return;
    refreshed.dateTime = date.toISOString();
    refreshed.textContent = "Last refresh " + date.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    });
  }
  function setStale(stale) {
    if (!refreshPill) return;
    refreshPill.classList.toggle("is-stale", stale);
    if (refreshState) refreshState.textContent = stale ? "Delayed" : "Live";
    refreshPill.title = stale ? "The latest refresh failed; retrying automatically." : "Live tender feed is refreshing.";
  }
  function prepend(item) {
    var li = document.createElement("li");
    li.className = "live-card";
    li.dataset.key = item.key;
    var main = addText(li, "div", "live-card__main", "");
    var title = document.createElement("h3");
    var detail = document.createElement("a");
    detail.href = "/opportunity?url=" + encodeURIComponent(item.url);
    detail.textContent = item.title || "Untitled tender";
    title.appendChild(detail);
    if (item.manual) addText(title, "span", "live-manual-badge", "Manually added");
    main.appendChild(title);
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
    // Cursor pages are oldest-to-newest. Prepending each item keeps the visible
    // list newest-first while preserving URL deduplication.
    list.prepend(li);
  }
  function showError(message) {
    status.textContent = message;
    status.classList.add("is-error");
    retry.hidden = false;
    setStale(true);
  }
  function clearError() {
    status.classList.remove("is-error");
    retry.hidden = true;
    setStale(false);
  }
  async function refresh() {
    if (stopped || document.visibilityState !== "visible" || controller) return;
    controller = new AbortController();
    list.setAttribute("aria-busy", "true");
    try {
      var added = 0, hasMore = false, pages = 0, checkedAt = "";
      var anchor = null, anchorTop = 0, preserveScroll = false;
      do {
        var since = list.dataset.cursor || "";
        var url = "/api/live-tenders" + (since ? "?since=" + encodeURIComponent(since) : "");
        var response = await fetch(url, {credentials: "same-origin", cache: "no-store", signal: controller.signal});
        if (response.status === 401) {
          showError("Your session has expired. Sign in again to resume the live feed.");
          errorDelay = 60000; schedule(errorDelay); return;
        }
        if (!response.ok) throw new Error("feed unavailable");
        var data = await response.json();
        checkedAt = data.checked_at || checkedAt;
        var incoming = Array.isArray(data.items) ? data.items : [];
        incoming.forEach(function (item) {
          if (!item || !item.key || seen.has(item.key)) return;
          if (!anchor) {
            anchor = list.querySelector("[data-key]");
            preserveScroll = window.scrollY > 0;
            if (anchor) anchorTop = anchor.getBoundingClientRect().top;
          }
          seen.add(item.key); prepend(item); added += 1;
        });
        if (data.next_cursor) list.dataset.cursor = data.next_cursor;
        pages += 1;
        hasMore = Boolean(data.has_more && data.next_cursor);
      } while (hasMore && pages < MAX_PAGES_PER_TICK);

      if (checkedAt) updateRefreshTime(checkedAt);
      count.textContent = seen.size + (seen.size === 1 ? " listing" : " listings");
      empty.hidden = seen.size !== 0;
      clearError(); errorDelay = 5000;
      if (added) status.textContent = added + (added === 1 ? " new tender added." : " new tenders added.");
      else status.textContent = seen.size ? "Showing recent stored listings." : "No stored listings yet. The feed will check again shortly.";
      schedule(hasMore ? 0 : 5000);
    } catch (error) {
      if (error && error.name === "AbortError") {
        if (!stopped && document.visibilityState === "visible") schedule(0);
        return;
      }
      showError("Connection problem. The feed will retry automatically.");
      errorDelay = Math.min(60000, Math.max(10000, errorDelay * 2));
      schedule(errorDelay);
    } finally {
      if (preserveScroll && anchor && anchor.isConnected) {
        var shift = anchor.getBoundingClientRect().top - anchorTop;
        if (shift) window.scrollBy(0, shift);
      }
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
  updateRefreshTime(refreshed && refreshed.dateTime);
  if (document.visibilityState === "visible") schedule(5000);
})();
