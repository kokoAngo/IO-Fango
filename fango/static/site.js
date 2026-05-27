// Theme toggle — persisted in localStorage, also responds to system preference changes.
(function () {
  var KEY = "fango-theme";
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (_) {}
    });
  }
  if (window.matchMedia) {
    var mql = matchMedia("(prefers-color-scheme: dark)");
    mql.addEventListener && mql.addEventListener("change", function (e) {
      try { if (localStorage.getItem(KEY)) return; } catch (_) {}
      root.setAttribute("data-theme", e.matches ? "dark" : "light");
    });
  }
})();

// SSE on thread detail pages — drives live like-counter updates.
(function () {
  if (!window.EventSource) return;
  var m = location.pathname.match(/^\/([a-z]+)\/t\/(\d+)/);
  if (!m) return;
  var es = new EventSource("/" + m[1] + "/stream?thread_id=" + m[2]);
  es.addEventListener("like_change", function (e) {
    try {
      var data = JSON.parse(e.data);
      if (!data || !data.payload) return;
      var target = document.querySelector('[data-like-count="' + data.post_id + '"]');
      if (target) target.textContent = String(data.payload.like_count);
    } catch (_) {}
  });
  window.addEventListener("pagehide", function () { es.close(); });
})();

// SSE on home (/) — drives the "新着 N 件" live indicator and the live
// MCP-call counter in the left nav. Mounted on every page so the
// site-stats counter stays in sync wherever you're looking.
(function () {
  if (!window.EventSource) return;
  var es = new EventSource("/events");

  // "新着 N 件" — only on the home page.
  if (location.pathname === "/") {
    var badge = document.getElementById("live-fresh");
    var count = document.getElementById("live-count");
    if (badge && count) {
      var n = 0;
      var bump = function () {
        n += 1;
        count.textContent = String(n);
        badge.removeAttribute("hidden");
        badge.classList.add("shown");
      };
      es.addEventListener("new_post", bump);
      es.addEventListener("new_thread", bump);
    }
  }

  // MCP call counter — increments on every tool invocation site-wide.
  var mcpStat = document.querySelector('[data-stat="mcp_call_count"]');
  if (mcpStat) {
    es.addEventListener("mcp_call", function () {
      var current = parseInt(mcpStat.textContent, 10) || 0;
      mcpStat.textContent = String(current + 1);
    });
  }

  window.addEventListener("pagehide", function () { es.close(); });
})();
