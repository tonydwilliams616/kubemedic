"""CLI: python -m lab [--context CTX] {list,validate,break,check,reset} [ID | --all]"""
from __future__ import annotations

import argparse
import sys
import time

from . import scenarios
from .check import evaluate
from .kube import KubeError, Kubectl, ensure_safe_context


def _targets(args) -> list[scenarios.Scenario]:
    if args.all:
        return scenarios.load_all()
    if not args.scenario:
        sys.exit("give a scenario id or --all")
    return [scenarios.load(args.scenario)]


def cmd_list(_args) -> int:
    for s in scenarios.load_all():
        spec = s.spec
        print(f"{s.id:<28} {spec['category']:<11} {spec['difficulty']:<7} {spec['title']}")
    return 0


def cmd_validate(_args) -> int:
    all_s = scenarios.load_all()
    problems = [p for s in all_s for p in scenarios.validate(s)]
    for p in problems:
        print(f"ERROR {p}")
    print(f"{len(all_s)} scenarios, {len(problems)} problems")
    return 1 if problems else 0


def cmd_break(args) -> int:
    k = Kubectl(args.context)
    for s in _targets(args):
        print(f"==> breaking {s.id} in {s.namespace}")
        k.apply_namespace(s.namespace, {
            "kubemedic.io/managed": "true",
            "kubemedic.io/scenario": s.id,
        })
        if s.has_base:
            k.run("apply", "-n", s.namespace, "-f", str(s.base_file))
            k.run("rollout", "status", "-n", s.namespace, f"deployment/{s.deployment}", "--timeout=180s")
        k.run("apply", "-n", s.namespace, "-f", str(s.break_file))
    return 0


def cmd_check(args) -> int:
    k = Kubectl(args.context)
    failed = 0
    for s in _targets(args):
        deadline = time.monotonic() + args.timeout
        while True:
            pods = k.get_json("pods", "-n", s.namespace, "-l", s.selector)["items"]
            events = k.get_json("events", "-n", s.namespace)["items"]
            result = evaluate(pods, events, s.spec["observable"])
            if result.ok or time.monotonic() > deadline:
                break
            time.sleep(3)
        status = "PASS" if result.ok else "FAIL"
        print(f"{status} {s.id:<28} pod={result.pod} {result.detail}")
        failed += 0 if result.ok else 1
    return 1 if failed else 0


def cmd_reset(args) -> int:
    k = Kubectl(args.context)
    for s in _targets(args):
        print(f"==> resetting {s.id}")
        k.run("delete", "namespace", s.namespace, "--ignore-not-found", "--wait=true")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lab", description="kubemedic fault-injection lab")
    p.add_argument("--context", default="kind-kubemedic", help="kubectl context (default: kind-kubemedic)")
    p.add_argument("--allow-non-kind", action="store_true", help="permit a non-kind context (disposable clusters only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("validate")
    for name in ("break", "check", "reset"):
        sp = sub.add_parser(name)
        sp.add_argument("scenario", nargs="?")
        sp.add_argument("--all", action="store_true")
        if name == "check":
            sp.add_argument("--timeout", type=int, default=180, help="seconds to wait for the fault to appear")
    args = p.parse_args(argv)

    handlers = {"list": cmd_list, "validate": cmd_validate, "break": cmd_break,
                "check": cmd_check, "reset": cmd_reset}
    try:
        if args.cmd in ("break", "check", "reset"):
            ensure_safe_context(args.context, args.allow_non_kind)
        return handlers[args.cmd](args)
    except (KubeError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
