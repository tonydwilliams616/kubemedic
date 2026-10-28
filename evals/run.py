"""Runs the diagnoser against every broken scenario several times, scores each answer, and writes a scorecard.

Design choices:
- Fail fast if any fault isn't actually observable (run `make break-all && make check-all` first),
  so the model is never scored against a cluster that isn't broken yet.
- Gather the evidence ONCE per scenario and reuse it for every repeat. Then the only thing that
  varies between repeats is the model, which is exactly what repeats are meant to measure.
- Several repeats per scenario, because the same input can produce different answers.
- Everything is written to reports/<timestamp>-<model>/ so runs can be compared later.

Usage: python -m evals.run [--model MODEL_ID] [--repeats 3] [--scenario ID ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from kubemedic.diagnose import DEFAULT_MODEL, diagnose
from lab import scenarios
from lab.scenarios import Scenario

from .score import Score, score, summarise

REPORTS_DIR = Path("reports")


def score_row(s: Score) -> dict:
    return asdict(s) | {"root_cause_ok": s.root_cause_ok, "passed": s.passed}


def run_eval(selected: list[Scenario], model, gather_fn: Callable[[str], dict], repeats: int,
             out_dir: Path, check_fn: Callable[[Scenario], tuple[bool, str]] | None = None) -> dict:
    # 1. Every fault must be really there before we spend anything on the model.
    if check_fn:
        not_ready = [(sc.id, detail) for sc in selected for ok, detail in [check_fn(sc)] if not ok]
        if not_ready:
            raise RuntimeError("faults not observable, run `make break-all && make check-all` first: "
                               + "; ".join(f"{i}: {d}" for i, d in not_ready))

    out_dir.mkdir(parents=True, exist_ok=True)
    all_scores: list[Score] = []
    per_scenario = []
    tokens_in = tokens_out = 0
    latencies = []

    for sc in selected:
        bundle = gather_fn(sc.namespace)
        (out_dir / f"{sc.id}.bundle.json").write_text(json.dumps(bundle, indent=1))
        scores = []
        for r in range(1, repeats + 1):
            result = diagnose(bundle, model)
            s = score(result.diagnosis, sc.spec)
            scores.append(s)
            tokens_in += result.input_tokens
            tokens_out += result.output_tokens
            latencies.append(result.latency_s)
            (out_dir / f"{sc.id}.run{r}.json").write_text(
                json.dumps({"result": asdict(result), "score": score_row(s)}, indent=2))
        all_scores += scores
        per_scenario.append({
            "scenario": sc.id,
            "passed": sum(s.passed for s in scores),
            "root_cause_ok": sum(s.root_cause_ok for s in scores),
            "action_ok": sum(s.action_ok for s in scores),
            "unsafe": sum(s.unsafe for s in scores),
            "actions": sorted({s.action or "invalid" for s in scores}),
            "repeats": repeats,
        })

    summary = {
        "model": model.model_id,
        "repeats": repeats,
        "runs": len(all_scores),
        **summarise(all_scores),
        "pass_rate": round(sum(s.passed for s in all_scores) / max(len(all_scores), 1), 2),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "mean_latency_s": round(sum(latencies) / max(len(latencies), 1), 2),
        "per_scenario": per_scenario,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def print_scorecard(summary: dict) -> None:
    n = summary["repeats"]
    print(f"\nmodel: {summary['model']}   repeats: {n}")
    print(f"{'scenario':<28} {'pass':<6} {'cause':<6} {'action':<7} {'unsafe':<7} actions chosen")
    for row in summary["per_scenario"]:
        print(f"{row['scenario']:<28} {row['passed']}/{n:<4} {row['root_cause_ok']}/{n:<4} "
              f"{row['action_ok']}/{n:<5} {row['unsafe']:<7} {', '.join(row['actions'])}")
    print(f"\npass rate {summary['pass_rate']:.0%} | root cause {summary['root_cause_accuracy']:.0%} | "
          f"action {summary['action_accuracy']:.0%} | UNSAFE {summary['unsafe']} | invalid {summary['invalid']}")
    print(f"tokens in/out {summary['input_tokens']}/{summary['output_tokens']} | "
          f"mean latency {summary['mean_latency_s']}s")


def _cluster_check(context: str) -> Callable[[Scenario], tuple[bool, str]]:
    from lab.check import evaluate
    from lab.kube import Kubectl

    k = Kubectl(context)

    def check(sc: Scenario) -> tuple[bool, str]:
        pods = k.get_json("pods", "-n", sc.namespace, "-l", sc.selector)["items"]
        events = k.get_json("events", "-n", sc.namespace)["items"]
        r = evaluate(pods, events, sc.spec["observable"])
        return r.ok, r.detail

    return check


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    from kubemedic.diagnose import BedrockModel
    from kubemedic.evidence import gather

    p = argparse.ArgumentParser(description="Score the diagnoser on every broken scenario")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--scenario", action="append", help="limit to these scenario ids (repeatable)")
    p.add_argument("--context", default="kind-kubemedic")
    args = p.parse_args(argv)

    selected = [scenarios.load(i) for i in args.scenario] if args.scenario else scenarios.load_all()
    safe_model = args.model.replace(":", "_").replace("/", "_")
    out_dir = REPORTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_model}"

    try:
        summary = run_eval(selected, BedrockModel(args.model), lambda ns: gather(ns, args.context),
                           args.repeats, out_dir, _cluster_check(args.context))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BotoCoreError as e:
        print(f"AWS credentials problem: {e}\nFix: source ./aws-login.sh", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"Bedrock error ({args.model}): {e.response['Error']['Message']}", file=sys.stderr)
        return 2

    print_scorecard(summary)
    print(f"\nfull results: {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
