// sse.js — first-party, no framework (ux §6). ONE EventSource per page.
//
// The push channel is NOTIFICATION-ONLY (design §6 / DDR-DASH-002): a `panel_update` carries
// {panel, scope_keys, at} and NO row data. On receipt we re-fetch that panel's fragment through the
// same tenant-bound mode=ro query layer (hx-get semantics), coalesced to at most one refetch per
// 1 s per panel, with a 600 ms background highlight on swap — no layout movement, no scroll jump.
//
// SSE connection health is a SEPARATE signal from data freshness (§6.3): the connection dot and the
// "updates paused" banner reflect the socket, and NEVER alter the as-of chips (freshness.js owns
// those). green = open · amber pulse = reconnecting · red = down >30 s (+ paused banner).
(function () {
  "use strict";

  function panelsOnPage() {
    var ids = [];
    document.querySelectorAll("[data-panel]").forEach(function (el) {
      var id = el.getAttribute("data-panel");
      if (id && ids.indexOf(id) === -1) ids.push(id);
    });
    return ids;
  }

  function setDot(state, title) {
    var dot = document.querySelector(".sse-dot");
    if (!dot) return;
    dot.setAttribute("data-sse-state", state);
    dot.setAttribute("title", title);
  }

  function setPaused(paused, at) {
    var slot = document.querySelector(".banner-slot");
    if (!slot) return;
    var existing = slot.querySelector(".banner--sse_paused");
    // The server-rendered projector-stalled banner (worse condition) always wins (§3).
    if (slot.querySelector(".banner--projector_stalled")) { if (existing) existing.remove(); return; }
    if (paused && !existing) {
      var b = document.createElement("div");
      b.className = "banner banner--sse_paused";
      b.setAttribute("role", "status");
      b.textContent = "updates paused — showing data as of " + (at || "last sync") + " · [refresh]";
      b.addEventListener("click", function () { window.location.reload(); });
      slot.appendChild(b);
    } else if (!paused && existing) {
      existing.remove();
    }
  }

  var lastFetch = {}; // panel -> epoch ms of last refetch (coalescing)
  var pending = {};

  function refetch(panel) {
    var target = document.querySelector('[data-panel="' + panel + '"]');
    if (!target) return;
    var now = Date.now();
    var prev = lastFetch[panel] || 0;
    if (now - prev < 1000) { // coalesce: at most one refetch per 1 s per panel
      if (!pending[panel]) {
        pending[panel] = setTimeout(function () { pending[panel] = null; refetch(panel); }, 1000 - (now - prev));
      }
      return;
    }
    lastFetch[panel] = now;
    if (window.htmx) {
      window.htmx.ajax("GET", "/fragments/" + panel, { target: target, swap: "outerHTML" }).then(function () {
        highlight(document.querySelector('[data-panel="' + panel + '"]'));
      });
    } else {
      fetch("/fragments/" + panel).then(function (r) { return r.text(); }).then(function (html) {
        target.outerHTML = html;
        highlight(document.querySelector('[data-panel="' + panel + '"]'));
      });
    }
  }

  function highlight(el) {
    if (!el) return;
    el.classList.add("panel-card--updated");
    setTimeout(function () { el.classList.remove("panel-card--updated"); }, 600);
  }

  function start() {
    var panels = panelsOnPage();
    if (!panels.length) return;
    var url = "/events?panels=" + encodeURIComponent(panels.join(","));
    var es = new EventSource(url);
    var downTimer = null;

    es.addEventListener("open", function () {
      setDot("open", "live updates connected");
      setPaused(false);
      if (downTimer) { clearTimeout(downTimer); downTimer = null; }
    });
    es.addEventListener("panel_update", function (e) {
      var data = {};
      try { data = JSON.parse(e.data); } catch (err) { return; }
      if (data.panel) refetch(data.panel);
    });
    es.addEventListener("error", function () {
      setDot("reconnecting", "reconnecting…"); // EventSource auto-retries (Last-Event-ID sent)
      if (!downTimer) {
        downTimer = setTimeout(function () {
          setDot("down", "updates paused (>30s)");
          setPaused(true, "last sync");
        }, 30000);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", start);
})();
