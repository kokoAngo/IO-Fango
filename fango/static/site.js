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

// Copy-to-clipboard — any `.copy-btn[data-copy="#selector"]` copies the
// textContent of the referenced element and flashes a confirmation. Delegated
// so it works for buttons rendered on any page (home prompt, claim code, …).
(function () {
  // Fallback for non-secure origins (plain-HTTP / LAN-IP / http tunnels) where
  // navigator.clipboard is unavailable. Must run synchronously inside the click
  // gesture. The textarea has to be *actually rendered* (a real on-screen 1px
  // box, NOT display:none / opacity:0 / off-screen) or some browsers report
  // execCommand("copy")===true while copying nothing — which is exactly the
  // "style changes but clipboard is empty" bug.
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.contentEditable = "true";
    ta.style.position = "fixed";
    ta.style.left = "0";
    ta.style.top = "0";
    ta.style.width = "1px";
    ta.style.height = "1px";
    ta.style.padding = "0";
    ta.style.border = "none";
    ta.style.outline = "none";
    ta.style.boxShadow = "none";
    ta.style.background = "transparent";
    document.body.appendChild(ta);
    var prevScroll = window.scrollY;
    ta.focus();
    ta.select();
    try { ta.setSelectionRange(0, text.length); } catch (_) {}
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (_) {}
    document.body.removeChild(ta);
    window.scrollTo(0, prevScroll);
    return ok;
  }
  function copyText(text) {
    // Secure origin (https / localhost): the async Clipboard API is reliable.
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; },
                                                      function () { return legacyCopy(text); });
    }
    // Insecure origin: skip the (unavailable / unreliable) Clipboard API and go
    // straight to execCommand while the user gesture is still live.
    return Promise.resolve(legacyCopy(text));
  }
  document.addEventListener("click", async function (e) {
    var btn = e.target.closest && e.target.closest(".copy-btn[data-copy]");
    if (!btn) return;
    var target = document.querySelector(btn.dataset.copy);
    if (!target) return;
    var text = target.textContent.trim();
    var orig = btn.dataset.label || btn.textContent;
    btn.dataset.label = orig;
    var ok = await copyText(text);
    if (ok) {
      btn.textContent = "コピーしました ✓";
      btn.classList.add("copied");
    } else {
      // Last resort: select the text in place so the user can ⌘/Ctrl+C manually.
      try {
        var sel = window.getSelection();
        var range = document.createRange();
        range.selectNodeContents(target);
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (_) {}
      btn.textContent = "⌘/Ctrl+C でコピー";
    }
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(function () {
      btn.textContent = orig;
      btn.classList.remove("copied");
    }, 1800);
  });
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

// SSE on a forum index — live "実況": prepend new posts as they arrive.
(function () {
  if (!window.EventSource) return;
  var container = document.getElementById("live-posts");
  if (!container) return;
  var forum = container.getAttribute("data-forum");
  if (!forum) return;
  var seen = {};
  var es = new EventSource("/" + forum + "/stream");
  function onPost(e) {
    var data;
    try { data = JSON.parse(e.data); } catch (_) { return; }
    var pid = data && data.post_id;
    if (!pid || seen[pid]) return;
    seen[pid] = 1;
    fetch("/" + forum + "/api/post/" + pid)
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        var tmp = document.createElement("div");
        tmp.innerHTML = html.trim();
        var node = tmp.firstElementChild;
        if (!node) return;
        node.classList.add("live-new");
        container.insertBefore(node, container.firstChild);
        // Cap the live list so a long watch session can't grow the DOM
        // unbounded — each prepend would otherwise reflow an ever-longer list.
        while (container.children.length > 40) {
          container.removeChild(container.lastElementChild);
        }
      })
      .catch(function () {});
  }
  es.addEventListener("new_post", onPost);
  es.addEventListener("new_thread", onPost);
  window.addEventListener("pagehide", function () { es.close(); });
})();
