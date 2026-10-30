"""Measures how well runbook search finds the right page for each scenario, with no LLM involved.

Scoring search on its own means that when the full agent improves (or doesn't), we can tell
whether it was the search or the reasoning.

For each scenario the whole runbook list is ranked, then we record:
- rank:   where the correct page (relevant_runbooks in expected.yaml) came; 1 is best
- hit@1:  was it first?          hit@3: was it in the top 3 (what the agent will be shown)?
- margin: top score minus runner-up score; small means the search was unsure

Summary metrics: hit@1, hit@3 and MRR (mean reciprocal rank: 1/rank averaged; 1.0 = always first).

Usage: python -m evals.retrieval [--min-hit3 1.0]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

from kubemedic.retrieve import EMBED_MODEL, build_index, query_text, search
from lab import scenarios
from lab.scenarios import Scenario

REPORTS_DIR = Path("reports")


def score_scenario(sc: Scenario, ranked: list[dict]) -> dict:
    relevant = set(sc.spec["relevant_runbooks"])
    rank = next((i for i, h in enumerate(ranked, 1) if h["id"] in relevant), None)
    return {
        "scenario": sc.id,
        "expected": sorted(relevant),
        "rank": rank,
        "hit@1": rank == 1,
        "hit@3": rank is not None and rank <= 3,
        "top3": [h["id"] for h in ranked[:3]],
        "margin": round(ranked[0]["score"] - ranked[1]["score"], 3) if len(ranked) > 1 else None,
    }


def summarise(rows: list[dict]) -> dict:
    n = max(len(rows), 1)
    return {
        "scenarios": len(rows),
        "hit@1": round(sum(r["hit@1"] for r in rows) / n, 2),
        "hit@3": round(sum(r["hit@3"] for r in rows) / n, 2),
        "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 2),
        "min_margin": min((r["margin"] for r in rows if r["margin"] is not None), default=None),
    }


def run_retrieval_eval(selected: list[Scenario], embedder, gather_fn: Callable[[str], dict], index=None) -> dict:
    index = index or build_index(embedder)  # tests pass a small prebuilt index
    rows = []
    for sc in selected:
        ranked = search(index, embedder, query_text(gather_fn(sc.namespace)), k=len(index.ids))
        rows.append(score_scenario(sc, ranked))
    return {"embed_model": embedder.model_id, **summarise(rows), "per_scenario": rows}


def print_report(result: dict) -> None:
    print(f"\nembedding model: {result['embed_model']}")
    print(f"{'scenario':<28} {'rank':<5} {'margin':<7} top 3")
    for r in result["per_scenario"]:
        mark = "✓" if r["hit@1"] else ("~" if r["hit@3"] else "✗")
        print(f"{r['scenario']:<28} {mark} {r['rank'] or '-':<3} {r['margin']:<7} {', '.join(r['top3'])}")
    print(f"\nhit@1 {result['hit@1']:.0%} | hit@3 {result['hit@3']:.0%} | MRR {result['mrr']:.2f} | "
          f"smallest margin {result['min_margin']}")


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    from kubemedic.evidence import gather
    from kubemedic.retrieve import TitanEmbedder

    from .run import _cluster_check

    p = argparse.ArgumentParser(description="Score runbook search on every broken scenario")
    p.add_argument("--context", default="kind-kubemedic")
    p.add_argument("--min-hit3", type=float, default=None, help="exit 1 if hit@3 falls below this")
    args = p.parse_args(argv)

    selected = scenarios.load_all()
    check = _cluster_check(args.context)
    not_ready = [sc.id for sc in selected if not check(sc)[0]]
    if not_ready:
        print(f"error: faults not observable, run `make break-all && make check-all` first: {not_ready}", file=sys.stderr)
        return 2
    try:
        result = run_retrieval_eval(selected, TitanEmbedder(), lambda ns: gather(ns, args.context))
    except BotoCoreError as e:
        print(f"AWS credentials problem: {e}\nFix: source ./aws-login.sh", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"Bedrock error ({EMBED_MODEL}): {e.response['Error']['Message']}", file=sys.stderr)
        return 2

    out = REPORTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-retrieval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(result, indent=2))
    print_report(result)
    print(f"\nfull results: {out}/summary.json")
    from .run import _track
    _track(result, out, None)
    if args.min_hit3 is not None and result["hit@3"] < args.min_hit3:
        print(f"FAIL: hit@3 {result['hit@3']} is below the minimum {args.min_hit3}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
