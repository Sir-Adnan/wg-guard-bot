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
    if (!burger) return;
    var scrim = doc.createElement("div");
    scrim.className = "scrim";
    scrim.hidden = true;
    doc.body.appendChild(scrim);

    function close() { doc.body.classList.remove("nav-open"); scrim.hidden = true; }
    burger.addEventListener("click", function () {
      var open = doc.body.classList.toggle("nav-open");
      scrim.hidden = !open;
    });
    scrim.addEventListener("click", close);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  }

  /* -- modals ----------------------------------------------------------- */
  function openModal(id) {
    var modal = doc.getElementById(id);
    if (!modal) return;
    modal.hidden = false;
    var focusable = modal.querySelector("input, select, textarea, button");
    if (focusable) setTimeout(function () { focusable.focus(); }, 60);
  }
  function closeModal(modal) { if (modal) modal.hidden = true; }

  function initModals() {
    doc.addEventListener("click", function (e) {
      var opener = e.target.closest("[data-modal-open]");
      if (opener) {
        e.preventDefault();
        var id = opener.getAttribute("data-modal-open");
        var modal = doc.getElementById(id);
        // Allow the opener to prefill fields: data-set-field="value"
        if (modal) {
          $$("[data-prefill]", opener).forEach(function () {});
          Array.prototype.forEach.call(opener.attributes, function (attr) {
            var match = /^data-set-(.+)$/.exec(attr.name);
            if (!match) return;
            var field = modal.querySelector('[name="' + match[1] + '"]');
            if (field) {
              if (field.type === "checkbox") field.checked = attr.value === "1";
              else field.value = attr.value;
            }
          });
          var titleTarget = modal.querySelector("[data-modal-title]");
          var customTitle = opener.getAttribute("data-title");
          if (titleTarget && customTitle) titleTarget.textContent = customTitle;
        }
        openModal(id);
        return;
      }
      var closer = e.target.closest("[data-modal-close]");
      if (closer) {
        e.preventDefault();
        closeModal(closer.closest(".modal"));
        return;
      }
      if (e.target.classList && e.target.classList.contains("modal")) closeModal(e.target);
    });
    doc.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      $$(".modal").forEach(function (m) { if (!m.hidden) closeModal(m); });
    });
  }
  window.panelOpenModal = openModal;

  /* -- confirmations ---------------------------------------------------- */
  function initConfirm() {
    doc.addEventListener("submit", function (e) {
      var form = e.target;
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) { e.preventDefault(); return; }
      var btn = form.querySelector("[type=submit]");
      if (btn && form.getAttribute("data-busy") !== "false") {
        btn.classList.add("is-disabled");
        btn.dataset.originalText = btn.textContent;
        btn.textContent = "در حال انجام…";
        setTimeout(function () {
          btn.classList.remove("is-disabled");
          if (btn.dataset.originalText) btn.textContent = btn.dataset.originalText;
        }, 8000);
      }
    });
    doc.addEventListener("click", function (e) {
      var link = e.target.closest("a[data-confirm]");
      if (link && !window.confirm(link.getAttribute("data-confirm"))) e.preventDefault();
    });
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
    if (saved) root.setAttribute("data-theme", saved);
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

  /* -- boot ------------------------------------------------------------- */
  function boot() {
    initNav();
    initModals();
    initConfirm();
    initCopy();
    initFilters();
    initAutoSubmit();
    initTheme();
    initCharts();
    initShortcuts();
    initDragLists();
    var flash = $("#server-flash");
    if (flash) toast(flash.getAttribute("data-message"), flash.getAttribute("data-level"));
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
