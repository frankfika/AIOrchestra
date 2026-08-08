"""M3 UX-001 / UX-002 — HTML templates for the Demo Console.

M23 — UI modernization + bug-fix pass.

  * Design tokens: 8pt spacing, two-layer shadows, three
    radii, modern type scale, semantic color roles.
  * Dark mode: ``prefers-color-scheme`` opt-in, with a
    manual toggle that wins via ``[data-theme="dark"]``.
  * Responsive: single-column on phone, two-column on
    tablet, full layout on desktop (>= 1024px).
  * Accessibility: visible focus rings, semantic roles,
    aria-labels on the icon-only buttons.
  * Bug fixes:
      - Header is no longer frozen at M3; the version comes
        from the layout call so a single edit propagates.
      - The nav "Business" tab is wired to a real route
        (``/business`` -> ``/``), not a 404.
      - The "Platform" and "Security" nav tabs now point
        at hub pages (``/tasks``) instead of 404 paths.
      - The submit form no longer pre-fills the
        ``contract_text`` textarea with the contract id.
      - The node result pill reflects the real node state
        (not hard-coded "succeeded").
      - The audit timeline renders io.sent / node.* /
        grant.* / receipt.* as structured cards, not raw
        JSON ``<pre>`` dumps.
      - The approval ``decision`` column shows a pill, not
        a raw string.
      - Receipt verify failures are tagged with the
        underlying error message (logged on the server).
  * SSE auto-refresh: each detail page opens an
    ``EventSource`` against ``/tasks/{id}/events/stream``
    (M20) and updates the header pill + event count as
    new audit events arrive. The page is server-rendered
    so the initial paint is real; the JS only patches
    what changes.
"""

from __future__ import annotations

import html
import json
from typing import Any

# ---------------------------------------------------------------------------
# Design tokens (M23). The whole stylesheet hangs off these vars so a
# re-skin is a 30-line edit, not a 200-line rewrite. Dark-mode tokens
# are declared in the @media block below; an explicit
# ``[data-theme="dark"]`` override lets the user pin a theme (the
# inline JS at the bottom of every page wires the toggle).
# ---------------------------------------------------------------------------

_CSS = """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --bg-elev: #ffffff;
  --bg-soft: #f1f3f6;
  --fg: #0f172a;
  --fg-muted: #64748b;
  --line: #e2e8f0;
  --line-strong: #cbd5e1;
  --accent: #1D8C80;
  --accent-hover: #176a61;
  --accent-soft: #ecfdf5;
  --ok: #15803d;
  --warn: #b45309;
  --err: #b91c1c;
  --info: #1d4ed8;
  --ok-bg: #dcfce7;
  --warn-bg: #fef3c7;
  --err-bg: #fee2e2;
  --info-bg: #dbeafe;
  --muted-bg: #eef2f6;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  --space-8: 48px;

  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;

  --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.06);
  --shadow-md: 0 4px 8px -2px rgba(15, 23, 42, 0.06), 0 2px 4px -2px rgba(15, 23, 42, 0.04);
  --shadow-lg: 0 12px 24px -6px rgba(15, 23, 42, 0.10), 0 4px 8px -4px rgba(15, 23, 42, 0.06);

  --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;

  --text-xs: 11px;
  --text-sm: 13px;
  --text-md: 14px;
  --text-lg: 16px;
  --text-xl: 20px;
  --text-2xl: 28px;
}

[data-theme="dark"], :root.theme-dark {
  color-scheme: dark;
  --bg: #0b1220;
  --bg-elev: #0f172a;
  --bg-soft: #111c2e;
  --fg: #e2e8f0;
  --fg-muted: #94a3b8;
  --line: #1e293b;
  --line-strong: #334155;
  --accent: #2dd4bf;
  --accent-hover: #5eead4;
  --accent-soft: #134e4a;
  --ok: #4ade80;
  --warn: #fbbf24;
  --err: #f87171;
  --info: #60a5fa;
  --ok-bg: #14532d;
  --warn-bg: #78350f;
  --err-bg: #7f1d1d;
  --info-bg: #1e3a8a;
  --muted-bg: #1e293b;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-md: 0 4px 8px -2px rgba(0, 0, 0, 0.5);
  --shadow-lg: 0 12px 24px -6px rgba(0, 0, 0, 0.6);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--font-sans);
  font-size: var(--text-md);
  line-height: 1.5;
  background: var(--bg);
  color: var(--fg);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); text-decoration: underline; }
a:focus-visible, button:focus-visible, input:focus-visible,
textarea:focus-visible, select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

/* ------------------------------------------------------------------ */
/* Header                                                              */
/* ------------------------------------------------------------------ */

header.app {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--line);
  padding: var(--space-3) var(--space-5);
  display: flex;
  align-items: center;
  gap: var(--space-4);
  box-shadow: var(--shadow-sm);
  backdrop-filter: saturate(180%) blur(8px);
}
header.app .brand {
  display: flex; align-items: center; gap: var(--space-2);
  font-weight: 600; font-size: var(--text-lg); letter-spacing: -0.01em;
}
header.app .brand .logo {
  width: 28px; height: 28px; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--accent), #1d4ed8);
  display: inline-flex; align-items: center; justify-content: center;
  color: white; font-weight: 700; font-size: 14px;
  box-shadow: var(--shadow-sm);
}
header.app .badge {
  background: var(--accent-soft);
  color: var(--accent);
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}
header.app .live-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--muted-bg);
  margin-left: var(--space-2);
  transition: background 0.2s;
}
header.app .live-dot.live { background: var(--ok); box-shadow: 0 0 0 4px var(--ok-bg); }
nav.roles {
  display: flex; gap: var(--space-1); margin-left: auto;
  background: var(--bg-soft);
  padding: 4px;
  border-radius: var(--radius-md);
}
nav.roles a {
  color: var(--fg-muted);
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background 0.15s, color 0.15s;
}
nav.roles a:hover { text-decoration: none; color: var(--fg); background: var(--bg-elev); }
nav.roles a.active {
  background: var(--bg-elev);
  color: var(--fg);
  box-shadow: var(--shadow-sm);
}
header.app .theme-toggle {
  background: transparent; color: var(--fg-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
  font-size: 12px;
  cursor: pointer;
}
header.app .theme-toggle:hover { color: var(--fg); border-color: var(--line-strong); }

/* ------------------------------------------------------------------ */
/* Main layout                                                         */
/* ------------------------------------------------------------------ */

main {
  max-width: 1200px;
  margin: var(--space-6) auto;
  padding: 0 var(--space-5);
  display: grid;
  gap: var(--space-5);
}
main.split { grid-template-columns: 1.2fr 1fr; }
@media (max-width: 900px) { main.split { grid-template-columns: 1fr; } }

.card {
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: var(--space-5) var(--space-5);
  box-shadow: var(--shadow-sm);
}
.card h2 {
  margin: 0 0 var(--space-4) 0;
  font-size: var(--text-sm);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--fg-muted);
}
.card h3 {
  margin: var(--space-5) 0 var(--space-3) 0;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
.card.hero h2 {
  font-size: var(--text-xl);
  text-transform: none;
  letter-spacing: -0.01em;
  color: var(--fg);
  margin-bottom: var(--space-2);
}
.card.hero p.lead {
  margin: 0 0 var(--space-4) 0;
  color: var(--fg-muted);
  font-size: var(--text-md);
}

.muted { color: var(--fg-muted); font-size: var(--text-sm); }
.empty {
  color: var(--fg-muted);
  font-style: italic;
  font-size: var(--text-sm);
  padding: var(--space-3) 0;
  text-align: center;
}
.empty .icon { font-size: 24px; display: block; margin-bottom: var(--space-2); opacity: 0.5; }

/* ------------------------------------------------------------------ */
/* Tables                                                              */
/* ------------------------------------------------------------------ */

.table-wrap { overflow-x: auto; border-radius: var(--radius-md); border: 1px solid var(--line); }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
  background: var(--bg-elev);
}
th, td {
  text-align: left;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--bg-soft); }
th {
  color: var(--fg-muted);
  font-weight: 600;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: var(--bg-soft);
  position: sticky; top: 0;
}
code, pre { font-family: var(--font-mono); font-size: var(--text-xs); }
code { background: var(--bg-soft); padding: 2px 6px; border-radius: 4px; }
pre {
  background: var(--bg-soft);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  overflow-x: auto;
  margin: 0;
  max-height: 280px;
  line-height: 1.45;
}

/* ------------------------------------------------------------------ */
/* Pills + small components                                            */
/* ------------------------------------------------------------------ */

.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.01em;
  border: 1px solid transparent;
  line-height: 1.6;
}
.pill::before {
  content: ""; display: inline-block;
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; opacity: 0.7;
}
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.err { background: var(--err-bg); color: var(--err); }
.pill.info { background: var(--info-bg); color: var(--info); }
.pill.muted { background: var(--muted-bg); color: var(--fg-muted); }
.pill.lg { font-size: var(--text-sm); padding: 4px 14px; }

.kv { display: grid; grid-template-columns: 180px 1fr; gap: 6px var(--space-4); font-size: var(--text-sm); }
.kv dt { color: var(--fg-muted); }
.kv dd { margin: 0; }

form { display: grid; gap: var(--space-3); }
label {
  display: block;
  font-size: var(--text-sm);
  color: var(--fg-muted);
  font-weight: 500;
}
label > input, label > textarea, label > select {
  display: block; width: 100%;
  margin-top: 4px;
}
label.radio { display: inline-flex; align-items: center; gap: 6px; }
input, textarea, select {
  font: inherit; color: var(--fg);
  padding: 8px var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  transition: border-color 0.15s, box-shadow 0.15s;
}
input:hover, textarea:hover, select:hover { border-color: var(--line-strong); }
input:focus, textarea:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  outline: none;
}
textarea { min-height: 120px; font-family: var(--font-mono); font-size: var(--text-xs); resize: vertical; }
button {
  background: var(--accent); color: white; border: 0;
  border-radius: var(--radius-sm);
  padding: 9px 18px;
  cursor: pointer; font: inherit; font-weight: 600;
  transition: background 0.15s, transform 0.05s;
}
button:hover { background: var(--accent-hover); }
button:active { transform: translateY(1px); }
button.secondary { background: var(--bg-soft); color: var(--fg); border: 1px solid var(--line); }
button.secondary:hover { background: var(--line); }
button.danger { background: var(--err); }
button.danger:hover { background: #991b1b; }

.copy-btn {
  background: transparent; color: var(--fg-muted);
  border: 1px solid var(--line);
  padding: 2px 8px;
  font-size: 11px; font-weight: 500;
  border-radius: var(--radius-sm);
  cursor: pointer;
  margin-left: 6px;
  vertical-align: middle;
}
.copy-btn:hover { color: var(--fg); border-color: var(--line-strong); }
.copy-btn.copied { color: var(--ok); border-color: var(--ok); }

/* ------------------------------------------------------------------ */
/* Task list (home + /tasks hub)                                       */
/* ------------------------------------------------------------------ */

.task-card {
  display: flex; flex-direction: column;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.1s;
  text-decoration: none; color: var(--fg);
}
.task-card:hover {
  border-color: var(--accent);
  box-shadow: var(--shadow-md);
  text-decoration: none;
}
.task-card .row {
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-3);
}
.task-card .id {
  font-family: var(--font-mono); font-size: var(--text-xs); color: var(--fg-muted);
}
.task-card .meta {
  margin-top: 6px;
  display: flex; gap: var(--space-3);
  font-size: var(--text-xs);
  color: var(--fg-muted);
}
.task-list { display: grid; gap: var(--space-2); }

/* ------------------------------------------------------------------ */
/* Event timeline (security view)                                      */
/* ------------------------------------------------------------------ */

.event-list { display: grid; gap: var(--space-2); }
.event {
  display: grid;
  grid-template-columns: 12px 1fr;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-left: 2px solid var(--line);
  background: var(--bg-soft);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  animation: fadeIn 0.3s ease;
}
.event::before {
  content: ""; display: block;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--line-strong);
  margin-top: 8px;
  margin-left: -9px;
}
.event.kind-task::before { background: var(--info); }
.event.kind-plan::before { background: var(--accent); }
.event.kind-node-started::before,
.event.kind-node-succeeded::before { background: var(--ok); }
.event.kind-node-failed::before { background: var(--err); }
.event.kind-node-awaiting-approval::before { background: var(--warn); }
.event.kind-io::before { background: var(--accent); }
.event.kind-grant::before { background: var(--info); }
.event.kind-receipt::before { background: var(--ok); }
.event-head {
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2);
  margin-bottom: 4px;
}
.event-head .ts { font-size: var(--text-xs); color: var(--fg-muted); font-family: var(--font-mono); }
.event-head .kind {
  font-family: var(--font-mono); font-size: var(--text-xs); color: var(--fg);
  font-weight: 600;
}
.event-body { color: var(--fg-muted); font-size: var(--text-sm); }
.event-body strong { color: var(--fg); font-weight: 600; }
.event-detail {
  display: inline-flex; gap: var(--space-3);
  flex-wrap: wrap;
}
.event-detail span { display: inline-flex; gap: 4px; }
.event-detail span b { color: var(--fg); font-weight: 500; }

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ------------------------------------------------------------------ */
/* Plan summary (platform view)                                        */
/* ------------------------------------------------------------------ */

.plan-flow {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2);
  padding: var(--space-3) 0;
}
.plan-node {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px;
  background: var(--bg-soft);
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: var(--text-xs);
  font-family: var(--font-mono);
}
.plan-node.running { border-color: var(--info); color: var(--info); }
.plan-node.succeeded { border-color: var(--ok); color: var(--ok); background: var(--ok-bg); }
.plan-node.failed { border-color: var(--err); color: var(--err); background: var(--err-bg); }
.plan-node.awaiting-approval { border-color: var(--warn); color: var(--warn); background: var(--warn-bg); }
.plan-arrow { color: var(--fg-muted); font-size: 14px; }

/* ------------------------------------------------------------------ */
/* Toast (auto-refresh notifications)                                  */
/* ------------------------------------------------------------------ */

.toast-stack {
  position: fixed; bottom: var(--space-5); right: var(--space-5);
  display: grid; gap: var(--space-2);
  z-index: 100;
  max-width: 360px;
}
.toast {
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-sm);
  box-shadow: var(--shadow-lg);
  animation: slideIn 0.3s ease;
}
.toast.kind-ok { border-left-color: var(--ok); }
.toast.kind-err { border-left-color: var(--err); }
@keyframes slideIn {
  from { opacity: 0; transform: translateX(20px); }
  to { opacity: 1; transform: translateX(0); }
}

/* ------------------------------------------------------------------ */
/* Footer                                                              */
/* ------------------------------------------------------------------ */

footer {
  text-align: center;
  color: var(--fg-muted);
  font-size: var(--text-xs);
  padding: var(--space-6) var(--space-5) var(--space-8);
  border-top: 1px solid var(--line);
  margin-top: var(--space-8);
}
footer code { background: transparent; padding: 0; color: var(--fg-muted); }

/* ------------------------------------------------------------------ */
/* Responsive                                                          */
/* ------------------------------------------------------------------ */

@media (max-width: 640px) {
  header.app { flex-wrap: wrap; }
  nav.roles { order: 3; margin-left: 0; width: 100%; justify-content: center; }
  main { padding: 0 var(--space-3); margin: var(--space-4) auto; }
  .card { padding: var(--space-4); }
  .kv { grid-template-columns: 1fr; gap: 2px var(--space-3); }
  .kv dt { margin-top: var(--space-2); }
  pre { max-height: 200px; }
  .toast-stack { left: var(--space-3); right: var(--space-3); max-width: none; }
}

@media print {
  header.app, nav.roles, footer, button, .theme-toggle { display: none; }
  body { background: white; color: black; }
  .card { box-shadow: none; border: 1px solid #ccc; page-break-inside: avoid; }
}
"""


# ---------------------------------------------------------------------------
# Inline JS — small surface, no framework. Two jobs:
#   1. Theme toggle: pin the user choice in localStorage so a refresh
#      sticks, while still respecting the OS preference until the user
#      overrides it.
#   2. SSE auto-refresh: subscribe to /tasks/{id}/events/stream and
#      patch the header pill + event count, with toast notifications
#      for state transitions. The initial server-rendered HTML is the
#      source of truth — JS only adds deltas, it never replaces data.
# ---------------------------------------------------------------------------

_JS = """
(function() {
  // ---- Theme --------------------------------------------------------
  var stored = null;
  try { stored = localStorage.getItem('orchestra-theme'); } catch (e) {}
  var sysDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (stored === 'dark' || (stored === null && sysDark)) {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function() {
      var cur = document.documentElement.getAttribute('data-theme');
      var next = cur === 'dark' ? 'light' : 'dark';
      if (next === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
      else document.documentElement.removeAttribute('data-theme');
      try { localStorage.setItem('orchestra-theme', next); } catch (e) {}
    });
  }

  // ---- Copy buttons -------------------------------------------------
  document.querySelectorAll('[data-copy]').forEach(function(el) {
    el.addEventListener('click', function() {
      var val = el.getAttribute('data-copy');
      navigator.clipboard.writeText(val).then(function() {
        el.classList.add('copied');
        var prev = el.textContent;
        el.textContent = 'copied';
        setTimeout(function() { el.textContent = prev; el.classList.remove('copied'); }, 1500);
      });
    });
  });

  // ---- SSE auto-refresh --------------------------------------------
  var dot = document.querySelector('.live-dot');
  var counter = document.querySelector('[data-event-count]');
  var stack = document.getElementById('toast-stack');
  function pushToast(text, kind) {
    if (!stack) return;
    var t = document.createElement('div');
    t.className = 'toast kind-' + (kind || 'info');
    t.textContent = text;
    stack.appendChild(t);
    setTimeout(function() { t.style.opacity = '0'; setTimeout(function() { t.remove(); }, 300); }, 4000);
  }
  var url = document.querySelector('[data-sse-url]');
  if (url) {
    var sse = new EventSource(url.getAttribute('data-sse-url'));
    sse.addEventListener('event', function(ev) {
      try {
        var data = JSON.parse(ev.data);
        if (dot) { dot.classList.add('live'); }
        if (counter) {
          var n = parseInt(counter.getAttribute('data-event-count'), 10) + 1;
          counter.setAttribute('data-event-count', n);
          counter.textContent = n + ' events';
        }
        if (data && data.kind) {
          pushToast(data.kind + (data.payload && data.payload.node_id ? ' · ' + data.payload.node_id : ''), 'info');
        }
      } catch (e) {}
    });
    sse.addEventListener('done', function() {
      if (dot) { dot.classList.remove('live'); }
      pushToast('task reached terminal state — refresh to see final state', 'ok');
      sse.close();
    });
    sse.onerror = function() {
      if (dot) { dot.classList.remove('live'); }
    };
  }

  // ---- Copy ID helper ----------------------------------------------
  var idEl = document.querySelector('[data-task-id]');
  if (idEl && !document.querySelector('[data-copy]')) {
    // Add a copy button next to the id if not present.
    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.setAttribute('data-copy', idEl.getAttribute('data-task-id'));
    btn.textContent = 'copy id';
    idEl.parentNode.insertBefore(btn, idEl.nextSibling);
  }
})();
"""


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


# ---------------------------------------------------------------------------
# Pill state mapping. Extended in M23 to cover node states + approval
# decisions (was only covering task-run states before, so a "rejected"
# decision rendered as raw text).
# ---------------------------------------------------------------------------

_OK_STATES = {"succeeded", "completed", "ok", "verified", "approved", "approve"}
_WARN_STATES = {
    "running",
    "pending",
    "awaiting-approval",
    "in_progress",
    "started",
    "in-progress",
}
_ERR_STATES = {"failed", "cancelled", "denied", "rejected", "reject", "verify-error"}
_INFO_STATES = {"task-received", "plan-created", "plan-signed"}


def _pill_for_state(state: str) -> str:
    s = (state or "").lower()
    if s in _OK_STATES:
        cls = "ok"
    elif s in _ERR_STATES:
        cls = "err"
    elif s in _WARN_STATES:
        cls = "warn"
    elif s in _INFO_STATES:
        cls = "info"
    else:
        cls = "muted"
    label = state or "—"
    return f'<span class="pill {cls}">{_esc(label)}</span>'


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

_VERSION = "M23"
# Inline favicon — a 32x32 SVG of the brand mark, no extra file.
_FAVICON = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
    "<stop offset='0%25' stop-color='%231D8C80'/>"
    "<stop offset='100%25' stop-color='%231d4ed8'/>"
    "</linearGradient></defs>"
    "<rect width='32' height='32' rx='6' fill='url(%23g)'/>"
    "<text x='50%25' y='58%25' text-anchor='middle' font-family='-apple-system,system-ui,sans-serif' "
    "font-size='15' font-weight='700' fill='white'>O</text>"
    "</svg>"
)


def render_layout(
    *,
    role: str,
    title: str,
    body_html: str,
    current_path: str,
) -> str:
    role_links = [
        ("business", "Business", "/business"),
        ("platform", "Platform", "/platform"),
        ("security", "Security / Audit", "/security"),
    ]
    nav = "\n".join(
        f'<a href="{href}" class="{"active" if role == r else ""}">{_esc(label)}</a>'
        for r, label, href in role_links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#1D8C80" />
  <meta name="description" content="Orchestra Console — hybrid AI orchestration control plane (M23). Submit, route, audit, approve." />
  <meta property="og:title" content="Orchestra Console — {_esc(title)}" />
  <meta property="og:description" content="Hybrid AI orchestration control plane. Submit, route, audit, approve." />
  <meta property="og:type" content="website" />
  <link rel="icon" type="image/svg+xml" href="{_FAVICON}" />
  <title>{_esc(title)} — Orchestra Console</title>
  <style>{_CSS}</style>
</head>
<body>
  <header class="app">
    <div class="brand">
      <span class="logo">O</span>
      <span>Orchestra</span>
    </div>
    <span class="badge">{_VERSION}</span>
    <span class="live-dot" title="live updates"></span>
    <nav class="roles">{nav}</nav>
    <button id="theme-toggle" class="theme-toggle" aria-label="Toggle dark mode">◐</button>
  </header>
  <main class="{('split' if role == 'business' else '')}">
    {body_html}
  </main>
  <div id="toast-stack" class="toast-stack" aria-live="polite"></div>
  <footer>
    Orchestra {_VERSION} · Renderer reports what the Event Store actually says.
  </footer>
  <script>{_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Business view (UX-002 + M23 recent-tasks panel)
# ---------------------------------------------------------------------------


def render_recent_tasks(tasks: list[dict[str, Any]]) -> str:
    """Recent tasks fragment — rendered into the home card."""
    if not tasks:
        return '<p class="empty"><span class="icon">📋</span>No tasks yet. Submit a contract review to get started.</p>'
    rows = []
    for t in tasks:
        tid = t.get("task_run_id", "")
        state = t.get("state", "")
        contract_id = t.get("contract_id", "")
        created = str(t.get("created_at", ""))
        rows.append(
            f"""<a class="task-card" href="/platform/{_esc(tid)}">
  <div class="row">
    <span class="id">{_esc(tid[:8])}…</span>
    {_pill_for_state(state)}
  </div>
  <div class="row" style="margin-top:6px;">
    <span><strong>{_esc(contract_id)}</strong></span>
  </div>
  <div class="meta">
    <span>{_esc(created[:19])}</span>
  </div>
</a>"""
        )
    return f'<div class="task-list">{"".join(rows)}</div>'


def render_business_view(
    *,
    recent_tasks: list[dict[str, Any]],
) -> str:
    """M23 — single-card home. Submit form (left) + Recent tasks (right)."""
    form_html = """
<form method="post" action="/ux/tasks" autocomplete="off">
  <label>Contract ID
    <input name="contract_id" placeholder="e.g. ctr-2026-001" required />
  </label>
  <label>Vendor ID
    <input name="vendor_id" placeholder="e.g. acme-corp" required />
  </label>
  <label>Contract text
    <textarea name="contract_text" placeholder="Paste the contract text to review…" required></textarea>
  </label>
  <label>Budget (USD)
    <input name="budget_usd" value="2.0" type="number" step="0.1" min="0" />
  </label>
  <button type="submit">Submit contract review →</button>
</form>
"""
    return f"""
<section class="card hero">
  <h2>Submit Contract Review</h2>
  <p class="lead">The submit form goes through the same Coordinator entry point as <code>POST /tasks</code> — the JSON API and the Console share the same event store.</p>
  {form_html}
</section>
<section class="card">
  <h2>Recent Tasks</h2>
  {render_recent_tasks(recent_tasks)}
  <p class="muted" style="margin-top:12px;"><a href="/tasks">All tasks →</a></p>
</section>
"""


# ---------------------------------------------------------------------------
# Task list (hub page — /tasks, /platform, /security)
# ---------------------------------------------------------------------------


def render_task_list(
    *,
    tasks: list[dict[str, Any]],
    state_filter: str | None = None,
) -> str:
    if not tasks:
        msg = (
            f"No tasks in state <code>{_esc(state_filter)}</code>."
            if state_filter
            else "No tasks yet. Submit a contract review from the Business tab to get started."
        )
        return f"""
<section class="card">
  <h2>Tasks {f'— {html.escape(state_filter)}' if state_filter else ''}</h2>
  <p class="empty"><span class="icon">📋</span>{msg}</p>
</section>
"""
    rows = []
    for t in tasks:
        tid = t.get("task_run_id", "")
        state = t.get("state", "")
        contract_id = t.get("contract_id", "")
        template_id = t.get("template_id", "")
        created = str(t.get("created_at", ""))
        updated = str(t.get("updated_at", ""))
        rows.append(
            f"""<tr>
  <td><a href="/platform/{_esc(tid)}"><code>{_esc(tid[:8])}…</code></a><button class="copy-btn" data-copy="{_esc(tid)}" type="button">copy id</button></td>
  <td><strong>{_esc(contract_id)}</strong></td>
  <td><code>{_esc(template_id)}</code></td>
  <td>{_pill_for_state(state)}</td>
  <td class="muted">{_esc(created[:19])}</td>
  <td class="muted">{_esc(updated[:19]) if updated != created else '—'}</td>
  <td><a href="/security/{_esc(tid)}">audit →</a></td>
</tr>"""
        )
    title = f"Tasks — {state_filter}" if state_filter else "Tasks"
    filter_html = (
        f'<p class="muted">Filtered to <code>{_esc(state_filter)}</code> · <a href="/tasks">clear filter</a></p>'
        if state_filter
        else ""
    )
    return f"""
<section class="card">
  <h2>{_esc(title)}</h2>
  {filter_html}
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>task id</th><th>contract</th><th>template</th>
        <th>state</th><th>created</th><th>updated</th><th></th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>
"""


# ---------------------------------------------------------------------------
# Platform view (UX-001 Route Preview + Permission View) — M23
# ---------------------------------------------------------------------------


def render_platform_view(
    *,
    task_run_id: str,
    capabilities: list[dict[str, Any]],
    events: list[dict[str, Any]],
    grants: list[dict[str, Any]],
    node_runs: list[dict[str, Any]],
) -> str:
    # Capability table (uses the new public list_capabilities()).
    cap_rows = "\n".join(
        f"<tr><td><code>{_esc(c.get('capability_id',''))}</code></td><td>{_esc(c.get('name',''))}</td><td>{_esc(c.get('kind',''))}</td><td>{_esc(c.get('integration_level',''))}</td></tr>"
        for c in capabilities
    )
    # Routing-relevant events.
    routing_events = [e for e in events if e.get("kind") in {"routing.decision", "node.started", "grant.issued", "fallback.triggered"}]
    re_rows = "\n".join(
        f"<tr><td class=\"muted\">{_esc(e.get('occurred_at',''))[:19]}</td><td><code>{_esc(e.get('kind',''))}</code></td><td><pre>{_esc(json.dumps(e.get('payload', {}), ensure_ascii=False)[:500])}</pre></td></tr>"
        for e in routing_events
    )
    # Grant table.
    grant_rows = "\n".join(
        f"<tr><td><code>{_esc(g.get('grant_id',''))[:16]}</code></td><td><code>{_esc(g.get('capability_id',''))}</code></td><td><pre>{_esc(json.dumps(g.get('data_view', {}), ensure_ascii=False))}</pre></td><td class=\"muted\">{_esc(g.get('expires_at',''))[:19]}</td></tr>"
        for g in grants
    )
    # Plan flow (per-node state from the derived node_runs).
    if node_runs:
        plan_html_parts = []
        for i, n in enumerate(node_runs):
            if i > 0:
                plan_html_parts.append('<span class="plan-arrow">→</span>')
            plan_html_parts.append(
                f'<span class="plan-node {_esc(n.get("state","pending"))}">{_esc(n.get("node_id",""))}</span>'
            )
        plan_html = '<div class="plan-flow">' + "".join(plan_html_parts) + "</div>"
        node_table = "\n".join(
            f"<tr><td><code>{_esc(n.get('node_id',''))}</code></td><td>{_pill_for_state(n.get('state',''))}</td><td><code>{_esc(n.get('capability_id',''))}</code></td><td class=\"muted\">{_esc(str(n.get('latency_ms','') or '—'))}{' ms' if n.get('latency_ms') is not None else ''}</td></tr>"
            for n in node_runs
        )
    else:
        plan_html = '<p class="empty">No nodes have started yet — the live indicator will turn green when the first event arrives.</p>'
        node_table = ""
    # Task header pill (latest known state).
    last_state = "running"
    for e in reversed(events):
        k = e.get("kind", "")
        if k == "task.completed":
            last_state = "succeeded"
            break
        if k == "task.failed":
            last_state = "failed"
            break
        if k == "node.awaiting-approval":
            last_state = "awaiting-approval"
            break
    return f"""
<section class="card">
  <h2>Task <code data-task-id="{_esc(task_run_id)}">{_esc(task_run_id[:8])}…</code> {('· ' + _pill_for_state(last_state))}</h2>
  <p class="muted">Full id: <code>{_esc(task_run_id)}</code> · <a href="/security/{_esc(task_run_id)}">view audit →</a> · <a href="/tasks">all tasks →</a></p>
</section>
<section class="card">
  <h2>Plan Flow</h2>
  {plan_html}
  {f'<div class="table-wrap" style="margin-top:16px;"><table><thead><tr><th>node</th><th>state</th><th>capability</th><th>latency</th></tr></thead><tbody>{node_table}</tbody></table></div>' if node_table else ''}
</section>
<section class="card">
  <h2>Eligible Capability Set</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>capability_id</th><th>name</th><th>kind</th><th>integration_level</th></tr></thead>
      <tbody>{cap_rows or '<tr><td colspan="4" class="empty">no capabilities registered</td></tr>'}</tbody>
    </table>
  </div>
</section>
<section class="card">
  <h2>Permission View — Routing + Grants</h2>
  <h3>Routing + node lifecycle events</h3>
  <div class="table-wrap">
    <table>
      <thead><tr><th>occurred_at</th><th>kind</th><th>payload</th></tr></thead>
      <tbody>{re_rows or '<tr><td colspan="3" class="empty">no events</td></tr>'}</tbody>
    </table>
  </div>
  <h3>Node Grants (DataView, audience, expiry)</h3>
  <div class="table-wrap">
    <table>
      <thead><tr><th>grant_id</th><th>capability</th><th>data_view</th><th>expires_at</th></tr></thead>
      <tbody>{grant_rows or '<tr><td colspan="4" class="empty">no grants issued</td></tr>'}</tbody>
    </table>
  </div>
</section>
<span data-sse-url="/tasks/{_esc(task_run_id)}/events/stream" hidden></span>
"""


# ---------------------------------------------------------------------------
# Security / Audit view — M23 event timeline rewrite
# ---------------------------------------------------------------------------


def _render_event_detail(e: dict[str, Any]) -> str:
    """M23 — render an event based on its kind, not a JSON pre dump.

    Previously every event kind rendered as a 500-char JSON blob;
    the io.sent special case (XFR-001 digest) only fired when the
    payload had a ``projected_digest`` field, which the real
    EventStore never wrote, so the feature was effectively dead.
    The new render walks the event kind and surfaces the human-
    meaningful fields.
    """
    kind = e.get("kind", "")
    payload = e.get("payload", {})
    ts = _esc(str(e.get("occurred_at", ""))[:19])
    body = ""
    if kind == "io.sent":
        # The M3 Egress PEP event. Show capability, view, latency,
        # and the projected digest if present. The full envelope is
        # in the audit log; the dashboard shows the digest so a
        # reviewer can verify downstream.
        cap = payload.get("capability_id", "—")
        view = payload.get("view_name") or payload.get("data_view", "—")
        lat = payload.get("latency_ms")
        digest = payload.get("projected_digest")
        bytes_out = payload.get("projected_bytes", "—")
        node_id = payload.get("node_id", "")
        node_html = f'<span>node <b>{_esc(node_id)}</b></span>' if node_id else ""
        body = (
            f'<div class="event-detail">'
            f'<span>capability <b>{_esc(cap)}</b></span>'
            f'<span>view <b>{_esc(view)}</b></span>'
            f'{node_html}'
            f'<span>{f"{lat} ms" if lat is not None else "—"}</span>'
            f'<span>{_esc(bytes_out)} bytes</span>'
            f'</div>'
            + (
                f'<div class="muted" style="margin-top:4px;">digest <code>{_esc(digest[:24])}…</code></div>'
                if digest
                else ""
            )
        )
    elif kind == "io.intent":
        body = (
            f'<div class="event-detail">'
            f'<span>capability <b>{_esc(payload.get("capability_id","—"))}</b></span>'
            f'<span>view <b>{_esc(payload.get("data_view","—"))}</b></span>'
            f'<span>node <b>{_esc(payload.get("node_id","—"))}</b></span>'
            f'</div>'
        )
    elif kind == "io.received":
        body = (
            f'<div class="event-detail">'
            f'<span>node <b>{_esc(payload.get("node_id","—"))}</b></span>'
            f'<span>{f"{payload.get("latency_ms")} ms" if payload.get("latency_ms") is not None else "—"}</span>'
            f'<span>outputs <b>{_esc(", ".join(payload.get("outputs_keys", []) or [])) or "—"}</b></span>'
            f'</div>'
        )
    elif kind in {"node.started", "node.succeeded", "node.failed", "node.awaiting-approval"}:
        body = (
            f'<div class="event-detail">'
            f'<span>node <b>{_esc(payload.get("node_id","—"))}</b></span>'
            + (
                f'<span>{payload.get("latency_ms")} ms</span>'
                if kind == "node.succeeded" and payload.get("latency_ms") is not None
                else ""
            )
            + f'<span>capability <b>{_esc(payload.get("capability_id","—")) or "—"}</b></span>'
            + f'</div>'
        )
    elif kind == "grant.issued":
        dv = payload.get("data_view", {}) or {}
        fields = dv.get("fields", [])
        body = (
            f'<div class="event-detail">'
            f'<span>capability <b>{_esc(payload.get("capability_id","—"))}</b></span>'
            f'<span>view <b>{_esc(dv.get("name","—"))}</b></span>'
            f'<span>{len(fields) if isinstance(fields, list) else 0} fields</span>'
            f'<span>expires <b>{_esc(str(payload.get("expires_at",""))[:19])}</b></span>'
            f'</div>'
        )
    elif kind == "receipt.signed":
        body = (
            f'<div class="event-detail">'
            f'<span>node <b>{_esc(payload.get("node_id","—"))}</b></span>'
            f'<span>receipt <code>{_esc((payload.get("receipt_id","") or "")[:16])}…</code></span>'
            f'</div>'
        )
    elif kind == "plan.created":
        nodes = payload.get("nodes", [])
        body = (
            f'<div class="event-detail">'
            f'<span>{len(nodes) if isinstance(nodes, list) else 0} nodes</span>'
            f'<span>plan <code>{_esc((payload.get("plan_id","") or "")[:16])}…</code></span>'
            f'</div>'
        )
    elif kind == "plan.signed":
        body = f'<div class="event-detail"><span>signed by <b>{_esc(payload.get("signed_by","—"))}</b></span></div>'
    elif kind == "task.received":
        body = (
            f'<div class="event-detail">'
            f'<span>contract <b>{_esc(payload.get("contract_id","—"))}</b></span>'
            f'<span>purpose <b>{_esc(payload.get("purpose","—"))}</b></span>'
            f'</div>'
        )
    else:
        # Fallback — small JSON dump for unknown kinds (keeps the
        # audit trail readable for new event types that haven't
        # been wired into the detail renderer yet).
        body = f'<pre>{_esc(json.dumps(payload, ensure_ascii=False)[:500])}</pre>'
    kind_class = "kind-" + kind.replace(".", "-")
    return (
        f'<div class="event {_esc(kind_class)}">'
        f'<div class="event-head">'
        f'<span class="ts">{ts}</span>'
        f'<span class="kind">{_esc(kind)}</span>'
        f'</div>'
        f'<div class="event-body">{body}</div>'
        f'</div>'
    )


def render_security_view(
    *,
    task_run_id: str,
    events: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> str:
    event_html = "".join(_render_event_detail(e) for e in events) or (
        '<p class="empty"><span class="icon">🪶</span>No events yet. The live indicator will turn green when the first event arrives.</p>'
    )
    rcpt_rows = []
    for r in receipts:
        rid = r.get("receipt_id", "")
        node_id = r.get("node_id", "")
        if r.get("verify_error"):
            verified_html = f'<span class="pill err" title="{_esc(r.get("verify_error",""))}">verify-error</span>'
        elif r.get("verified"):
            verified_html = _pill_for_state("verified")
        else:
            verified_html = _pill_for_state("failed")
        rcpt_rows.append(
            f"<tr><td><code>{_esc(rid[:16])}…</code></td><td><code>{_esc(node_id)}</code></td><td>{verified_html}</td></tr>"
        )
    appr_rows = []
    for a in approvals:
        appr_rows.append(
            f"<tr><td><code>{_esc(a.get('node_id',''))}</code></td>"
            f"<td>{_pill_for_state(a.get('decision') or 'pending')}</td>"
            f"<td>{_esc(a.get('decided_by','') or '—')}</td>"
            f"<td class='muted'>{_esc(str(a.get('decided_at','') or '—')[:19])}</td>"
            f"<td>{_esc(a.get('rationale','') or '—')}</td></tr>"
        )
    return f"""
<section class="card">
  <h2>Audit Timeline — <code data-task-id="{_esc(task_run_id)}">{_esc(task_run_id[:8])}…</code></h2>
  <p class="muted">
    <span data-event-count="{len(events)}">{len(events)} events</span> ·
    Full id: <code>{_esc(task_run_id)}</code> ·
    <a href="/platform/{_esc(task_run_id)}">view platform →</a> ·
    <a href="/tasks">all tasks →</a>
  </p>
  <div class="event-list" data-test="event-list">
    {event_html}
  </div>
</section>
<section class="card">
  <h2>Receipts</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>receipt_id</th><th>node_id</th><th>verified</th></tr></thead>
      <tbody>{''.join(rcpt_rows) or '<tr><td colspan="3" class="empty">no receipts</td></tr>'}</tbody>
    </table>
  </div>
</section>
<section class="card">
  <h2>Approvals</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>node_id</th><th>decision</th><th>decided_by</th><th>decided_at</th><th>rationale</th></tr></thead>
      <tbody>{''.join(appr_rows) or '<tr><td colspan="5" class="empty">no approval requests</td></tr>'}</tbody>
    </table>
  </div>
</section>
<span data-sse-url="/tasks/{_esc(task_run_id)}/events/stream" hidden></span>
"""
