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
    var flash = $("#server-flash");
    if (flash) toast(flash.getAttribute("data-message"), flash.getAttribute("data-level"));
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
