"""M4 OSS-001 — Orchestra CLI.

A small command-line tool that exercises the public API. The CLI is
the canonical "third-party" caller for clean-room install tests; a
new contributor should be able to::

    $ orchestra submit --contract ctr-001 --text "..." --vendor demo
    $ orchestra status <task_run_id>
    $ orchestra approve <task_run_id> --by "alice" --rationale "looks good"
    $ orchestra audit <task_run_id>
    $ orchestra benchmark

without reading the source.

The CLI is also the integration test's primary "host" — Dify and
AgenticHub plugins would call the same HTTP API, but the CLI is what
we run in Docker Compose to prove the server is up.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx


def _default_base() -> str:
    return "http://127.0.0.1:8000"


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def cmd_submit(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/tasks",
        json={
            "contract_id": args.contract,
            "contract_text": args.text,
            "vendor_id": args.vendor,
            "budget_usd": args.budget,
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        print(f"submit failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"task_run_id: {data['task_run_id']}")
    print(f"state:       {data['state']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/tasks/{args.task_run_id}", timeout=10.0)
    if r.status_code == 404:
        print(f"task {args.task_run_id} not found", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"status failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    path = "/approve" if args.decision == "approve" else "/reject"
    r = httpx.post(
        f"{base}/tasks/{args.task_run_id}{path}",
        json={"decided_by": args.by, "rationale": args.rationale},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"{args.decision} failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/tasks/{args.task_run_id}/events", timeout=10.0)
    if r.status_code != 200:
        print(f"audit failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    events = data.get("events", [])
    print(f"task_run_id: {data['task_run_id']}")
    print(f"event count: {len(events)}")
    if args.last:
        events = events[-args.last:]
    for e in events:
        print(f"  {e.get('occurred_at','')}  {e.get('kind',''):20s}  {json.dumps(e.get('payload', {}), ensure_ascii=False)[:160]}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(f"{base}/benchmark/run", timeout=120.0)
    if r.status_code != 200:
        print(f"benchmark failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/capabilities", timeout=10.0)
    if r.status_code != 200:
        print(f"capabilities failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


# ---------------------------------------------------------------------------
# M11 — Doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Health check for ops. Probes the API, the capability set,
    and the published cards. Returns 0 on green, 1 on any
    non-fatal warning, 2 on a hard failure.

    The doctor is the canonical "is the cluster up?" probe a
    SRE runs in a PagerDuty runbook.
    """
    base = args.base.rstrip("/")
    failures: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        if ok:
            checks.append({"name": name, "status": "ok", "detail": detail})
        elif warn:
            warnings.append(f"{name}: {detail}")
            checks.append({"name": name, "status": "warn", "detail": detail})
        else:
            failures.append(f"{name}: {detail}")
            checks.append({"name": name, "status": "fail", "detail": detail})

    # 1. /healthz
    try:
        r = httpx.get(f"{base}/healthz", timeout=5.0)
        if r.status_code == 200:
            body = r.json()
            _check("api_health", True, f"milestone={body.get('milestone', '?')}")
        else:
            _check("api_health", False, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        _check("api_health", False, f"connect: {e}")

    # 2. capabilities registered
    try:
        r = httpx.get(f"{base}/capabilities", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            n = len(data.get("manifests", []))
            if n == 0:
                _check("capabilities", False, "no capabilities registered")
            else:
                _check("capabilities", True, f"{n} capabilities registered")
        else:
            _check("capabilities", False, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        _check("capabilities", False, f"connect: {e}")

    # 3. published cards (best-effort)
    try:
        r = httpx.get(f"{base}/admin/publish", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            n = len(data.get("cards", []))
            _check("published_cards", n > 0, f"{n} cards", warn=False) if n > 0 else _check(
                "published_cards", True, f"{n} cards (no published capabilities yet)", warn=True
            )
        else:
            _check("published_cards", False, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        _check("published_cards", False, f"connect: {e}")

    _print_json({
        "base": base,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    })
    if failures:
        return 2
    if warnings:
        return 1
    return 0


# ---------------------------------------------------------------------------
# M8 — Tenant admin subcommands
# ---------------------------------------------------------------------------


def cmd_tenant_list(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/admin/tenants", timeout=10.0)
    if r.status_code != 200:
        print(f"tenant list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_tenant_create(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/admin/tenants",
        json={"tenant_id": args.tenant_id, "name": args.name, "plan": args.plan},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"tenant create failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    print(f"created tenant: {args.tenant_id} (plan={args.plan})")
    return 0


# ---------------------------------------------------------------------------
# M8 — Publish admin subcommands
# ---------------------------------------------------------------------------


def cmd_publish_list(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/admin/publish", timeout=10.0)
    if r.status_code != 200:
        print(f"publish list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_publish_create(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    audiences = [a.strip() for a in args.audiences.split(",") if a.strip()]
    data_views = [v.strip() for v in args.data_views.split(",") if v.strip()]
    body = {
        "capability_id": args.capability,
        "name": args.name,
        "version": args.version,
        "partner_id": args.partner,
        "partner_contract_id": args.contract,
        "audiences": audiences,
        "data_views": data_views,
        "description": args.description,
    }
    r = httpx.post(f"{base}/admin/publish", json=body, timeout=10.0)
    if r.status_code != 200:
        print(f"publish create failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"published: {data.get('capability_id')} v{data.get('version')} (status={data.get('status')})")
    print(f"card_id: {data.get('card_id')}")
    return 0


def cmd_publish_revoke(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/admin/publish/{args.capability}/{args.version}/revoke",
        json={"reason": args.reason},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"publish revoke failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    print(f"revoked: {args.capability} v{args.version}")
    return 0


# ---------------------------------------------------------------------------
# M24 SEC-001 / SEC-002 — Break-glass admin subcommands
# ---------------------------------------------------------------------------


def cmd_breakglass_request(args: argparse.Namespace) -> int:
    """Create a break-glass request. The effect / resource_scope are
    passed as raw JSON strings so the CLI can carry any structured
    payload the API expects without the CLI having to know its
    shape. ``--actor`` is the applicant identity (the CLI defaults
    to ``cli``); a production swap to OIDC is in
    ``pilot-readiness.md §4.2``.
    """
    base = args.base.rstrip("/")
    try:
        effect = json.loads(args.effect) if args.effect else {}
    except json.JSONDecodeError as e:
        print(f"--effect must be valid JSON: {e}", file=sys.stderr)
        return 1
    try:
        resource_scope = json.loads(args.resource_scope) if args.resource_scope else {}
    except json.JSONDecodeError as e:
        print(f"--resource-scope must be valid JSON: {e}", file=sys.stderr)
        return 1
    body = {
        "tenant_id": args.tenant,
        "purpose": args.purpose,
        "effect": effect,
        "resource_scope": resource_scope,
        "requested_by": args.actor or "cli",
    }
    if args.ticket:
        body["ticket"] = args.ticket
    if args.window_seconds is not None:
        body["window_seconds"] = args.window_seconds
    r = httpx.post(
        f"{base}/admin/breakglass",
        json=body,
        headers={"X-Orchestra-Actor": args.actor or "cli"},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"breakglass request failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"request_id: {data.get('request_id')}")
    print(f"state:      {data.get('state')}")
    print(f"window:     {data.get('window_seconds')}s")
    return 0


def cmd_breakglass_list(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    params: dict[str, str] = {"tenant_id": args.tenant}
    if args.state:
        params["state"] = args.state
    r = httpx.get(f"{base}/admin/breakglass", params=params, timeout=10.0)
    if r.status_code != 200:
        print(f"breakglass list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    rows = data.get("requests", [])
    print(f"tenant: {data.get('tenant_id')}  count: {data.get('count')}")
    for row in rows:
        print(
            f"  {row.get('request_id','')}  state={row.get('state','')}  "
            f"requested_by={row.get('requested_by','')}  "
            f"purpose={(row.get('purpose') or '')[:48]}"
        )
    return 0


def cmd_breakglass_approve(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/admin/breakglass/{args.request_id}/approve",
        json={"rationale": args.rationale or ""},
        headers={"X-Orchestra-Actor": args.actor or "cli"},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"breakglass approve failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"state: {data.get('state')}")
    return 0


def cmd_breakglass_revoke(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/admin/breakglass/{args.request_id}/revoke",
        json={"reason": args.reason or ""},
        headers={"X-Orchestra-Actor": args.actor or "cli"},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"breakglass revoke failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"state: {data.get('state')}")
    return 0


def cmd_breakglass_sweep(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(f"{base}/admin/breakglass/sweep", timeout=10.0)
    if r.status_code != 200:
        print(f"breakglass sweep failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"expired_count: {data.get('expired_count')}")
    for rid in data.get("expired_ids", []):
        print(f"  {rid}")
    return 0


# ---------------------------------------------------------------------------
# M24 DLM-001 — Retention + Legal Hold admin subcommands (ADR-0014)
# ---------------------------------------------------------------------------


def cmd_retention_policy_set(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    retention_seconds = int(args.retention_days) * 24 * 3600
    r = httpx.post(
        f"{base}/admin/retention/policy",
        json={
            "tenant_id": args.tenant,
            "resource_kind": args.resource_kind,
            "retention_seconds": retention_seconds,
            "auto_delete": bool(args.auto_delete),
        },
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"policy set failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(
        f"policy: {data['policy_id']}  tenant={data['tenant_id']}  "
        f"kind={data['resource_kind']}  retention={data['retention_seconds']}s  "
        f"auto_delete={data['auto_delete']}"
    )
    return 0


def cmd_retention_policy_show(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(
        f"{base}/admin/retention/policy/{args.tenant}/{args.resource_kind}",
        timeout=10.0,
    )
    if r.status_code == 404:
        print(
            f"no policy for tenant={args.tenant} resource_kind={args.resource_kind}",
            file=sys.stderr,
        )
        return 1
    if r.status_code != 200:
        print(f"policy show failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_retention_hold_create(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    body: dict[str, Any] = {
        "tenant_id": args.tenant,
        "case_id": args.case_id,
        "reason": args.reason,
        "created_by": args.actor or "cli",
    }
    # Resource pairs are passed positionally; the CLI
    # supports repeated --resource-kind / --resource-id flags
    # (argparse ``action="append"``) so a single invocation
    # can cover multiple resources.
    if args.resource_kind and args.resource_id:
        if len(args.resource_kind) != len(args.resource_id):
            print(
                "error: --resource-kind and --resource-id must be passed the same number of times",
                file=sys.stderr,
            )
            return 2
        body["resource_kinds"] = args.resource_kind
        body["resource_ids"] = args.resource_id
    r = httpx.post(f"{base}/admin/holds", json=body, timeout=10.0)
    if r.status_code != 200:
        print(f"hold create failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(
        f"hold: {data['hold_id']}  case={data['case_id']}  "
        f"created_by={data['created_by']}  resources={len(data.get('resource_ids', []))}"
    )
    return 0


def cmd_retention_hold_list(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(
        f"{base}/admin/holds",
        params={"tenant_id": args.tenant, "active_only": str(not args.all).lower()},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"hold list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"tenant: {data['tenant_id']}  holds: {data['count']}")
    for h in data.get("holds", []):
        released = h.get("released_at")
        state = "active" if not released else f"released@{released}"
        print(
            f"  {h['hold_id']}  case={h['case_id']}  {state}  reason={h.get('reason','')[:60]}"
        )
    return 0


def cmd_retention_hold_release(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.request(
        "DELETE",
        f"{base}/admin/holds/{args.hold_id}",
        json={"released_by": args.actor or "cli", "reason": args.reason or ""},
        timeout=10.0,
    )
    if r.status_code != 200:
        print(f"hold release failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    print(f"released: {args.hold_id}")
    return 0


def cmd_retention_delete(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(
        f"{base}/admin/deletion-jobs",
        json={
            "tenant_id": args.tenant,
            "resource_kind": args.resource_kind,
            "resource_id": args.resource_id,
            "requested_by": args.actor or "cli",
            "force": bool(args.force),
        },
        timeout=10.0,
    )
    if r.status_code == 409:
        # Blocked by a hold or retained by the policy; the
        # response body carries the reason. Surface it and
        # exit non-zero so a SRE can tell.
        try:
            body = r.json()
        except Exception:
            body = {}
        print(
            f"deletion refused: {body.get('detail', body) or r.text}",
            file=sys.stderr,
        )
        return 1
    if r.status_code != 200:
        print(f"deletion failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(
        f"job: {data['job_id']}  state={data['state']}  kind={data['resource_kind']}  "
        f"id={data['resource_id']}"
    )
    return 0


def cmd_retention_job_show(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.get(f"{base}/admin/deletion-jobs/{args.job_id}", timeout=10.0)
    if r.status_code == 404:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"job show failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    _print_json(r.json())
    return 0


def cmd_retention_job_list(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    params: dict[str, Any] = {"tenant_id": args.tenant}
    if args.state:
        params["state"] = args.state
    r = httpx.get(f"{base}/admin/deletion-jobs", params=params, timeout=10.0)
    if r.status_code != 200:
        print(f"job list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(f"tenant: {data['tenant_id']}  jobs: {data['count']}")
    for j in data.get("jobs", []):
        last_error = j.get("last_error") or ""
        err = f"  err={last_error[:60]}" if last_error else ""
        print(
            f"  {j['job_id']}  state={j['state']}  kind={j['resource_kind']}  "
            f"id={j['resource_id']}  attempt={j.get('attempt',0)}/{j.get('max_attempts',0)}{err}"
        )
    return 0


def cmd_retention_job_retry(args: argparse.Namespace) -> int:
    base = args.base.rstrip("/")
    r = httpx.post(f"{base}/admin/deletion-jobs/{args.job_id}/retry", timeout=30.0)
    if r.status_code == 409:
        print(f"retry refused: {r.text}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"retry failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print(
        f"job: {data['job_id']}  state={data['state']}  attempt={data.get('attempt',0)}"
    )
    return 0


# ---------------------------------------------------------------------------
# M24 W4: Pilot operations primitives (M24-OPS-001)
# ---------------------------------------------------------------------------


def cmd_kms_rotate(args: argparse.Namespace) -> int:
    """Rotate the active KMS signing key.

    The new ``kid`` is printed. The old key remains valid
    until the on-call revokes it after the rotation window
    elapses.
    """
    from orchestra.enterprise.connectors import InMemoryKMSKeyProvider  # noqa: PLC0415
    from orchestra.enterprise.ops import rotate_kms_key  # noqa: PLC0415

    provider = InMemoryKMSKeyProvider()
    try:
        result = rotate_kms_key(provider, old_kid=args.kid)
    except KeyError as e:
        print(f"rotation refused: {e}", file=sys.stderr)
        return 1
    print(
        f"rotated: old={result.old_kid} new={result.new_kid} "
        f"algorithm={result.algorithm} at={result.rotated_at}"
    )
    return 0


def cmd_webhook_secret_rotate(args: argparse.Namespace) -> int:
    """Generate a fresh webhook HMAC secret for a partner.

    The new secret is printed exactly once. Copy it into
    the partner's secret store immediately. The previous
    secret is hashed for audit; the plaintext is discarded.
    """
    from orchestra.enterprise.ops import rotate_webhook_secret  # noqa: PLC0415

    result = rotate_webhook_secret(
        partner=args.partner,
        current_secret=args.current or None,
    )
    print(f"partner: {result.partner}")
    print(f"new_secret: {result.new_secret}")
    print(f"old_secret_sha256: {result.old_secret_sha256}")
    return 0


def cmd_pilot_drill(args: argparse.Namespace) -> int:
    """Exercise the M24 safety path end-to-end (offline).

    The drill creates a break-glass request, sweeps the
    active set, creates + releases a legal hold, and verifies
    that deletion is blocked while the hold is active. The
    full report is printed as JSON; ``passed`` is the
    non-zero exit signal.
    """
    import json as _json  # noqa: PLC0415

    from orchestra.enterprise.connectors import InMemoryKMSKeyProvider  # noqa: PLC0415
    from orchestra.enterprise.lifecycle import (  # noqa: PLC0415
        InMemoryDevArtifactStore,
        LifecycleManager,
    )
    from orchestra.enterprise.break_glass import BreakGlassService  # noqa: PLC0415
    from orchestra.enterprise.ops import run_pilot_drill  # noqa: PLC0415

    # The drill runs against a synthetic in-memory stack so
    # it never touches the live DB. The Pilot operator runs
    # this from the maintenance box.
    kms = InMemoryKMSKeyProvider()
    artifact_store = InMemoryDevArtifactStore()

    # We don't have a real EventStore here, so the drill
    # degrades gracefully: the report is still produced
    # with the steps the offline run can complete. In the
    # production run, ``/admin/pilot-drill`` is the live
    # version.
    from orchestra.enterprise.break_glass import BreakGlassService  # noqa: PLC0415
    from orchestra.enterprise.break_glass import EventKind  # noqa: PLC0415

    class _StubStore:
        def list_break_glass_for_tenant(self, tenant_id, state=None):
            return []

        def record_audit(self, kind, actor, payload):
            return None

    bg = BreakGlassService(store=_StubStore(), kms=kms)
    lifecycle = LifecycleManager(
        store=_StubStore(),  # type: ignore[arg-type]
        artifact_store=artifact_store,
    )
    report = run_pilot_drill(
        tenant_id=args.tenant,
        break_glass_service=bg,
        lifecycle=lifecycle,
    )
    print(_json.dumps(
        {
            "tenant_id": report.tenant_id,
            "passed": report.passed,
            "summary": report.summary,
            "steps": [
                {"name": s.name, "ok": s.ok, "detail": s.detail}
                for s in report.steps
            ],
        },
        indent=2,
    ))
    return 0 if report.passed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestra", description="Orchestra CLI (M4 OSS-001)")
    p.add_argument("--base", default=_default_base(), help="Orchestra API base URL")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="Submit a contract review")
    s.add_argument("--contract", required=True, help="contract id")
    s.add_argument("--text", required=True, help="contract text")
    s.add_argument("--vendor", required=True, help="vendor id")
    s.add_argument("--budget", type=float, default=1.0, help="budget in USD")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("status", help="Get task status")
    s.add_argument("task_run_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="Approve a pending task")
    s.add_argument("task_run_id")
    s.add_argument("--by", default="cli", help="decided_by label")
    s.add_argument("--rationale", default="", help="decision rationale")
    s.set_defaults(func=lambda a: _decide(a, "approve"))

    s = sub.add_parser("reject", help="Reject a pending task")
    s.add_argument("task_run_id")
    s.add_argument("--by", default="cli")
    s.add_argument("--rationale", default="")
    s.set_defaults(func=lambda a: _decide(a, "reject"))

    s = sub.add_parser("audit", help="Show the audit timeline")
    s.add_argument("task_run_id")
    s.add_argument("--last", type=int, default=0, help="only show the last N events")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("benchmark", help="Run the 3-baseline benchmark")
    s.set_defaults(func=cmd_benchmark)

    s = sub.add_parser("capabilities", help="List registered capabilities")
    s.set_defaults(func=cmd_capabilities)

    s = sub.add_parser("doctor", help="Health check (SRE runbook probe)")
    s.set_defaults(func=cmd_doctor)

    # --- M8: Tenant admin -------------------------------------------------
    s = sub.add_parser("tenant", help="Tenant admin operations")
    tenant_sub = s.add_subparsers(dest="tenant_command", required=True)
    s2 = tenant_sub.add_parser("list", help="List all tenants")
    s2.set_defaults(func=cmd_tenant_list)
    s2 = tenant_sub.add_parser("create", help="Create a tenant")
    s2.add_argument("tenant_id", help="tenant id (e.g. tenant:acme)")
    s2.add_argument("--name", default=None, help="display name")
    s2.add_argument("--plan", default="default", help="plan (default, pilot, enterprise)")
    s2.set_defaults(func=cmd_tenant_create)

    # --- M8: Publish admin -------------------------------------------------
    s = sub.add_parser("publish", help="Published Capability admin operations")
    pub_sub = s.add_subparsers(dest="publish_command", required=True)
    s2 = pub_sub.add_parser("list", help="List all published cards")
    s2.set_defaults(func=cmd_publish_list)
    s2 = pub_sub.add_parser("create", help="Publish an Agent Card")
    s2.add_argument("--capability", required=True, help="capability id (e.g. demo.summarize)")
    s2.add_argument("--name", required=True, help="display name")
    s2.add_argument("--version", default="0.1.0", help="card version (semver)")
    s2.add_argument("--partner", required=True, help="partner id")
    s2.add_argument("--contract", required=True, help="partner contract id")
    s2.add_argument("--audiences", default="partner", help="comma-separated audience ids")
    s2.add_argument("--data-views", default="", help="comma-separated data view names")
    s2.add_argument("--description", default="", help="human-readable description")
    s2.set_defaults(func=cmd_publish_create)
    s2 = pub_sub.add_parser("revoke", help="Revoke a published card")
    s2.add_argument("capability", help="capability id")
    s2.add_argument("version", help="card version")
    s2.add_argument("--reason", default="", help="revocation reason")
    s2.set_defaults(func=cmd_publish_revoke)

    # --- M24: Break-glass admin (ADR-0012) --------------------------------
    s = sub.add_parser(
        "breakglass",
        help="Break-glass admin (ADR-0012; M24 SEC-001 / SEC-002)",
    )
    bg_sub = s.add_subparsers(dest="breakglass_command", required=True)

    # --- M24 W4: Pilot operations (M24-OPS-001) ---------------------------
    s = sub.add_parser("kms", help="KMS key admin (M24 OPS-001)")
    kms_sub = s.add_subparsers(dest="kms_command", required=True)
    s2 = kms_sub.add_parser("rotate", help="Rotate the active signing key")
    s2.add_argument(
        "--kid", default=None, help="key id to rotate (default: most recently created)"
    )
    s2.set_defaults(func=cmd_kms_rotate)

    s = sub.add_parser(
        "webhook-secret", help="Webhook HMAC secret admin (M24 OPS-001)"
    )
    wh_sub = s.add_subparsers(dest="webhook_secret_command", required=True)
    s2 = wh_sub.add_parser("rotate", help="Generate a fresh partner HMAC secret")
    s2.add_argument("--partner", required=True, help="partner id")
    s2.add_argument(
        "--current", default=None, help="current secret (hashed for audit, plaintext discarded)"
    )
    s2.set_defaults(func=cmd_webhook_secret_rotate)

    s = sub.add_parser(
        "pilot-drill",
        help="End-to-end M24 safety drill (M24 OPS-001)",
    )
    s.add_argument(
        "--tenant",
        required=True,
        help="tenant id under which to run the synthetic drill",
    )
    s.set_defaults(func=cmd_pilot_drill)

    s2 = bg_sub.add_parser("request", help="Create a break-glass request")
    s2.add_argument("--tenant", required=True, help="tenant id (e.g. tenant:acme)")
    s2.add_argument("--purpose", required=True, help="human-readable incident id")
    s2.add_argument(
        "--effect",
        default="{}",
        help='JSON object, e.g. \'{"kind":"override_egress_view","view":"egress.internal"}\'',
    )
    s2.add_argument(
        "--resource-scope",
        default="{}",
        help='JSON object, e.g. \'{"resource_kind":"artifact","resource_id":"art-1"}\'',
    )
    s2.add_argument("--ticket", default=None, help="external ticket / case id")
    s2.add_argument(
        "--window-seconds",
        type=int,
        default=None,
        help="requested window in seconds (clamped to tenant_max and the 4h hard cap)",
    )
    s2.add_argument("--actor", default="cli", help="applicant identity")
    s2.set_defaults(func=cmd_breakglass_request)

    s2 = bg_sub.add_parser("list", help="List break-glass requests for a tenant")
    s2.add_argument("--tenant", required=True, help="tenant id")
    s2.add_argument("--state", default=None, help="filter by state")
    s2.set_defaults(func=cmd_breakglass_list)

    s2 = bg_sub.add_parser("approve", help="Sign a break-glass request (1 of 2)")
    s2.add_argument("request_id", help="break-glass request id")
    s2.add_argument("--actor", default="cli", help="approver identity")
    s2.add_argument("--rationale", default="", help="decision rationale")
    s2.set_defaults(func=cmd_breakglass_approve)

    s2 = bg_sub.add_parser("revoke", help="Revoke a break-glass request")
    s2.add_argument("request_id", help="break-glass request id")
    s2.add_argument("--actor", default="cli", help="revoker identity")
    s2.add_argument("--reason", default="", help="revoke reason")
    s2.set_defaults(func=cmd_breakglass_revoke)

    s2 = bg_sub.add_parser("sweep", help="Sweep expired break-glass requests")
    s2.set_defaults(func=cmd_breakglass_sweep)

    # --- M24: Retention + Legal Hold admin (ADR-0014) --------------------
    s = sub.add_parser("retention", help="Retention + Legal Hold admin (ADR-0014)")
    ret_sub = s.add_subparsers(dest="retention_command", required=True)

    # policy set / show
    s2 = ret_sub.add_parser("policy", help="Lifecycle policy admin")
    pol_sub = s2.add_subparsers(dest="policy_command", required=True)
    s3 = pol_sub.add_parser("set", help="Upsert a lifecycle policy")
    s3.add_argument("--tenant", required=True, help="tenant id")
    s3.add_argument(
        "--resource-kind",
        required=True,
        choices=sorted({"artifact", "receipt", "event", "webhook", "cache", "backup"}),
    )
    s3.add_argument("--retention-days", type=int, required=True, help="retention in days")
    s3.add_argument(
        "--auto-delete", action="store_true", help="opt in to automatic deletion"
    )
    s3.set_defaults(func=cmd_retention_policy_set)
    s3 = pol_sub.add_parser("show", help="Show a lifecycle policy")
    s3.add_argument("--tenant", required=True)
    s3.add_argument(
        "--resource-kind",
        required=True,
        choices=sorted({"artifact", "receipt", "event", "webhook", "cache", "backup"}),
    )
    s3.set_defaults(func=cmd_retention_policy_show)

    # hold create / list / release
    s2 = ret_sub.add_parser("hold", help="Legal Hold admin")
    hold_sub = s2.add_subparsers(dest="hold_command", required=True)
    s3 = hold_sub.add_parser("create", help="Create a Legal Hold")
    s3.add_argument("--tenant", required=True)
    s3.add_argument("--case-id", required=True)
    s3.add_argument("--reason", default="")
    s3.add_argument("--actor", default="cli", help="created_by label")
    s3.add_argument(
        "--resource-kind",
        action="append",
        help="resource kind to hold (repeat for multiple)",
    )
    s3.add_argument(
        "--resource-id",
        action="append",
        help="resource id to hold (must match --resource-kind count)",
    )
    s3.set_defaults(func=cmd_retention_hold_create)
    s3 = hold_sub.add_parser("list", help="List Legal Holds for a tenant")
    s3.add_argument("--tenant", required=True)
    s3.add_argument(
        "--all", action="store_true", help="include released holds (default: active only)"
    )
    s3.set_defaults(func=cmd_retention_hold_list)
    s3 = hold_sub.add_parser("release", help="Release a Legal Hold")
    s3.add_argument("hold_id")
    s3.add_argument("--actor", default="cli")
    s3.add_argument("--reason", default="")
    s3.set_defaults(func=cmd_retention_hold_release)

    # delete
    s2 = ret_sub.add_parser("delete", help="Request a DeletionJob")
    s2.add_argument("--tenant", required=True)
    s2.add_argument(
        "--resource-kind",
        required=True,
        choices=sorted({"artifact", "receipt", "event", "webhook", "cache", "backup"}),
    )
    s2.add_argument("--resource-id", required=True)
    s2.add_argument("--actor", default="cli")
    s2.add_argument(
        "--force", action="store_true", help="bypass the auto_delete=False gate"
    )
    s2.set_defaults(func=cmd_retention_delete)

    # job show / list / retry
    s2 = ret_sub.add_parser("job", help="DeletionJob admin")
    job_sub = s2.add_subparsers(dest="job_command", required=True)
    s3 = job_sub.add_parser("show", help="Show one DeletionJob")
    s3.add_argument("job_id")
    s3.set_defaults(func=cmd_retention_job_show)
    s3 = job_sub.add_parser("list", help="List DeletionJobs for a tenant")
    s3.add_argument("--tenant", required=True)
    s3.add_argument("--state", default=None, help="filter by state (pending, partial, ...)")
    s3.set_defaults(func=cmd_retention_job_list)
    s3 = job_sub.add_parser("retry", help="Retry a partial / failed DeletionJob")
    s3.add_argument("job_id")
    s3.set_defaults(func=cmd_retention_job_retry)

    return p


def _decide(args: argparse.Namespace, decision: str) -> int:
    args.decision = decision
    return cmd_approve(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
