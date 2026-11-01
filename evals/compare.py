"""Puts two eval runs side by side (e.g. Bedrock vs the self-hosted model): quality, safety, speed, cost per diagnosis, and how many diagnoses an hour the GPU needs to break even.

Usage: python -m evals.compare reports/<bedrock-run> reports/<self-hosted-run>   (or make compare A=... B=...)

The break-even sum, in plain terms: Bedrock charges per diagnosis; a GPU costs the same per hour
however much it is used. Divide the GPU's hourly price by Bedrock's price per diagnosis and you
get the number of diagnoses an hour at which owning the GPU becomes cheaper than renting.
The evals send one request at a time, so "one at a time" capacity is measured here; vLLM serves
many requests at once, so real capacity is higher. That part is noted, not measured.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .tracking import estimate_cost, load_pricing

ROWS = [("pass rate", "pass_rate", "pct"), ("root cause", "root_cause_accuracy", "pct"),
        ("action (system)", "action_accuracy", "pct"), ("action (model alone)", "model_action_accuracy", "pct"),
        ("UNSAFE", "unsafe", "int"), ("invalid", "invalid", "int"),
        ("mean latency (s)", "mean_latency_s", "num")]
# These must match for the comparison to be like-for-like
SAME = ("mode", "rag", "prompt_version", "evidence_version", "repeats", "root_cause_grader")


def load(path: str) -> dict:
    p = Path(path)
    return json.loads((p / "summary.json" if p.is_dir() else p).read_text())


def per_diagnosis(summary: dict, pricing: dict) -> dict:
    runs = max(summary.get("runs", 0), 1)
    cost = estimate_cost(summary["model"], summary["input_tokens"], summary["output_tokens"], pricing,
                         seconds=summary.get("mean_latency_s", 0) * runs)
    price = pricing.get(summary["model"], {})
    return {"tokens": round((summary["input_tokens"] + summary["output_tokens"]) / runs),
            "cost": None if cost is None else cost / runs,
            "billing": "per GPU-hour" if "gpu_hour_usd" in price else ("per token" if price else "unpriced"),
            "gpu_hour_usd": price.get("gpu_hour_usd")}


def break_even(token_billed: dict, gpu_billed: dict, gpu_summary: dict) -> dict | None:
    """Diagnoses per hour at which the GPU costs the same per diagnosis as the per-token model."""
    if not (token_billed["cost"] and gpu_billed["gpu_hour_usd"]):
        return None
    needed = gpu_billed["gpu_hour_usd"] / token_billed["cost"]
    one_at_a_time = 3600 / gpu_summary["mean_latency_s"] if gpu_summary.get("mean_latency_s") else None
    return {"per_hour": round(needed), "one_at_a_time_capacity": round(one_at_a_time) if one_at_a_time else None}


def fmt(value, kind: str) -> str:
    if value is None:
        return "-"
    return {"pct": f"{value:.0%}", "int": str(value), "num": f"{value}"}[kind]


def report(a: dict, b: dict, pricing: dict) -> list[str]:
    lines = []
    mismatched = [k for k in SAME if a.get(k) != b.get(k)]
    if mismatched:
        lines.append("WARNING: not like-for-like, these differ: "
                     + ", ".join(f"{k} ({a.get(k)} vs {b.get(k)})" for k in mismatched))
    w = max(len(a["model"]), len(b["model"]), 14) + 2
    lines.append(f"{'':<22}{a['model']:<{w}}{b['model']}")
    for label, key, kind in ROWS:
        lines.append(f"{label:<22}{fmt(a.get(key), kind):<{w}}{fmt(b.get(key), kind)}")
    pa, pb = per_diagnosis(a, pricing), per_diagnosis(b, pricing)
    lines.append(f"{'tokens / diagnosis':<22}{pa['tokens']:<{w}}{pb['tokens']}")
    cost = lambda p: "-" if p["cost"] is None else f"${p['cost']:.6f} ({p['billing']})"  # noqa: E731
    lines.append(f"{'cost / diagnosis':<22}{cost(pa):<{w}}{cost(pb)}")

    pairs = [(pa, pb, b), (pb, pa, a)]
    for token_billed, gpu_billed, gpu_summary in pairs:
        if token_billed["billing"] == "per token" and gpu_billed["billing"] == "per GPU-hour":
            be = break_even(token_billed, gpu_billed, gpu_summary)
            if be:
                lines.append("")
                lines.append(f"break-even: the GPU (${gpu_billed['gpu_hour_usd']}/h) matches the per-token price at "
                             f"{be['per_hour']:,} diagnoses an hour.")
                if be["one_at_a_time_capacity"]:
                    lines.append(f"one request at a time it manages ~{be['one_at_a_time_capacity']:,} an hour; vLLM batches "
                                 "concurrent requests, so real capacity is higher (not measured here).")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare two eval runs side by side")
    p.add_argument("a", help="report folder or summary.json")
    p.add_argument("b", help="report folder or summary.json")
    args = p.parse_args(argv)
    print("\n".join(report(load(args.a), load(args.b), load_pricing())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
