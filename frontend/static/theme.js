// theme.js — first-party, no framework. Loaded in <head> WITHOUT defer, so the light or dark theme
// is set on <html> before the first paint. The stored choice wins; otherwise the system setting.
// Storage and matchMedia can both throw (private windows, blocked site data), so each is guarded,
// and the page renders light without them. It also wires any [data-print] button to the
// browser's print, so no template needs an inline handler.
(function () {
  "use strict";
  var KEY = "factory-theme";
  var root = document.documentElement;

  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(value) {
    try { window.localStorage.setItem(KEY, value); } catch (e) { /* not kept: fine */ }
  }
  function system() {
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch (e) { return "light"; }
  }
  function current() { return root.getAttribute("data-theme") === "dark" ? "dark" : "light"; }

  var theme = stored();
  if (theme !== "dark" && theme !== "light") theme = system();
  root.setAttribute("data-theme", theme);

  // The switch's words name the theme it switches TO ("Dark mode"); a hidden "Switch to" makes
  // that plain. On phones the word "mode" is hidden to fit the top bar.
  function sync(button) {
    var label = button.querySelector(".theme-switch__label");
    if (label) label.textContent = current() === "dark" ? "Light" : "Dark";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var switches = document.querySelectorAll(".theme-switch");
    switches.forEach(function (button) {
      sync(button);
      button.hidden = false; // it does nothing without this script, so it starts hidden
      button.addEventListener("click", function () {
        var next = current() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        store(next);
        switches.forEach(sync);
      });
    });
    document.querySelectorAll("[data-print]").forEach(function (button) {
      button.addEventListener("click", function () { window.print(); });
    });
  });
})();
