/* Apply validated local preferences before first paint; no inline script. */
(function () {
  "use strict";
  try {
    var mode = localStorage.getItem("wggb-theme");
    if (mode === "light" || mode === "dark") document.documentElement.dataset.theme = mode;
    if (localStorage.getItem("wggb-nav-compact") === "1") document.documentElement.dataset.navCompact = "1";
  } catch (error) { /* Storage can be unavailable; the light default remains. */ }
})();
