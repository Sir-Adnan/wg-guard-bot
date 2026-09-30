/* ==========================================================================
   WG-Guard Bot — admin panel behaviour
   Vanilla ES2020, no build step, no external requests.
   Everything is opt-in through data-* attributes so templates stay declarative.
   ========================================================================== */
(function () {
  "use strict";

  var doc = document;

  /* -- helpers ---------------------------------------------------------- */
  function $(sel, root) { return (root || doc).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }

  function toast(message, level) {
    if (!message) return;
    var stack = $("#toasts");
    if (!stack) {
      stack = doc.createElement("div");
      stack.id = "toasts";
      stack.className = "toast-stack";
      doc.body.appendChild(stack);
    }
    var el = doc.createElement("div");
    el.className = "toast toast--" + (level || "info");
    el.setAttribute("role", "status");
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(function () {
      el.classList.add("is-leaving");
      setTimeout(function () { el.remove(); }, 260);
    }, 4200);
  }
  window.panelToast = toast;

  /* -- sidebar ---------------------------------------------------------- */
  function initNav() {
    var burger = $("[data-nav-toggle]");
    var sidebar = $("#sidebar");
    if (!burger || !sidebar) return;
    var scrim = doc.createElement("button");
    scrim.className = "scrim";
    scrim.type = "button";
    scrim.setAttribute("aria-label", doc.body.dataset.uiClose || "Close");
    scrim.hidden = true;
    doc.body.appendChild(scrim);
    var mobile = window.matchMedia("(max-width: 860px)");
    function close() {
      doc.body.classList.remove("nav-open"); scrim.hidden = true;
      burger.setAttribute("aria-expanded", "false");
      if (mobile.matches) sidebar.inert = true;
    }
    function sync() {
      close(); sidebar.inert = mobile.matches;
      if (!mobile.matches) burger.setAttribute("aria-expanded", doc.documentElement.dataset.navCompact !== "1" ? "true" : "false");
    }
    burger.addEventListener("click", function () {
      if (!mobile.matches) {
        var compact = doc.documentElement.dataset.navCompact === "1" ? "0" : "1";
        doc.documentElement.dataset.navCompact = compact;
        burger.setAttribute("aria-expanded", compact === "0" ? "true" : "false");
        try { localStorage.setItem("wggb-nav-compact", compact); } catch (error) { /* local preference only */ }
        return;
      }
      if (doc.body.classList.contains("nav-open")) { close(); return; }
      sidebar.inert = false; doc.body.classList.add("nav-open"); scrim.hidden = false;
      burger.setAttribute("aria-expanded", "true");
      var first = sidebar.querySelector("a"); if (first) first.focus();
    });
    scrim.addEventListener("click", function () { close(); burger.focus(); });
    $$("[data-nav-close]").forEach(function (button) { button.addEventListener("click", function () { close(); burger.focus(); }); });
    doc.addEventListener("keydown", function (event) {
      if (!doc.body.classList.contains("nav-open")) return;
      if (event.key === "Escape") { close(); burger.focus(); }
      if (event.key === "Tab") trapFocus(event, sidebar);
    });
    mobile.addEventListener("change", sync); sync();
  }

  function trapFocus(event, root) {
    var nodes = $$("a[href], button:not(:disabled), input:not(:disabled):not([type=hidden]), select, textarea, [tabindex='0']", root).filter(function (node) { return node.getClientRects().length > 0; });
    if (!nodes.length) { event.preventDefault(); root.focus(); return; }
    var first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  /* Existing data-modal-* contract, with focus containment and return. */
  var modalFocus = new WeakMap();
  function openModal(id) {
    var modal = doc.getElementById(id);
    if (!modal) return;
    modalFocus.set(modal, doc.activeElement);
    if (modal.parentElement !== doc.body) doc.body.appendChild(modal);
    var title = modal.querySelector("[data-modal-title], .modal__title");
    if (title) { if (!title.id) title.id = id + "-title"; modal.setAttribute("aria-labelledby", title.id); }
    modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true");
    modal.hidden = false; doc.body.classList.add("modal-open");
    var shell = $(".shell"); if (shell) shell.inert = true;
    var focusable = modal.querySelector("input:not(:disabled):not([type=hidden]), select:not(:disabled), textarea:not(:disabled)") || modal.querySelector("button");
    if (focusable) focusable.focus();
  }
  function closeModal(modal) {
    if (!modal) return;
    modal.hidden = true;
    if (!$$(".modal").some(function (node) { return !node.hidden; })) {
      doc.body.classList.remove("modal-open"); var shell = $(".shell"); if (shell) shell.inert = false;
    }
    var opener = modalFocus.get(modal); if (opener && opener.isConnected) opener.focus();
  }
  function initModals() {
    $$("[data-modal-close]").forEach(function (button) {
      if (!button.textContent.trim() && !button.hasAttribute("aria-label")) button.setAttribute("aria-label", doc.body.dataset.uiClose || "Close");
    });
    doc.addEventListener("click", function (event) {
      var opener = event.target.closest("[data-modal-open]");
      if (opener) {
        event.preventDefault(); var id = opener.getAttribute("data-modal-open"); var modal = doc.getElementById(id);
        if (modal) {
          Array.prototype.forEach.call(opener.attributes, function (attr) {
            var match = /^data-set-(.+)$/.exec(attr.name); if (!match) return;
            var field = modal.querySelector('[name="' + match[1] + '"]');
            if (field) { if (field.type === "checkbox") field.checked = attr.value === "1"; else field.value = attr.value; }
          });
          var title = modal.querySelector("[data-modal-title]"); var custom = opener.getAttribute("data-title");
          if (title && custom) title.textContent = custom;
        }
        openModal(id); return;
      }
      var closer = event.target.closest("[data-modal-close]");
      if (closer) { event.preventDefault(); closeModal(closer.closest(".modal")); return; }
      if (event.target.classList && event.target.classList.contains("modal")) closeModal(event.target);
    });
    doc.addEventListener("keydown", function (event) {
      if ($(".workspace-dialog[open]")) return;
      var open = $$(".modal").filter(function (modal) { return !modal.hidden; });
      var current = open[open.length - 1]; if (!current) return;
      if (event.key === "Escape") closeModal(current);
      if (event.key === "Tab") trapFocus(event, current);
    });
  }
  window.panelOpenModal = openModal;

  /* -- confirmations ---------------------------------------------------- */
  function confirmAction(message) {
    var dialog = $("#confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(message));
    if (dialog.open) return Promise.resolve(false);
    $("#confirm-message", dialog).textContent = message;
    dialog.returnValue = ""; dialog.showModal();
    return new Promise(function (resolve) {
      dialog.addEventListener("close", function () { resolve(dialog.returnValue === "confirm"); }, { once: true });
    });
  }
  function initConfirm() {
    doc.addEventListener("submit", function (event) {
      var form = event.target;
      if (form.method === "dialog") return;
      if (form.dataset.submitting === "1") { event.preventDefault(); return; }
      var message = form.getAttribute("data-confirm");
      if (message && form.dataset.confirmApproved !== "1") {
        event.preventDefault(); var submitter = event.submitter;
        confirmAction(message).then(function (approved) {
          if (!approved) return; form.dataset.confirmApproved = "1";
          if (submitter && submitter.isConnected) form.requestSubmit(submitter); else form.requestSubmit();
        }); return;
      }
      delete form.dataset.confirmApproved;
      if (form.getAttribute("data-busy") === "false") return;
      form.dataset.submitting = "1";
      var button = event.submitter || form.querySelector("[type=submit]");
      if (button) button.setAttribute("aria-busy", "true");
      setTimeout(function () { delete form.dataset.submitting; if (button) button.removeAttribute("aria-busy"); }, 10000);
    });
    doc.addEventListener("click", function (event) {
      var link = event.target.closest("a[data-confirm]"); if (!link) return;
      event.preventDefault(); confirmAction(link.dataset.confirm).then(function (approved) { if (approved) window.location.assign(link.href); });
    });
    window.addEventListener("pageshow", function () {
      $$("form[data-submitting]").forEach(function (form) { delete form.dataset.submitting; });
      $$("[aria-busy]").forEach(function (button) { button.removeAttribute("aria-busy"); });
    });
  }

  function initCommand() {
    var dialog = $("#command-dialog"); if (!dialog || typeof dialog.showModal !== "function") return;
    var input = $("#command-input", dialog), results = $(".command-results", dialog), empty = $(".command-empty", dialog);
    $$("[data-nav-link]").forEach(function (link) {
      var item = link.cloneNode(true); item.removeAttribute("aria-current"); item.className = "command-item";
      var badge = $(".nav__badge", item); if (badge) badge.remove();
      results.appendChild(item);
    });
    function search() {
      var needle = input.value.normalize("NFKC").replace(/ي/g, "ی").replace(/ك/g, "ک").toLowerCase(); var count = 0;
      $$("a", results).forEach(function (link) { var match = link.textContent.normalize("NFKC").replace(/ي/g, "ی").replace(/ك/g, "ک").toLowerCase().includes(needle); link.hidden = !match; if (match) count++; });
      empty.hidden = count !== 0;
    }
    function open() { if (!dialog.open) { input.value = ""; search(); dialog.showModal(); input.focus(); } }
    $$("[data-command-open]").forEach(function (button) { button.addEventListener("click", open); });
    $("[data-dialog-close]", dialog).addEventListener("click", function () { dialog.close(); });
    input.addEventListener("input", search);
    input.addEventListener("keydown", function (event) { var first = $("a:not([hidden])", results); if (event.key === "ArrowDown" && first) { event.preventDefault(); first.focus(); } if (event.key === "Enter" && first) { event.preventDefault(); first.click(); } });
    doc.addEventListener("keydown", function (event) { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); open(); } });
    dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });
  }

  /* -- copy to clipboard ------------------------------------------------ */
  function initCopy() {
    doc.addEventListener("click", function (e) {
      var el = e.target.closest("[data-copy]");
      if (!el) return;
      var value = el.getAttribute("data-copy") || el.textContent.trim();
      var done = function () { toast("کپی شد ✓", "success"); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(done, function () { fallback(value, done); });
      } else {
        fallback(value, done);
      }
    });
    function fallback(value, done) {
      var ta = doc.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      doc.body.appendChild(ta);
      ta.select();
      try { doc.execCommand("copy"); done(); } catch (err) { toast("کپی نشد", "danger"); }
      ta.remove();
    }
  }

  /* -- client-side table filter ---------------------------------------- */
  function initFilters() {
    $$("[data-filter-target]").forEach(function (input) {
      var table = doc.getElementById(input.getAttribute("data-filter-target"));
      if (!table) return;
      input.addEventListener("input", function () {
        var needle = input.value.trim().toLowerCase();
        $$("tbody tr", table).forEach(function (row) {
          row.style.display = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1 ? "" : "none";
        });
      });
    });
  }

  /* -- auto-submit on change ------------------------------------------- */
  function initAutoSubmit() {
    doc.addEventListener("change", function (e) {
      var el = e.target.closest("[data-autosubmit]");
      if (el && el.form) el.form.submit();
    });
  }

  /* -- theme ------------------------------------------------------------ */
  function initTheme() {
    var root = doc.documentElement;
    var saved = null;
    try { saved = localStorage.getItem("wggb-theme"); } catch (err) { saved = null; }
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
    $$("[data-theme-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("wggb-theme", next); } catch (err) { /* ignore */ }
      });
    });
  }

  /* -- charts: reveal bar heights -------------------------------------- */
  function initCharts() {
    $$(".chart .bar").forEach(function (bar, i) {
      var target = bar.getAttribute("data-h");
      if (!target) return;
      bar.setAttribute("height", "0");
      setTimeout(function () { bar.setAttribute("height", target); }, 60 + i * 18);
    });
    $$(".bar-row__fill").forEach(function (fill) {
      var target = fill.getAttribute("data-w");
      if (!target) return;
      fill.style.width = "0%";
      setTimeout(function () { fill.style.width = target; }, 80);
    });
  }

  /* -- keyboard shortcuts ---------------------------------------------- */
  function initShortcuts() {
    doc.addEventListener("keydown", function (e) {
      if (e.target.matches("input, textarea, select")) return;
      if (e.key === "/") {
        var search = $("input[type=search], .searchbar input");
        if (search) { e.preventDefault(); search.focus(); }
      }
    });
  }

  /* -- drag & drop ordering ---------------------------------------------
     Opt-in contract, also documented in `docs/PANEL-CONTRACT.md` §6:

       [data-drag-list="<entity>"]   the list; its children may be dragged
       [data-drag-scope="<id>"]      optional partition (a category's parent_id)
       [data-drag-next="/panel/..."] optional return URL for the no-JS path
       [data-drag-id]                one row/item of that list
       [data-drag-handle]            the visible grip inside the item

     The list may be a <table>, a <div> or a <ul>; anything else on the page (a
     form, a modal, a filter) keeps working exactly as before.
  --------------------------------------------------------------------- */
  var DRAG_SELECTOR = "[data-drag-list]";
  var DRAG_ITEM_SELECTOR = "[data-drag-id]";
  var DRAG_HANDLE_SELECTOR = "[data-drag-handle]";
  var REORDER_PATH = "/reorder";

  function dragState() {
    if (!window.__panelDrag) {
      window.__panelDrag = { row: null, startOrder: null, placeholder: null, list: null, busy: false };
    }
    return window.__panelDrag;
  }

  function csrfToken() {
    return (doc.body && doc.body.getAttribute("data-csrf")) || "";
  }

  /* Direct children of the list that are draggable — a nested list's items are
     not children of the outer list, so a category tree stays safe. */
  function dragItems(list) {
    var out = [];
    for (var i = 0; i < list.children.length; i++) {
      var child = list.children[i];
      if (child.hasAttribute && child.hasAttribute("data-drag-id")) out.push(child);
    }
    return out;
  }

  function dragOrder(list) {
    return dragItems(list).map(function (item) { return item.getAttribute("data-drag-id"); }).join(",");
  }

  /* The list a nested item belongs to: the closest ancestor, not the outer one. */
  function listOf(item) {
    return item && item.closest ? item.closest(DRAG_SELECTOR) : null;
  }

  function clearDragState() {
    var state = dragState();
    if (state.placeholder) state.placeholder.remove();
    if (state.list) state.list.classList.remove("drop-target");
    if (state.row) state.row.classList.remove("is-dragging");
    state.row = null;
    state.placeholder = null;
    state.list = null;
  }

  function restoreDragOrder(list, markup) {
    if (list && markup != null) list.innerHTML = markup;
  }

  /* Which side of `item` the pointer is on (RTL safe: the horizontal midpoint
     only decides for tiles, rows are decided vertically). */
  function midpointPast(item, event) {
    var box = item.getBoundingClientRect();
    var vertical = box.height > 0 ? (event.clientY - box.top) / box.height : 0;
    if (box.width <= 0) return vertical > 0.5;
    var horizontal = Math.abs(box.width - box.height) < 4 ? 0.5 : (event.clientX - box.left) / box.width;
    return horizontal > 0.5 || vertical > 0.5;
  }

  function placePlaceholder(state, item, event) {
    var list = state.list;
    var after = midpointPast(item, event);
    if (after) {
      var next = item.nextElementSibling;
      list.insertBefore(state.placeholder, next);
    } else {
      list.insertBefore(state.placeholder, item);
    }
  }

  /* The gap the row will land in: cloning it keeps the height honest so the
     list does not jump while the operator drags. */
  function makePlaceholder(row) {
    var clone = row.cloneNode(true);
    clone.removeAttribute("data-drag-id");
    clone.removeAttribute("draggable");
    clone.classList.add("drag-placeholder");
    clone.setAttribute("aria-hidden", "true");
    var handle = clone.querySelector(DRAG_HANDLE_SELECTOR);
    if (handle) handle.removeAttribute("tabindex");
    return clone;
  }

  /* Optimistic DOM move for the keyboard path, before the POST goes out. */
  function moveItem(item, direction) {
    var list = item.parentNode;
    var items = dragItems(list);
    var index = items.indexOf(item);
    var target = index + direction;
    if (index < 0 || target < 0 || target >= items.length) return null;
    var other = items[target];
    if (direction < 0) list.insertBefore(item, other);
    else list.insertBefore(other, item);
    return list;
  }

  async function postOrder(list, markup) {
    var state = dragState();
    var entity = list.getAttribute("data-drag-list");
    var scope = list.getAttribute("data-drag-scope");
    var next = list.getAttribute("data-drag-next");
    var failed = "ترتیب ذخیره نشد؛ صفحه را دوباره باز کنید و دوباره تلاش کنید.";

    var body = new FormData();
    body.append("entity", entity || "");
    body.append("csrf_token", csrfToken());
    body.append("ids", dragOrder(list));
    if (scope) body.append("scope", scope);
    if (next) body.append("next", next);

    try {
      var response = await fetch(REORDER_PATH, {
        method: "POST",
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
        body: body,
        credentials: "same-origin"
      });
      var payload = null;
      try { payload = await response.json(); } catch (err) { payload = null; }
      /* A redirect to the login page answers with HTML, so an unreadable body
         means "not saved" — restore the order the operator started from. */
      if (!payload || payload.ok !== true) {
        restoreDragOrder(list, markup);
        toast((payload && payload.message) || failed, "error");
        return;
      }
      toast(payload.message || "ترتیب ذخیره شد.");
    } catch (err) {
      restoreDragOrder(list, markup);
      toast(failed, "error");
    } finally {
      state.busy = false;
    }
  }

  function initDragLists() {
    $$(DRAG_SELECTOR).forEach(function (list) {
      dragItems(list).forEach(function (item) {
        if (item.querySelector(DRAG_HANDLE_SELECTOR)) item.setAttribute("draggable", "true");
      });

      list.addEventListener("dragstart", function (event) {
        var state = dragState();
        var handle = event.target.closest ? event.target.closest(DRAG_HANDLE_SELECTOR) : null;
        var item = handle && handle.closest(DRAG_ITEM_SELECTOR);
        if (!item || item.parentNode !== list || state.busy) {
          event.preventDefault();
          return;
        }
        state.row = item;
        state.list = list;
        state.startOrder = dragOrder(list);
        state.placeholder = makePlaceholder(item);
        item.classList.add("is-dragging");
        list.classList.add("drop-target");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          /* Firefox refuses to start a drag with an empty payload. */
          event.dataTransfer.setData("text/plain", item.getAttribute("data-drag-id") || "");
        }
      });

      list.addEventListener("dragover", function (event) {
        var state = dragState();
        if (!state.row || state.list !== list) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        var over = event.target.closest ? event.target.closest(DRAG_ITEM_SELECTOR) : null;
        if (over && over.parentNode === list && over !== state.row && over !== state.placeholder) {
          placePlaceholder(state, over, event);
        }
      });

      list.addEventListener("drop", function (event) {
        var state = dragState();
        if (!state.row || state.list !== list) return;
        event.preventDefault();
        var markup = list.innerHTML;
        var startedFrom = state.startOrder;
        if (state.placeholder && state.placeholder.parentNode) {
          list.insertBefore(state.row, state.placeholder);
        }
        var order = dragOrder(list);
        clearDragState();
        if (order === startedFrom) {
          restoreDragOrder(list, markup);
          return;
        }
        state.busy = true;
        postOrder(list, markup);
      });

      list.addEventListener("dragend", function () {
        clearDragState();
      });
    });

    /* Keyboard path: the handle owns focus, Alt+Arrow moves the row through the
       same POST, so a reorder never needs a mouse. */
    doc.addEventListener("keydown", function (event) {
      if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
      var target = event.target;
      var item = target && target.closest ? target.closest(DRAG_ITEM_SELECTOR) : null;
      if (!item || !item.querySelector(DRAG_HANDLE_SELECTOR)) return;
      var list = listOf(item);
      if (!list) return;

      var state = dragState();
      if (state.busy) return;
      event.preventDefault();

      var markup = list.innerHTML;
      var previousOrder = dragOrder(list);
      moveItem(item, event.key === "ArrowUp" ? -1 : 1);
      if (dragOrder(list) === previousOrder) {
        restoreDragOrder(list, markup);
        return;
      }

      state.busy = true;
      postOrder(list, markup);
      var handle = item.querySelector(DRAG_HANDLE_SELECTOR);
      if (handle) handle.focus();
    });
  }

  /* -- main-menu layout builder -----------------------------------------
     Opt-in contract, also documented in `docs/PANEL-CONTRACT.md` §6.2:

       [data-menu-builder]       the card; it also carries the endpoint + caps
       [data-menu-url]           where a fetch save goes (POST, JSON in `layout`)
       [data-menu-max-rows]      row cap, straight from the service
       [data-menu-max-buttons]   buttons per row, straight from the service
       [data-menu-rows]          the container of the row containers
       [data-menu-row]           one row of the menu
       [data-menu-palette]       where the buttons outside the menu live
       [data-menu-palette-note]  the "nothing left to add" line
       [data-menu-key]           one chip (one menu button)
       [data-menu-handle]        the focusable grip inside the chip
       [data-menu-remove]        the ✕ inside a chip that is in a row
       [data-menu-add-row]       appends an empty row
       [data-menu-save]          posts the whole arrangement

     A drag or a click only edits the page: this editor saves the whole menu at
     once, and a save that fails puts back the arrangement the server still has,
     so the screen never shows an unsaved menu as if it were saved.  The
     keyboard path (Alt+arrow) moves the chip and then saves through that very
     same function, which is what makes the editor usable without a mouse.
  --------------------------------------------------------------------- */
  var MENU_BUILDER_SELECTOR = "[data-menu-builder]";
  var MENU_ROWS_SELECTOR = "[data-menu-rows]";
  var MENU_ROW_SELECTOR = "[data-menu-row]";
  var MENU_CHIP_SELECTOR = "[data-menu-key]";
  var MENU_HANDLE_SELECTOR = "[data-menu-handle]";
  var MENU_REMOVE_SELECTOR = "[data-menu-remove]";
  var MENU_FAILED = "چیدمان ذخیره نشد؛ صفحه را دوباره باز کنید و دوباره تلاش کنید.";

  /* Which way each arrow moves a chip.  The panel is RTL, so "left" is later in
     the row and "right" is earlier — the same order the bot will draw. */
  var MENU_STEPS = {
    ArrowLeft: { axis: "row", delta: 1 },
    ArrowRight: { axis: "row", delta: -1 },
    ArrowUp: { axis: "rows", delta: -1 },
    ArrowDown: { axis: "rows", delta: 1 }
  };

  function menuState() {
    if (!window.__panelMenu) window.__panelMenu = { chip: null, busy: false };
    return window.__panelMenu;
  }

  function initMenuBuilder() {
    var root = $(MENU_BUILDER_SELECTOR);
    var rowsBox = root && $(MENU_ROWS_SELECTOR, root);
    var palette = root && $("[data-menu-palette]", root);
    var url = root && root.getAttribute("data-menu-url");
    if (!root || !rowsBox || !palette || !url) return;

    var maxRows = parseInt(root.getAttribute("data-menu-max-rows"), 10) || 12;
    var maxButtons = parseInt(root.getAttribute("data-menu-max-buttons"), 10) || 8;
    var note = $("[data-menu-palette-note]", root);

    /* Every chip in the card, in the order the page rendered it.  The DOM *is*
       the model: a chip is only ever moved, never rebuilt, so its label, emoji
       and hidden form field travel with it. */
    var chips = {};
    var order = [];
    $$(MENU_CHIP_SELECTOR, root).forEach(function (chip) {
      var key = chip.getAttribute("data-menu-key");
      if (!key || chips[key]) return;
      chips[key] = chip;
      order.push(key);
      chip.setAttribute("draggable", "true");
    });

    function rowElements() { return $$(MENU_ROW_SELECTOR, rowsBox); }

    function keysOf(row) {
      return $$(MENU_CHIP_SELECTOR, row).map(function (chip) { return chip.getAttribute("data-menu-key"); });
    }

    function rowsModel() { return rowElements().map(keysOf); }

    /* An empty row stays until the next save — the service drops it, and until
       then it is a perfectly good drop target. */
    function makeRow() {
      var row = doc.createElement("div");
      row.className = "menu-row";
      row.setAttribute("data-menu-row", "");
      var marker = doc.createElement("input");
      marker.type = "hidden";
      marker.name = "row_break";
      marker.value = "1";
      row.appendChild(marker);
      rowsBox.appendChild(row);
      return row;
    }

    /* Redraw the page from `target` (a list of lists of keys): every chip the
       arrangement does not mention goes back to the palette. */
    function paint(target) {
      var rowEls = rowElements();
      while (rowEls.length < target.length) rowEls.push(makeRow());
      while (rowEls.length > target.length) rowEls.pop().remove();

      var placed = {};
      target.forEach(function (keys, index) {
        var row = rowEls[index];
        var marker = row.querySelector('input[name="row_break"]');
        keys.forEach(function (key) {
          var chip = chips[key];
          if (!chip || placed[key]) return;
          placed[key] = true;
          row.insertBefore(chip, marker);
        });
      });

      order.forEach(function (key) {
        var chip = chips[key];
        var inMenu = !!placed[key];
        if (!inMenu) palette.appendChild(chip);
        chip.classList.toggle("is-palette", !inMenu);
        /* A chip outside the menu must not submit its key on the no-JS path. */
        var field = chip.querySelector('input[name="keys"]');
        if (field) field.disabled = !inMenu;
      });
      if (note) note.hidden = !!palette.querySelector(MENU_CHIP_SELECTOR);
    }

    /* The arrangement the server last confirmed: the page as rendered, then the
       last save that answered ok.  Every failed save goes back to it. */
    var saved = rowsModel();

    function removeKey(key) {
      paint(
        rowsModel().map(function (row) {
          return row.filter(function (item) { return item !== key; });
        })
      );
    }

    /* Where a chip from the palette joins the menu: the last row, or a new one
       when that row is full.  Returns null when the menu cannot take it. */
    function claim(key) {
      var target = rowsModel();
      if (!target.length) target.push([]);
      var last = target.length - 1;
      if (target[last].length >= maxButtons) {
        if (target.length >= maxRows) {
          toast("ردیف‌های منو پر شده‌اند؛ برای افزودن دکمه‌ی تازه یکی از ردیف‌ها را خالی کنید.", "error");
          return null;
        }
        target.push([]);
        last = target.length - 1;
      }
      target[last].push(key);
      return target;
    }

    /* The one save path: the button, the keyboard and anything else that has to
       persist the arrangement all end up here. */
    async function save() {
      var state = menuState();
      if (state.busy) return;
      state.busy = true;
      var saveBtn = $("[data-menu-save]", root);
      if (saveBtn) saveBtn.classList.add("is-disabled");

      var body = new FormData();
      body.append("csrf_token", csrfToken());
      body.append("layout", JSON.stringify(rowsModel()));

      try {
        var response = await fetch(url, {
          method: "POST",
          headers: { "X-Requested-With": "fetch", Accept: "application/json" },
          body: body,
          credentials: "same-origin"
        });
        var payload = null;
        try { payload = await response.json(); } catch (err) { payload = null; }
        /* An expired session redirects to the login page, which answers with
           HTML: an unreadable body means "not saved", so put back the menu the
           server still has instead of leaving the edit on screen. */
        if (!payload || payload.ok !== true) {
          paint(saved);
          toast((payload && payload.message) || MENU_FAILED, "error");
          return;
        }
        /* The service drops empty rows, so the confirmed arrangement is this one
           without them: what the screen shows and what the bot draws stay the
           same menu. */
        saved = rowsModel().filter(function (row) { return row.length; });
        paint(saved);
        toast(payload.message || "چیدمان منوی اصلی ذخیره شد.");
      } catch (err) {
        paint(saved);
        toast(MENU_FAILED, "error");
      } finally {
        state.busy = false;
        if (saveBtn) saveBtn.classList.remove("is-disabled");
      }
    }

    /* The chip the pointer is over, but only one of *this* card's chips. */
    function chipFromEvent(event) {
      var node = event.target;
      var chip = node && node.closest ? node.closest(MENU_CHIP_SELECTOR) : null;
      return chip && chips[chip.getAttribute("data-menu-key")] === chip ? chip : null;
    }

    function rowFromEvent(event) {
      var node = event.target;
      return node && node.closest ? node.closest(MENU_ROW_SELECTOR) : null;
    }

    /* Which side of `over` the pointer is on.  Direction matters: in this RTL
       panel "after" means the pointer went past the midpoint towards the left. */
    function pastMidpoint(over, event) {
      var box = over.getBoundingClientRect();
      if (box.width <= 0) return false;
      var ratio = (event.clientX - box.left) / box.width;
      var rtl = window.getComputedStyle(over.parentNode).direction === "rtl";
      return rtl ? ratio < 0.5 : ratio > 0.5;
    }

    function clearMenuDrag() {
      var state = menuState();
      if (state.chip) state.chip.classList.remove("is-dragging");
      state.chip = null;
      rowsBox.classList.remove("drop-target");
      palette.classList.remove("drop-target");
    }

    root.addEventListener("dragstart", function (event) {
      var state = menuState();
      var chip = chipFromEvent(event);
      /* Only the grip starts a drag, exactly like the ordering lists — a chip
         carries its ✕ right next to its label. */
      var handle = event.target.closest ? event.target.closest(MENU_HANDLE_SELECTOR) : null;
      if (!chip || !handle || state.busy) {
        event.preventDefault();
        return;
      }
      state.chip = chip;
      chip.classList.add("is-dragging");
      rowsBox.classList.add("drop-target");
      palette.classList.add("drop-target");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        /* Firefox refuses to start a drag with an empty payload. */
        event.dataTransfer.setData("text/plain", chip.getAttribute("data-menu-key") || "");
      }
    });

    /* The chip follows the pointer live; the save button persists the result. */
    rowsBox.addEventListener("dragover", function (event) {
      var state = menuState();
      if (!state.chip) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      var row = rowFromEvent(event) || rowElements()[rowElements().length - 1];
      if (!row) row = makeRow();
      var over = chipFromEvent(event);
      if (over === state.chip) return;
      if (over && over.parentNode === row) {
        row.insertBefore(state.chip, pastMidpoint(over, event) ? over.nextElementSibling : over);
      } else if (!over) {
        row.insertBefore(state.chip, row.querySelector('input[name="row_break"]'));
      }
    });

    rowsBox.addEventListener("drop", function (event) {
      if (!menuState().chip) return;
      event.preventDefault();
      clearMenuDrag();
    });

    /* Dropping a chip on the palette takes it out of the menu — the dragging
       counterpart of the ✕. */
    palette.addEventListener("dragover", function (event) {
      if (!menuState().chip) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    });

    palette.addEventListener("drop", function (event) {
      var state = menuState();
      if (!state.chip) return;
      event.preventDefault();
      var chip = state.chip;
      clearMenuDrag();
      removeKey(chip.getAttribute("data-menu-key"));
    });

    root.addEventListener("dragend", clearMenuDrag);

    root.addEventListener("click", function (event) {
      var node = event.target;
      if (!node || !node.closest) return;

      if (node.closest(MENU_REMOVE_SELECTOR)) {
        /* Without JavaScript this is a real submit that removes and saves. */
        event.preventDefault();
        var chip = chipFromEvent(event);
        if (chip) removeKey(chip.getAttribute("data-menu-key"));
        return;
      }

      if (node.closest("[data-menu-add-row]")) {
        event.preventDefault();
        if (rowElements().length >= maxRows) {
          toast("ردیف‌های منو پر شده‌اند؛ برای افزودن ردیف تازه یکی از ردیف‌ها را خالی کنید.", "error");
          return;
        }
        makeRow();
        return;
      }

      var paletteChip = chipFromEvent(event);
      if (paletteChip && paletteChip.classList.contains("is-palette")) {
        var claimed = claim(paletteChip.getAttribute("data-menu-key"));
        if (claimed) paint(claimed);
      }
    });

    /* Keyboard path: the grip owns focus, Alt+arrow moves the chip and saves it
       through the same function the save button uses.  Without a mouse the
       editor is still completable, and a palette chip joins the menu first. */
    doc.addEventListener("keydown", function (event) {
      if (!event.altKey) return;
      var step = MENU_STEPS[event.key];
      if (!step) return;
      var chip = chipFromEvent(event);
      if (!chip || !chip.querySelector(MENU_HANDLE_SELECTOR)) return;
      if (menuState().busy) return;

      var key = chip.getAttribute("data-menu-key");
      var model = rowsModel();
      var from = -1;
      var at = -1;
      model.forEach(function (row, index) {
        var position = row.indexOf(key);
        if (position !== -1) { from = index; at = position; }
      });

      event.preventDefault();

      if (from === -1) {
        var claimed = claim(key);
        if (claimed) { paint(claimed); save(); }
        return;
      }

      var target = model.map(function (row) { return row.slice(); });
      target[from].splice(at, 1);
      if (step.axis === "row") {
        var slot = step.delta > 0 ? Math.min(at + 1, target[from].length) : at - 1;
        if (slot < 0 || slot === at) return;
        target[from].splice(slot, 0, key);
      } else {
        var neighbour = from + step.delta;
        if (neighbour < 0 || neighbour >= target.length) return;
        target[neighbour].splice(Math.min(at, target[neighbour].length), 0, key);
      }

      paint(target);
      var handle = chip.querySelector(MENU_HANDLE_SELECTOR);
      if (handle) handle.focus();
      save();
    });
  }

  /* -- boot ------------------------------------------------------------- */
  function boot() {
    initNav();
    initModals();
    initConfirm();
    initCopy();
    initFilters();
    $$(".table-wrap").forEach(function (region) { region.tabIndex = 0; region.setAttribute("role", "region"); var card = region.closest(".card"); var title = card ? $(".card__title", card) : $("#page-heading"); if (title) region.setAttribute("aria-label", title.textContent); });
    initAutoSubmit();
    initTheme();
    initCharts();
    initShortcuts();
    initCommand();
    initDragLists();
    initMenuBuilder();
    var flash = $("#server-flash");
    if (flash) toast(flash.getAttribute("data-message"), flash.getAttribute("data-level"));
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
