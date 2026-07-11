// freshness.js — first-party, vendored, no framework (ux §5.1).
// Re-evaluates each as-of chip's data-asof / data-threshold against the WALL CLOCK every 5s, so a
// quiet panel or an SSE outage drifts green -> amber -> red honestly WITHOUT a refetch. This is a
// display-time comparison only: the timestamps and thresholds still arrive from the query layer
// (ux §1.3 holds). SSE connection state is a SEPARATE signal and never alters these chips (§6.3).
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

  document.addEventListener("DOMContentLoaded", function () {
    tick();
    setInterval(tick, 5000);
  });
})();
