(function () {
  "use strict";

  var stageScreens = ["sources", "opportunities", "analysis", "draft", "review"];
  var currentScreen = "opportunities";
  var workflowStage = "opportunities";
  var toastTimer = 0;
  var drawerReturn = null;
  var menuTarget = null;
  var menuTrigger = null;
  var screenTitles = {
    sources: "Source selection", opportunities: "Opportunity list", analysis: "Analysis",
    draft: "Draft setup", review: "Review pack", pipeline: "Pipeline",
    history: "Opportunity history", settings: "Settings", team: "Team and CVs",
    evidence: "Evidence library", signin: "Sign-in screen", "system-error": "Error states"
  };
  var technicalSections = Array.from(document.querySelectorAll(".outline-row")).map(function (row) {
    return { title: row.querySelector(".outline-title").value, pages: parseInt(row.querySelector(".target-length").textContent, 10) || 2 };
  });
  var eoiSections = [
    { title: "Cover letter", pages: 2 }, { title: "Firm profile", pages: 2 },
    { title: "Relevant experience", pages: 3 }, { title: "Proposed approach summary", pages: 2 },
    { title: "Key experts", pages: 2 }, { title: "Annexes", pages: 2 }
  ];

  function showScreen(name) {
    if (!document.querySelector('[data-view="' + name + '"]')) return;
    currentScreen = name;
    if (stageScreens.indexOf(name) >= 0) workflowStage = name;
    document.querySelectorAll("[data-view]").forEach(function (view) {
      var active = view.getAttribute("data-view") === name;
      view.hidden = !active;
      view.classList.toggle("is-visible", active);
    });
    document.querySelectorAll("[data-screen]").forEach(function (node) {
      var key = node.getAttribute("data-screen");
      if (node.classList.contains("stage-tab")) {
        var selected = key === workflowStage;
        node.classList.toggle("is-active", selected);
        node.setAttribute("aria-selected", selected ? "true" : "false");
      }
      if (node.classList.contains("side-link")) {
        node.classList.toggle("is-active", key === name);
        if (key === name) node.setAttribute("aria-current", "page");
        else node.removeAttribute("aria-current");
      }
    });

    var stageIndex = stageScreens.indexOf(name);
    var footer = document.querySelector("[data-stage-footer]");
    document.querySelector(".stage-progress").hidden = stageIndex < 0;
    footer.hidden = stageIndex < 0;
    if (stageIndex >= 0) {
      var progress = document.querySelector(".stage-progress");
      progress.setAttribute("aria-label", "Stage " + (stageIndex + 1) + " of 5");
      progress.querySelectorAll("span").forEach(function (seg, i) {
        seg.classList.toggle("is-done", i < stageIndex);
        seg.classList.toggle("is-current", i === stageIndex);
      });
      var label = document.querySelector("[data-footer-label]");
      label.textContent = stageIndex === 1 ? "4 opportunities · 3 sources selected" : (screenTitles[name] + " · sample workspace");
      var prev = document.querySelector("[data-prev-stage]");
      var next = document.querySelector("[data-next-stage]");
      prev.disabled = stageIndex === 0;
      next.disabled = stageIndex === stageScreens.length - 1;
      var mobileSelect = document.querySelector(".mobile-stage-select");
      if (mobileSelect) mobileSelect.value = workflowStage;
    }
    var mobileSelect = document.querySelector(".mobile-stage-select");
    if (mobileSelect) mobileSelect.value = workflowStage;
    document.querySelector("[data-sidebar]").classList.remove("is-open");
    document.querySelector("[data-menu-toggle]").setAttribute("aria-expanded", "false");
    document.querySelector("[data-menu-close]").classList.remove("is-open");
    document.querySelectorAll("[data-state-panel]").forEach(function (panel) { panel.hidden = true; });
    document.querySelectorAll(".state-controls button").forEach(function (button) { button.setAttribute("aria-pressed", "false"); });
    var analysisCards = document.querySelector(".analysis-grid");
    var analysisRun = document.querySelector(".run-panel");
    if (analysisCards) analysisCards.hidden = false;
    if (analysisRun) analysisRun.hidden = false;
    var runStatus = document.querySelector(".run-panel-head .status");
    if (runStatus) { runStatus.textContent = "Complete"; runStatus.className = "status status--addressed"; }
    document.querySelectorAll(".run-steps li").forEach(function (step) {
      step.className = "is-done";
      step.querySelector("span").textContent = "✓";
    });
    document.querySelector(".progress-log").hidden = false;
    document.querySelectorAll(".team-match-card, .strategy-card").forEach(function (card) { card.hidden = false; });
    document.querySelector("[data-opportunity-cards]").hidden = false;
    document.querySelector(".desktop-opportunities").hidden = false;
    document.querySelector(".source-grid").hidden = false;
    document.querySelector("[data-outline-list]").hidden = false;
    document.querySelector("[data-review-content]").hidden = false;
    if (name === "analysis" && !window.__previewScoreAnimated) animateScore();
    if (name === "draft" && !document.querySelector(".outline-row")) {
      document.querySelector("[data-outline-list]").hidden = true;
      document.querySelector('[data-state-panel="draft-empty"]').hidden = false;
    }
    document.querySelector(".main-scroll").scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  function toast(message) {
    var node = document.querySelector(".toast");
    node.textContent = message;
    node.hidden = false;
    requestAnimationFrame(function () { node.classList.add("is-visible"); });
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      node.classList.remove("is-visible");
      window.setTimeout(function () { node.hidden = true; }, 260);
    }, 2200);
  }

  function openDrawer(id) {
    var drawer = document.getElementById(id);
    if (!drawer) return;
    drawerReturn = document.activeElement;
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    drawer.setAttribute("aria-modal", "true");
    document.querySelector("[data-drawer-backdrop]").classList.add("is-open");
    var first = drawer.querySelector("button, input, a, select");
    if (first) first.focus();
  }

  function closeDrawers() {
    document.querySelectorAll(".drawer.is-open").forEach(function (drawer) {
      drawer.classList.remove("is-open");
      drawer.setAttribute("aria-hidden", "true");
      drawer.removeAttribute("aria-modal");
    });
    document.querySelector("[data-drawer-backdrop]").classList.remove("is-open");
    if (drawerReturn && drawerReturn.focus) drawerReturn.focus();
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest(".opp-overflow-menu, [data-opp-overflow]")) {
      var openMenu = document.querySelector(".opp-overflow-menu");
      if (openMenu) openMenu.hidden = true;
      document.querySelectorAll("[data-opp-overflow]").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
    }
    var screenControl = event.target.closest("[data-screen]");
    if (screenControl) {
      if (screenControl.tagName === "A") event.preventDefault();
      showScreen(screenControl.getAttribute("data-screen"));
      var sectionName = screenControl.getAttribute("data-focus-section");
      if (sectionName) window.setTimeout(function () {
        var targetRow = Array.from(document.querySelectorAll(".outline-row")).find(function (row) {
          return row.querySelector(".outline-title").value.toLowerCase().includes(sectionName.toLowerCase());
        });
        if (targetRow) {
          targetRow.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
          targetRow.querySelector(".outline-title").focus();
        }
      }, 30);
      return;
    }

    var drawerControl = event.target.closest("[data-open-drawer]");
    if (drawerControl) { openDrawer(drawerControl.getAttribute("data-open-drawer")); return; }
    if (event.target.closest("[data-close-drawer]") || event.target.matches("[data-drawer-backdrop]")) { closeDrawers(); return; }

    var oppOverflow = event.target.closest("[data-opp-overflow]");
    if (oppOverflow) {
      var menu = document.querySelector(".opp-overflow-menu");
      var bounds = oppOverflow.getBoundingClientRect();
      menuTarget = oppOverflow.closest("tr, .opportunity-card");
      menuTrigger = oppOverflow;
      menu.style.top = Math.min(window.innerHeight - 160, bounds.bottom + 5) + "px";
      menu.style.left = Math.max(8, Math.min(window.innerWidth - 190, bounds.right - 180)) + "px";
      menu.hidden = false;
      oppOverflow.setAttribute("aria-expanded", "true");
      menu.querySelector("button").focus();
      return;
    }
    var oppMenuAction = event.target.closest("[data-opp-menu-action]");
    if (oppMenuAction) {
      var menuAction = oppMenuAction.getAttribute("data-opp-menu-action");
      document.querySelector(".opp-overflow-menu").hidden = true;
      document.querySelectorAll("[data-opp-overflow]").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
      if (menuAction === "dismiss" && menuTarget) {
        var title = menuTarget.querySelector(".row-title");
        var phrase = title ? title.textContent.trim().toLowerCase() : "";
        document.querySelectorAll(".opportunities-table tbody tr, .opportunity-card").forEach(function (item) {
          if (item.getAttribute("data-search").includes(phrase)) item.hidden = true;
        });
        var nextTrigger = document.querySelector(".opportunities-table tr:not([hidden]) [data-opp-overflow], .opportunity-card:not([hidden]) [data-opp-overflow]");
        if (nextTrigger) nextTrigger.focus();
        toast("Opportunity dismissed in this sample view.");
      } else if (menuAction === "source") {
        if (menuTrigger) menuTrigger.focus();
        toast("Sample source page. No external page was opened.");
      } else {
        if (menuTrigger) menuTrigger.focus();
        toast("Sample TOR in Drive. No external file was opened.");
      }
      return;
    }

    if (event.target.closest("[data-menu-toggle]")) {
      var side = document.querySelector("[data-sidebar]");
      var open = !side.classList.contains("is-open");
      side.classList.toggle("is-open", open);
      document.querySelector("[data-menu-toggle]").setAttribute("aria-expanded", open ? "true" : "false");
      document.querySelector("[data-menu-close]").classList.toggle("is-open", open);
      return;
    }
    if (event.target.matches("[data-menu-close]")) {
      document.querySelector("[data-sidebar]").classList.remove("is-open");
      document.querySelector("[data-menu-toggle]").setAttribute("aria-expanded", "false");
      event.target.classList.remove("is-open");
      return;
    }
    if (event.target.closest("[data-avatar-menu]")) {
      var toggle = document.querySelector("[data-avatar-menu]");
      var pop = document.querySelector("[data-avatar-popover]");
      var opened = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", opened ? "true" : "false");
      pop.hidden = !opened;
      return;
    }
    if (!event.target.closest(".avatar-menu")) {
      document.querySelector("[data-avatar-menu]").setAttribute("aria-expanded", "false");
      document.querySelector("[data-avatar-popover]").hidden = true;
    }

    var stateControl = event.target.closest("[data-state]");
    if (stateControl) {
      var stateKey = stateControl.getAttribute("data-state");
      document.querySelectorAll("[data-state-panel]").forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-state-panel") !== stateKey;
      });
      document.querySelectorAll(".state-controls button").forEach(function (button) {
        button.setAttribute("aria-pressed", button === stateControl ? "true" : "false");
      });
      if (stateKey.indexOf("sources-") === 0) {
        document.querySelector(".source-grid").hidden = stateKey !== "sources-error";
      }
      if (stateKey.indexOf("opportunities-") === 0) {
        var isLoadOrError = stateKey === "opportunities-loading" || stateKey === "opportunities-error";
        document.querySelector("[data-opportunity-cards]").hidden = isLoadOrError;
        document.querySelector(".desktop-opportunities").hidden = isLoadOrError;
      }
      if (stateKey.indexOf("analysis-") === 0) {
        var failedWithoutScore = stateKey === "analysis-failed";
        var running = stateKey === "analysis-running";
        document.querySelector(".analysis-grid").hidden = running || failedWithoutScore;
        document.querySelector(".run-panel").hidden = running || failedWithoutScore;
        if (stateKey === "analysis-partial" || stateKey === "analysis-error") {
          var status = document.querySelector(".run-panel-head .status");
          status.textContent = stateKey === "analysis-partial" ? "In progress" : "Step failed";
          status.className = stateKey === "analysis-partial" ? "status status--check" : "status status--failed";
          var steps = Array.from(document.querySelectorAll(".run-steps li"));
          steps.forEach(function (step, i) {
            step.className = i < 4 ? "is-done" : "is-waiting";
            step.querySelector("span").textContent = i < 4 ? "✓" : "○";
          });
          if (stateKey === "analysis-partial") {
            steps[4].className = "is-working";
            steps[4].querySelector("span").textContent = "…";
          } else {
            steps[4].className = "is-failed";
            steps[4].querySelector("span").textContent = "!";
          }
          document.querySelector(".progress-log").hidden = true;
        }
        document.querySelectorAll(".team-match-card, .strategy-card").forEach(function (card) {
          card.hidden = stateKey === "analysis-partial" || stateKey === "analysis-error";
        });
        if (running) animateRunLog();
      }
      if (stateKey === "draft-empty") {
        document.querySelector("[data-outline-list]").replaceChildren();
        document.querySelector("[data-outline-list]").hidden = true;
        updateOutlineTotal();
      }
      if (stateKey === "draft-error") {
        var failedSection = Array.from(document.querySelectorAll(".outline-row")).find(function (row) { return row.querySelector(".outline-title").value.toLowerCase().includes("methodology"); });
        if (failedSection) {
          var failLabel = failedSection.querySelector(".outline-status");
          failLabel.hidden = false;
          failLabel.textContent = "Failed";
          failLabel.classList.add("is-failed");
        }
      }
      if (stateKey === "review-loading") document.querySelector("[data-review-content]").hidden = true;
      return;
    }

    var emailTab = event.target.closest("[data-email-tab]");
    if (emailTab) {
      var tabName = emailTab.getAttribute("data-email-tab");
      var emailGroup = emailTab.closest(".email-card");
      emailGroup.querySelectorAll("[data-email-tab]").forEach(function (tab) {
        var selected = tab === emailTab;
        tab.classList.toggle("is-active", selected);
        tab.setAttribute("aria-selected", selected ? "true" : "false");
      });
      emailGroup.querySelectorAll("[data-email-pane]").forEach(function (pane) { pane.hidden = pane.getAttribute("data-email-pane") !== tabName; });
      return;
    }

    if (event.target.closest("[data-prev-stage]")) {
      var prevIndex = stageScreens.indexOf(currentScreen);
      if (prevIndex > 0) showScreen(stageScreens[prevIndex - 1]);
      return;
    }
    if (event.target.closest("[data-next-stage]")) {
      var nextIndex = stageScreens.indexOf(currentScreen);
      if (nextIndex >= 0 && nextIndex < stageScreens.length - 1) showScreen(stageScreens[nextIndex + 1]);
      return;
    }

    var action = event.target.closest("[data-action]");
    if (!action) return;
    if (action.tagName === "A") event.preventDefault();
    var kind = action.getAttribute("data-action");
    if (kind === "settings-sources") {
      showScreen("settings");
      window.setTimeout(function () { document.getElementById("settings-sources").scrollIntoView({ block: "start" }); }, 20);
    } else if (kind === "all-sources") {
      document.querySelectorAll('.source-card input[type="checkbox"]').forEach(function (input) {
        input.checked = true;
        input.closest(".source-card").classList.add("is-checked");
        input.closest(".source-card").querySelector(".source-switch").classList.add("is-on");
      });
      toast("All configurable sources are selected in this preview.");
    } else if (kind === "clear-filters") {
      var search = document.querySelector("[data-opportunity-search]");
      if (search) search.value = "";
      document.querySelectorAll(".filters-card select").forEach(function (select) { select.selectedIndex = 0; });
      filterOpportunities();
    } else if (kind === "remove-filter") {
      var filterKey = action.getAttribute("data-filter-key");
      if (filterKey === "keyword") document.querySelector("[data-opportunity-search]").value = "";
      else {
        var filterControls = { source: "Source filter", country: "Country or region", sector: "Sector", deadline: "Deadline range", status: "Status", fit: "Fit score range" };
        var control = document.querySelector('.filters-card select[aria-label="' + filterControls[filterKey] + '"]');
        if (control) control.selectedIndex = 0;
      }
      filterOpportunities();
      toast("Filter removed.");
    } else if (kind === "retry") {
      toast("Sample only: no live run was retried.");
    } else if (kind === "add-section") {
      addSection();
    } else if (kind === "generate") {
      generateDraft();
    } else if (kind === "no-bid") {
      var reasonBox = document.querySelector(".no-bid-reason");
      reasonBox.hidden = !reasonBox.hidden;
      if (!reasonBox.hidden) reasonBox.querySelector("input").focus();
    } else if (kind === "download") {
      toast("Sample only: no Word file was downloaded.");
    } else if (kind === "manual-intake") {
      closeDrawers();
      toast("Sample only: no file or link was saved.");
      showScreen("analysis");
    } else if (kind === "add-recipient") {
      addRecipient(action);
    } else if (kind === "send-email") {
      if (!action.disabled) {
        var recipients = Array.from(document.querySelectorAll(".email-header .recipient-chip")).map(function (chip) {
          return Array.from(chip.childNodes).filter(function (node) { return node.nodeType === Node.TEXT_NODE; }).map(function (node) { return node.textContent; }).join("").trim();
        });
        if (!recipients.length) { toast("Add at least one internal recipient first."); return; }
        var oldText = action.textContent;
        action.disabled = true;
        action.setAttribute("aria-busy", "true");
        action.innerHTML = '<span class="spinner spinner--button" aria-hidden="true"></span> Sending…';
        window.setTimeout(function () {
          action.textContent = oldText;
          action.removeAttribute("aria-busy");
          var resend = document.querySelector("[data-confirm-resend]");
          document.querySelectorAll('[data-action="send-email"]').forEach(function (button) {
            button.disabled = !(resend && resend.checked);
          });
          toast("Preview only: would email " + recipients.join(", ") + ". No email was sent.");
        }, 650);
      }
    } else {
      toast("Sample preview only. No live data was changed.");
    }
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-confirm-resend]")) {
      document.querySelectorAll('[data-action="send-email"]').forEach(function (button) { button.disabled = !event.target.checked; });
    }
    if (event.target.matches("[data-file-input]")) {
      var name = event.target.files && event.target.files[0] ? event.target.files[0].name : "Drop a file here or browse";
      document.querySelector("[data-file-name]").textContent = name;
    }
    if (event.target.matches('.source-card input[type="checkbox"]')) {
      var card = event.target.closest(".source-card");
      card.classList.toggle("is-checked", event.target.checked);
      card.querySelector(".source-switch").classList.toggle("is-on", event.target.checked);
    }
    if (event.target.matches('input[name="doctype"]')) {
      var isEoi = event.target.value === "eoi";
      document.querySelector("[data-doc-type-label]").textContent = isEoi ? "Expression of Interest" : "Technical Proposal";
      document.querySelector(".chapter-preview span").textContent = isEoi
        ? "Cover letter → Firm profile → Relevant experience → Approach summary → Key experts → Annexes"
        : "Cover letter → Executive summary → Methodology → Work plan → Team → Annexes";
      replaceOutline(isEoi ? eoiSections : technicalSections);
    }
    if (event.target.closest(".filters-card") && event.target.matches("select")) filterOpportunities();
    if (event.target.matches('[aria-label="Sort opportunities"]')) filterOpportunities();
  });

  document.addEventListener("input", function (event) {
    if (event.target.matches("[data-opportunity-search]")) filterOpportunities();
  });

  function filterOpportunities() {
    var field = document.querySelector("[data-opportunity-search]");
    if (!field) return;
    var term = field.value.trim().toLowerCase();
    var selectors = {
      source: '[aria-label="Source filter"]', country: '[aria-label="Country or region"]',
      sector: '[aria-label="Sector"]', deadline: '[aria-label="Deadline range"]',
      status: '[aria-label="Status"]', fit: '[aria-label="Fit score range"]'
    };
    var values = {};
    Object.keys(selectors).forEach(function (key) { values[key] = document.querySelector(selectors[key]).value; });
    var rows = Array.from(document.querySelectorAll("[data-opportunity-rows] tr"));
    var cards = Array.from(document.querySelectorAll("[data-opportunity-cards] .opportunity-card"));
    function matches(item) {
      if (!item.getAttribute("data-search").includes(term)) return false;
      if (values.source !== "All sources" && item.dataset.source !== values.source) return false;
      if (values.country !== "All locations" && item.dataset.country !== values.country) return false;
      if (values.sector !== "All sectors" && item.dataset.sector !== values.sector) return false;
      if (values.status !== "Any status" && item.dataset.status !== values.status) return false;
      var days = parseInt(item.dataset.deadline, 10);
      if (values.deadline === "Next 7 days" && days > 7) return false;
      if (values.deadline === "Next 30 days" && days > 30) return false;
      if (values.deadline === "Later" && days <= 30) return false;
      var fit = item.dataset.fit === "" ? NaN : parseInt(item.dataset.fit, 10);
      if (values.fit === "70 to 100" && !(fit >= 70)) return false;
      if (values.fit === "50 to 69" && !(fit >= 50 && fit <= 69)) return false;
      if (values.fit === "Below 50" && !(fit < 50)) return false;
      return true;
    }
    rows.forEach(function (row) { row.hidden = !matches(row); });
    cards.forEach(function (card) { card.hidden = !matches(card); });
    var sorter = document.querySelector('[aria-label="Sort opportunities"]').value;
    function sortList(items, parent) {
      items.sort(function (a, b) {
        if (sorter === "Newest") return b.dataset.published.localeCompare(a.dataset.published);
        if (sorter === "Fit score") return (parseInt(b.dataset.fit, 10) || -1) - (parseInt(a.dataset.fit, 10) || -1);
        return parseInt(a.dataset.deadline, 10) - parseInt(b.dataset.deadline, 10);
      }).forEach(function (item) { parent.appendChild(item); });
    }
    sortList(rows, document.querySelector("[data-opportunity-rows]"));
    sortList(cards, document.querySelector("[data-opportunity-cards]"));
    var shown = rows.filter(function (row) { return !row.hidden; }).length;
    document.querySelector("[data-visible-count]").textContent = shown + " opportunit" + (shown === 1 ? "y" : "ies");
    document.querySelector("[data-opportunity-empty]").hidden = shown > 0;
    var active = document.querySelector("[data-active-filters]");
    var chips = [];
    if (term) chips.push(["keyword", '“' + field.value.trim() + '”']);
    var labels = { source: "All sources", country: "All locations", sector: "All sectors", deadline: "Any deadline", status: "Any status", fit: "Any fit score" };
    Object.keys(selectors).forEach(function (key) { if (values[key] !== labels[key]) chips.push([key, values[key]]); });
    active.replaceChildren();
    chips.forEach(function (entry) {
      var chip = document.createElement("button");
      chip.type = "button"; chip.className = "filter-pill"; chip.setAttribute("data-action", "remove-filter"); chip.setAttribute("data-filter-key", entry[0]);
      chip.appendChild(document.createTextNode(entry[1] + " "));
      var close = document.createElement("span"); close.setAttribute("aria-hidden", "true"); close.textContent = "×"; chip.appendChild(close);
      active.appendChild(chip);
    });
    if (!chips.length) { var none = document.createElement("span"); none.className = "muted"; none.textContent = "No filters applied"; active.appendChild(none); }
    var clear = document.createElement("button"); clear.type = "button"; clear.className = "link-button"; clear.setAttribute("data-action", "clear-filters"); clear.textContent = "Clear all"; active.appendChild(clear);
  }

  function addRecipient(button) {
    var field = button.parentElement.querySelector("input");
    var value = field.value.trim();
    if (!value) { field.focus(); return; }
    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "recipient-chip";
    chip.appendChild(document.createTextNode(value + " "));
    var remove = document.createElement("span");
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove recipient");
    chip.appendChild(remove);
    button.parentElement.parentElement.insertBefore(chip, button.parentElement);
    field.value = "";
    toast("Recipient added to the sample preview.");
  }

  function addSection() {
    var list = document.querySelector("[data-outline-list]");
    var choice = document.querySelector("[data-section-choice]");
    var title = choice && choice.value && choice.value !== "custom" ? choice.value : "New section";
    var row = makeOutlineRow({ title: title, pages: 2 });
    list.appendChild(row);
    list.hidden = false;
    document.querySelector('[data-state-panel="draft-empty"]').hidden = true;
    if (choice) choice.selectedIndex = 0;
    row.querySelector(".outline-title").focus();
    updateOutlineTotal();
  }

  function makeOutlineRow(section) {
    var row = document.createElement("div");
    row.className = "outline-row";
    row.draggable = true;
    row.innerHTML = '<button type="button" class="drag-handle" aria-label="Drag section">⠿</button><label class="check-touch"><input type="checkbox" checked aria-label="Include section"></label><input class="outline-title" aria-label="Section title"><span class="target-length"></span><button class="move-section" type="button" aria-label="Move section up" data-move="up">↑</button><button class="move-section" type="button" aria-label="Move section down" data-move="down">↓</button><button class="delete-section" type="button" aria-label="Remove section">×</button>';
    var title = row.querySelector(".outline-title");
    title.value = section.title;
    var pageCell = row.querySelector(".target-length");
    pageCell.appendChild(document.createTextNode(section.pages + " pages"));
    var state = document.createElement("span");
    state.className = "outline-status";
    state.hidden = true;
    pageCell.appendChild(state);
    var limit = document.createElement("span");
    limit.className = "outline-limit-tag";
    limit.hidden = true;
    limit.textContent = "Over limit";
    pageCell.appendChild(limit);
    title.addEventListener("input", function () {
      row.querySelector(".drag-handle").setAttribute("aria-label", "Drag " + (title.value || "section"));
    });
    return row;
  }

  function replaceOutline(sections) {
    var list = document.querySelector("[data-outline-list]");
    list.replaceChildren();
    list.hidden = false;
    document.querySelector('[data-state-panel="draft-empty"]').hidden = true;
    sections.forEach(function (section) { list.appendChild(makeOutlineRow(section)); });
    updateOutlineTotal();
  }

  function updateOutlineTotal() {
    var total = 0;
    var checkedCount = 0;
    document.querySelectorAll(".outline-row").forEach(function (row) {
      var box = row.querySelector('input[type="checkbox"]');
      var pages = parseInt(row.querySelector(".target-length").textContent, 10) || 0;
      var limitTag = row.querySelector(".outline-limit-tag");
      if (box.checked) {
        checkedCount += 1;
        limitTag.hidden = total + pages <= 35;
        row.querySelector(".outline-title").classList.toggle("is-over-limit", total + pages > 35);
        total += pages;
      } else {
        limitTag.hidden = true;
        row.querySelector(".outline-title").classList.remove("is-over-limit");
      }
    });
    var result = document.querySelector("[data-outline-total]");
    result.textContent = total + " page" + (total === 1 ? "" : "s");
    result.closest(".card-head").classList.toggle("is-over-limit", total > 35);
    document.querySelector(".over-limit-tag").hidden = total <= 35;
    document.querySelector('[data-state-panel="draft-empty"]').hidden = checkedCount > 0;
    document.querySelector('[data-action="generate"]').disabled = checkedCount === 0;
  }

  function generateDraft() {
    var rows = Array.from(document.querySelectorAll(".outline-row"));
    var selected = rows.filter(function (row) { return row.querySelector('input[type="checkbox"]').checked; });
    var panel = document.querySelector(".generation-progress");
    if (!selected.length) {
      document.querySelector('[data-state-panel="draft-empty"]').hidden = false;
      toast("Add at least one section before generating.");
      return;
    }
    panel.hidden = false;
    var progressList = panel.querySelector("ol");
    progressList.replaceChildren();
    selected.forEach(function (section) {
      var line = document.createElement("li");
      line.setAttribute("data-gen-row", "");
      line.appendChild(document.createTextNode(section.querySelector(".outline-title").value));
      var label = document.createElement("span");
      label.textContent = "Queued";
      line.appendChild(label);
      progressList.appendChild(line);
      var state = section.querySelector(".outline-status");
      if (state) { state.hidden = false; state.textContent = "Queued"; state.className = "outline-status"; }
    });
    var progressRows = Array.from(panel.querySelectorAll("[data-gen-row]"));
    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      progressRows.forEach(function (row, i) {
        row.classList.add("is-done");
        var pages = parseInt(selected[i].querySelector(".target-length").textContent, 10) || 2;
        row.querySelector("span").textContent = (640 + i * 130) + " words · ~" + pages + " pages · Done";
        var state = selected[i].querySelector(".outline-status");
        state.textContent = (640 + i * 130) + " words · ~" + pages + " pages · Done";
        state.classList.add("is-done");
      });
      toast("Sample draft ready. No document was generated.");
      return;
    }
    progressRows.forEach(function (row, i) {
      window.setTimeout(function () {
        row.classList.add("is-writing");
        row.querySelector("span").textContent = "Writing";
        var state = selected[i].querySelector(".outline-status");
        state.textContent = "Writing";
        state.classList.add("is-writing");
      }, i * 750);
      window.setTimeout(function () {
        row.classList.remove("is-writing");
        row.classList.add("is-done");
        var pages = parseInt(selected[i].querySelector(".target-length").textContent, 10) || 2;
        row.querySelector("span").textContent = (640 + i * 130) + " words · ~" + pages + " pages · Done";
        var state = selected[i].querySelector(".outline-status");
        state.textContent = (640 + i * 130) + " words · ~" + pages + " pages · Done";
        state.classList.add("is-done");
        if (i === progressRows.length - 1) toast("Sample draft ready. No document was generated.");
      }, i * 750 + 450);
    });
  }

  function animateScore() {
    var gauge = document.querySelector(".score-gauge");
    if (!gauge) return;
    var number = gauge.querySelector("[data-score-number]");
    var target = parseInt(number.textContent, 10) || 0;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gauge.style.setProperty("--score", target);
      window.__previewScoreAnimated = true;
      return;
    }
    var start = performance.now();
    number.textContent = "0";
    gauge.style.setProperty("--score", 0);
    function frame(now) {
      var t = Math.min(1, (now - start) / 500);
      var eased = 1 - Math.pow(1 - t, 3);
      var value = Math.round(target * eased);
      number.textContent = String(value);
      gauge.style.setProperty("--score", value);
      if (t < 1) requestAnimationFrame(frame);
      else window.__previewScoreAnimated = true;
    }
    requestAnimationFrame(frame);
  }

  function animateRunLog() {
    var panel = document.querySelector("[data-state-panel=" + '"analysis-running"' + "]");
    var label = panel.querySelector("strong");
    var lines = ["Fetching Terms of reference (TOR)", "Extracting the structured brief", "Checking stored client history", "Scoring fit and recommendation", "Matching team CVs", "Building the win strategy"];
    var log = panel.querySelector(".run-live-log");
    var step = 0;
    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    log.replaceChildren();
    lines.forEach(function (line) {
      var item = document.createElement("li");
      item.textContent = "○ " + line;
      log.appendChild(item);
    });
    if (reduced) {
      log.querySelectorAll("li").forEach(function (item) { item.textContent = "✓ " + item.textContent.slice(2); });
      label.textContent = "Run complete · 6 steps";
      return;
    }
    label.textContent = "Starting analysis…";
    window.setTimeout(function next() {
      var current = log.children[step];
      current.textContent = "✓ " + lines[step];
      current.classList.add("is-done");
      label.textContent = lines[step] + " · done";
      step += 1;
      if (step < lines.length) window.setTimeout(next, 300);
      else label.textContent = "Run complete · 6 steps";
    }, 300);
  }

  document.addEventListener("click", function (event) {
    var deleteButton = event.target.closest(".delete-section");
    if (deleteButton) { deleteButton.closest(".outline-row").remove(); updateOutlineTotal(); return; }
    var moveButton = event.target.closest("[data-move]");
    if (moveButton) {
      var row = moveButton.closest(".outline-row");
      if (moveButton.getAttribute("data-move") === "up" && row.previousElementSibling) row.parentElement.insertBefore(row, row.previousElementSibling);
      if (moveButton.getAttribute("data-move") === "down" && row.nextElementSibling) row.parentElement.insertBefore(row.nextElementSibling, row);
      return;
    }
    var recipient = event.target.closest(".recipient-chip");
    if (recipient && event.target.closest(".email-header")) { recipient.remove(); toast("Recipient removed from the sample preview."); }
  });

  function installOpportunityMenus() {
    var menu = document.createElement("div");
    menu.className = "opp-overflow-menu";
    menu.id = "opportunity-actions-menu";
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    menu.innerHTML = '<button type="button" role="menuitem" data-opp-menu-action="dismiss">Dismiss</button><button type="button" role="menuitem" data-opp-menu-action="source">Open source page ↗</button><button type="button" role="menuitem" data-opp-menu-action="drive">Open TOR in Drive ↗</button>';
    document.body.appendChild(menu);
    document.querySelectorAll(".opportunities-table tbody tr").forEach(function (row) {
      var cell = row.lastElementChild;
      var primary = cell.querySelector("button, a");
      var group = document.createElement("div");
      group.className = "opp-actions";
      cell.replaceChildren(group);
      group.appendChild(primary);
      group.appendChild(makeOverflowButton());
    });
    document.querySelectorAll(".opportunity-card").forEach(function (card) {
      var primary = card.querySelector(":scope > button[data-screen]");
      var group = document.createElement("div");
      group.className = "opportunity-actions";
      if (primary) group.appendChild(primary);
      group.appendChild(makeOverflowButton());
      card.appendChild(group);
    });
  }

  function makeOverflowButton() {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "overflow-button";
    button.textContent = "⋯";
    button.setAttribute("aria-label", "More opportunity actions");
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "opportunity-actions-menu");
    button.setAttribute("data-opp-overflow", "");
    return button;
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches('.outline-row input[type="checkbox"]')) updateOutlineTotal();
  });
  document.addEventListener("dragstart", function (event) {
    var row = event.target.closest(".outline-row");
    if (!row) return;
    if (!event.target.closest(".drag-handle")) { event.preventDefault(); return; }
    row.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", "outline-row");
  });
  document.addEventListener("dragend", function (event) {
    var row = event.target.closest(".outline-row");
    if (row) row.classList.remove("is-dragging");
  });
  document.addEventListener("dragover", function (event) {
    var target = event.target.closest(".outline-row");
    var dragging = document.querySelector(".outline-row.is-dragging");
    if (!target || !dragging || target === dragging) return;
    event.preventDefault();
    var rect = target.getBoundingClientRect();
    target.parentElement.insertBefore(dragging, event.clientY < rect.top + rect.height / 2 ? target : target.nextElementSibling);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeDrawers();
    var openDrawer = document.querySelector(".drawer.is-open");
    if (openDrawer && event.key === "Tab") {
      var focusable = Array.from(openDrawer.querySelectorAll('button:not([disabled]), input:not([disabled]), a[href], select, [tabindex]:not([tabindex="-1"])'));
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    var tab = event.target.closest('.stage-tabs [role="tab"]');
    if (tab && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
      event.preventDefault();
      var tabs = Array.from(document.querySelectorAll('.stage-tabs [role="tab"]'));
      var index = tabs.indexOf(tab);
      var nextTab = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      nextTab.focus();
      nextTab.click();
    }
    var emailTab = event.target.closest('.email-tabs [role="tab"]');
    if (emailTab && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
      event.preventDefault();
      var emailTabs = Array.from(emailTab.parentElement.querySelectorAll('[role="tab"]'));
      var emailIndex = emailTabs.indexOf(emailTab);
      var nextEmailTab = emailTabs[(emailIndex + (event.key === "ArrowRight" ? 1 : emailTabs.length - 1)) % emailTabs.length];
      nextEmailTab.focus();
      nextEmailTab.click();
    }
    if (event.target.matches(".add-recipient input") && event.key === "Enter") {
      event.preventDefault();
      event.target.parentElement.querySelector("button").click();
    }
    if (event.key === "Escape") {
      var openMenu = document.querySelector(".opp-overflow-menu");
      if (openMenu && !openMenu.hidden) {
        openMenu.hidden = true;
        document.querySelectorAll("[data-opp-overflow]").forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
        if (menuTrigger) menuTrigger.focus();
      }
    }
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && event.target.closest(".opp-overflow-menu")) {
      event.preventDefault();
      var menuItems = Array.from(document.querySelectorAll(".opp-overflow-menu button"));
      var menuIndex = menuItems.indexOf(event.target.closest("button"));
      menuItems[(menuIndex + (event.key === "ArrowDown" ? 1 : menuItems.length - 1)) % menuItems.length].focus();
    }
  });
  document.querySelector(".mobile-stage-select").addEventListener("change", function (event) { showScreen(event.target.value); });
  document.querySelectorAll(".email-tabs").forEach(function (tablist, listIndex) {
    tablist.querySelectorAll('[role="tab"]').forEach(function (tab, tabIndex) {
      var name = tab.getAttribute("data-email-tab");
      var pane = tablist.parentElement.querySelector('[data-email-pane="' + name + '"]');
      var id = "review-email-tab-" + listIndex + "-" + tabIndex;
      tab.id = id;
      tab.setAttribute("aria-controls", "review-email-pane-" + listIndex + "-" + tabIndex);
      if (pane) {
        pane.id = "review-email-pane-" + listIndex + "-" + tabIndex;
        pane.setAttribute("role", "tabpanel");
        pane.setAttribute("aria-labelledby", id);
      }
    });
  });
  installOpportunityMenus();
  filterOpportunities();
  document.querySelectorAll(".outline-row").forEach(function (row) {
    row.draggable = true;
    var pageCell = row.querySelector(".target-length");
    if (!pageCell.querySelector(".outline-status")) {
      var state = document.createElement("span");
      state.className = "outline-status";
      state.hidden = true;
      pageCell.appendChild(state);
    }
    if (!pageCell.querySelector(".outline-limit-tag")) {
      var limit = document.createElement("span");
      limit.className = "outline-limit-tag";
      limit.hidden = true;
      limit.textContent = "Over limit";
      pageCell.appendChild(limit);
    }
  });
  updateOutlineTotal();
})();
