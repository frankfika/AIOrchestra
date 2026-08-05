"""M3 UX-001 / UX-002 — HTML templates for the Demo Console.

Plain f-string templates (no Jinja2 dependency). The data bindings are
real backend state; the templates never fabricate.
"""
from __future__ import annotations

import html
import json
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Shared stylesheet (inline, single-file friendly)
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --bg: #fafafa; --fg: #1a1a1a; --muted: #666; --accent: #1D8C80; --warn: #b95c00; --err: #b00020; --ok: #1a7f3c; --line: #e5e5e5; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; margin: 0; background: var(--bg); color: var(--fg); }
header { background: var(--accent); color: white; padding: 16px 24px; display: flex; align-items: baseline; gap: 16px; }
header h1 { margin: 0; font-size: 18px; font-weight: 600; }
header .badge { background: rgba(255,255,255,0.18); padding: 2px 8px; border-radius: 4px; font-size: 12px; }
nav.roles { display: flex; gap: 4px; margin-left: auto; }
nav.roles a { color: white; text-decoration: none; padding: 4px 10px; border-radius: 4px; font-size: 13px; opacity: 0.85; }
nav.roles a.active { background: rgba(255,255,255,0.22); opacity: 1; }
main { max-width: 1200px; margin: 24px auto; padding: 0 24px; display: grid; gap: 20px; }
.card { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 18px 22px; }
.card h2 { margin: 0 0 12px 0; font-size: 15px; font-weight: 600; letter-spacing: 0.02em; text-transform: uppercase; color: var(--muted); }
.muted { color: var(--muted); font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
code, pre { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
pre { background: #f4f4f4; border-radius: 4px; padding: 8px 10px; overflow-x: auto; }
.kv { display: grid; grid-template-columns: 180px 1fr; gap: 4px 12px; font-size: 13px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; }
.pill { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px; font-weight: 500; }
.pill.ok { background: #dff5e1; color: var(--ok); }
.pill.warn { background: #fff1d9; color: var(--warn); }
.pill.err { background: #ffe0e0; color: var(--err); }
.pill.muted { background: #efefef; color: var(--muted); }
form { display: grid; gap: 8px; max-width: 720px; }
input, textarea, select { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 4px; background: white; }
textarea { min-height: 120px; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
button { background: var(--accent); color: white; border: 0; border-radius: 4px; padding: 8px 16px; cursor: pointer; font: inherit; }
button:hover { filter: brightness(1.05); }
.empty { color: var(--muted); font-style: italic; }
footer { text-align: center; color: var(--muted); font-size: 12px; padding: 24px; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def _pill_for_state(state: str) -> str:
    s = state.lower()
    cls = "muted"
    if s in {"succeeded", "completed", "ok", "verified"}:
        cls = "ok"
    elif s in {"running", "pending", "awaiting-approval"}:
        cls = "warn"
    elif s in {"failed", "cancelled", "denied", "rejected"}:
        cls = "err"
    return f'<span class="pill {cls}">{_esc(state)}</span>'


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def render_layout(*, role: str, title: str, body_html: str, current_path: str) -> str:
    role_links = [
        ("business", "Business"),
        ("platform", "Platform"),
        ("security", "Security / Audit"),
    ]
    nav = "\n".join(
        f'<a href="/{r}" class="{"active" if r == role else ""}">{label}</a>'
        for r, label in role_links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{_esc(title)} — Orchestra Console</title>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <h1>Orchestra M3 Demo Console</h1>
    <span class="badge">hybrid-e2e</span>
    <nav class="roles">{nav}</nav>
  </header>
  <main>
    {body_html}
  </main>
  <footer>
    M3 Governed Hybrid E2E — Renderer reports what the Event Store actually says.
  </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Business view (UX-002)
# ---------------------------------------------------------------------------


def render_business_view(*, contract: str, vendor_id: str, task_run_id: str | None, task_state: str | None, node_results: dict) -> str:
    if task_run_id is None:
        form = f"""
<form method="post" action="/tasks">
  <label>Contract ID
    <input name="contract_id" value="{_esc(contract)}" />
  </label>
  <label>Vendor ID
    <input name="vendor_id" value="{_esc(vendor_id)}" />
  </label>
  <label>Contract text
    <textarea name="contract_text">{_esc(contract or '')}</textarea>
  </label>
  <label>Budget (USD)
    <input name="budget_usd" value="2.0" type="number" step="0.1" min="0" />
  </label>
  <button type="submit">Submit contract review</button>
</form>
"""
    else:
        form = f"""
<p>Task <code>{_esc(task_run_id)}</code> — state {_pill_for_state(task_state or '?')}</p>
<p><a href="/platform/{_esc(task_run_id)}">See Platform view</a> · <a href="/security/{_esc(task_run_id)}">See Audit view</a></p>
"""
    if node_results:
        rows = "\n".join(
            f"<tr><td><code>{_esc(k)}</code></td><td>{_pill_for_state('succeeded')}</td><td><pre>{_esc(json.dumps(v, ensure_ascii=False, indent=2)[:600])}</pre></td></tr>"
            for k, v in node_results.items()
        )
        results_html = f"""
<table>
  <thead><tr><th>node_id</th><th>state</th><th>output (truncated)</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""
    else:
        results_html = '<p class="empty">Submit a contract to see the result.</p>'

    return f"""
<section class="card">
  <h2>Submit Contract Review</h2>
  {form}
</section>
<section class="card">
  <h2>Result</h2>
  {results_html}
</section>
"""


# ---------------------------------------------------------------------------
# Platform view (UX-001 Route Preview + Permission View)
# ---------------------------------------------------------------------------


def render_platform_view(*, task_run_id: str, capabilities: list[dict], events: list[dict], grants: list[dict]) -> str:
    cap_rows = "\n".join(
        f"<tr><td><code>{_esc(c.get('capability_id',''))}</code></td><td>{_esc(c.get('name',''))}</td><td>{_esc(c.get('kind',''))}</td><td>{_esc(c.get('integration_level',''))}</td></tr>"
        for c in capabilities
    )
    routing_events = [e for e in events if e.get("kind") in {"routing.decision", "node.started", "grant.issued", "fallback.triggered"}]
    re_rows = "\n".join(
        f"<tr><td>{_esc(e.get('occurred_at',''))}</td><td><code>{_esc(e.get('kind',''))}</code></td><td><pre>{_esc(json.dumps(e.get('payload', {}), ensure_ascii=False)[:500])}</pre></td></tr>"
        for e in routing_events
    )
    grant_rows = "\n".join(
        f"<tr><td><code>{_esc(g.get('grant_id',''))[:16]}</code></td><td><code>{_esc(g.get('capability_id',''))}</code></td><td><pre>{_esc(json.dumps(g.get('data_view', {}), ensure_ascii=False))}</pre></td><td>{_esc(g.get('expires_at',''))}</td></tr>"
        for g in grants
    )
    return f"""
<section class="card">
  <h2>Route Preview — Eligible Capability Set</h2>
  <table>
    <thead><tr><th>capability_id</th><th>name</th><th>kind</th><th>integration_level</th></tr></thead>
    <tbody>{cap_rows or '<tr><td colspan="4" class="empty">no capabilities registered</td></tr>'}</tbody>
  </table>
</section>
<section class="card">
  <h2>Permission View — Routing + Grants</h2>
  <h3>Routing + node lifecycle events</h3>
  <table>
    <thead><tr><th>occurred_at</th><th>kind</th><th>payload</th></tr></thead>
    <tbody>{re_rows or '<tr><td colspan="3" class="empty">no events</td></tr>'}</tbody>
  </table>
  <h3>Node Grants (DataView, audience, expiry)</h3>
  <table>
    <thead><tr><th>grant_id</th><th>capability</th><th>data_view</th><th>expires_at</th></tr></thead>
    <tbody>{grant_rows or '<tr><td colspan="4" class="empty">no grants issued</td></tr>'}</tbody>
  </table>
</section>
"""


# ---------------------------------------------------------------------------
# Security / Audit view (UX-001 Audit Timeline + Receipts)
# ---------------------------------------------------------------------------


def render_security_view(*, task_run_id: str, events: list[dict], receipts: list[dict], approvals: list[dict]) -> str:
    # Full timeline — every AuditEvent for the task. The io.sent rows
    # show the projected digest, never the raw payload (XFR-001).
    rows = []
    for e in events:
        payload = e.get("payload", {})
        kind = e.get("kind", "")
        if kind == "io.sent" and "projected_digest" in payload:
            detail = (
                f"view={_esc(payload.get('view_name',''))} · "
                f"digest=<code>{_esc(payload.get('projected_digest',''))[:24]}…</code> · "
                f"bytes={payload.get('projected_bytes','')} · "
                f"dropped={_esc(payload.get('dropped_fields', []))}"
            )
        else:
            detail = f"<pre>{_esc(json.dumps(payload, ensure_ascii=False)[:500])}</pre>"
        rows.append(
            f"<tr><td>{_esc(e.get('occurred_at',''))}</td>"
            f"<td><code>{_esc(kind)}</code></td>"
            f"<td>{detail}</td></tr>"
        )
    rows_html = "\n".join(rows)
    rcpt_rows = "\n".join(
        f"<tr><td><code>{_esc(r.get('receipt_id',''))[:16]}</code></td>"
        f"<td><code>{_esc(r.get('node_id',''))}</code></td>"
        f"<td>{_pill_for_state('verified' if r.get('verified') else 'failed')}</td></tr>"
        for r in receipts
    )
    appr_rows = "\n".join(
        f"<tr><td><code>{_esc(a.get('node_id',''))}</code></td>"
        f"<td>{_esc(a.get('decision','pending'))}</td>"
        f"<td>{_esc(a.get('decided_by',''))}</td>"
        f"<td>{_esc(a.get('decided_at',''))}</td>"
        f"<td>{_esc(a.get('rationale',''))}</td></tr>"
        for a in approvals
    )
    return f"""
<section class="card">
  <h2>Audit Timeline — task {_esc(task_run_id)}</h2>
  <table>
    <thead><tr><th>occurred_at</th><th>kind</th><th>detail</th></tr></thead>
    <tbody>{rows_html or '<tr><td colspan="3" class="empty">no events</td></tr>'}</tbody>
  </table>
</section>
<section class="card">
  <h2>Receipts</h2>
  <table>
    <thead><tr><th>receipt_id</th><th>node_id</th><th>verified</th></tr></thead>
    <tbody>{rcpt_rows or '<tr><td colspan="3" class="empty">no receipts</td></tr>'}</tbody>
  </table>
</section>
<section class="card">
  <h2>Approvals</h2>
  <table>
    <thead><tr><th>node_id</th><th>decision</th><th>decided_by</th><th>decided_at</th><th>rationale</th></tr></thead>
    <tbody>{appr_rows or '<tr><td colspan="5" class="empty">no approval requests</td></tr>'}</tbody>
  </table>
</section>
"""
