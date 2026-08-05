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
