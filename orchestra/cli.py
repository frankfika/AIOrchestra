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
