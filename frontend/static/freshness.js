// freshness.js — first-party, vendored, no framework (ux §5.1).
// 1. Re-evaluates each as-of chip's data-asof / data-threshold against the WALL CLOCK every 5s, so a
//    quiet panel or an SSE outage drifts green -> amber -> red honestly WITHOUT a refetch. This is a
//    display-time comparison only: the timestamps and thresholds still arrive from the query layer
//    (ux §1.3 holds). SSE connection state is a SEPARATE signal and never alters these chips (§6.3).
// 2. Keeps the page's content and its read time together. The freshness element (#freshness, in
//    fragments/freshness.html) is polled by htmx and carries data-version: the version of the data
//    behind the page (the factory record's change count, whether it is fresh or stale, and a
//    five-minute clock tick for ages that grow on their own). When a poll brings the same version,
//    only the read time moves on, which is true: nothing the page shows has changed. When it brings
//    a different one, the poll's swap is cancelled and the page's main content is fetched again;
//    that response carries its own freshness element (swapped out of band), so the content, the
//    read time and the banner always come from one read. The chat drawer sits outside #main and is
//    never replaced.
(function () {
  "use strict";
  var STATES = ["live", "lagging", "stale"];

  function driftState(ageSecs, thresholdSecs) {
    if (ageSecs < thresholdSecs) return "live";
    if (ageSecs < 2 * thresholdSecs) return "lagging";
    return "stale";
  }

  function apply(chip, state) {
    STATES.forEach(function (s) { chip.classList.remove("asof-chip--" + s); });
    chip.classList.add("asof-chip--" + state);
    var dot = chip.querySelector(".asof-dot");
    if (dot) {
      STATES.forEach(function (s) { dot.classList.remove("asof-dot--" + s); });
      dot.classList.add("asof-dot--" + state);
    }
  }

  function tick() {
    var nowSecs = Date.now() / 1000;
    var chips = document.querySelectorAll(".asof-chip[data-asof]");
    chips.forEach(function (chip) {
      var asof = parseFloat(chip.getAttribute("data-asof"));
      var threshold = parseFloat(chip.getAttribute("data-threshold"));
      if (isNaN(asof) || isNaN(threshold)) return; // feed-pending chips carry no timestamp
      apply(chip, driftState(nowSecs - asof, threshold));
    });
  }

  var refreshing = false;

  // The version of the data the page shows: the freshness element is only ever replaced by one
  // of the same version (a poll) or together with the content (a refresh), so it always says.
  function shownVersion() {
    var el = document.getElementById("freshness");
    return el ? el.getAttribute("data-version") : null;
  }

  function refreshContent() {
    if (refreshing || !window.htmx || !document.getElementById("main")) return;
    refreshing = true;
    var done = function () { refreshing = false; };
    window.htmx.ajax("GET", window.location.href, { target: "#main", select: "#main", swap: "outerHTML" })
      .then(done, done);
  }

  function onBeforeSwap(evt) {
    var d = evt.detail;
    if (!d || !d.target || d.target.id !== "freshness") return; // only the freshness poll
    var m = /data-version="([^"]*)"/.exec(d.serverResponse || "");
    if (!m || m[1] === shownVersion()) return; // same data: let the new read time in
    d.shouldSwap = false; // newer data than the page shows: fetch both together instead
    refreshContent();
  }

  if (typeof document !== "undefined") {
    document.addEventListener("htmx:beforeSwap", onBeforeSwap);
    document.addEventListener("DOMContentLoaded", function () {
      tick();
      setInterval(tick, 5000);
    });
  }
  // For tests (tests/test_pages.py runs this file under node with a stub page).
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { onBeforeSwap: onBeforeSwap, driftState: driftState };
  }
})();
