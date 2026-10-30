"""Logs every eval run to MLflow (settings, scores, tokens, cost and all result files) so runs can be compared in one dashboard.

What gets recorded for each run:
- params:    what was tried   (model, prompt version, RAG on/off, repeats, git commit ...)
- metrics:   how it did       (pass rate, root cause, action, unsafe, tokens, latency, cost ...)
- artifacts: the evidence     (the whole reports/<run>/ folder: bundles, every answer, summary)

Storage is local: a SQLite database in .mlflow/ (MLflow's default backend; the plain-file store
is in maintenance mode). Nothing leaves your machine. View it with `make mlflow-ui`.

Tracking is optional: if MLflow isn't installed, or KUBEMEDIC_TRACKING=off, evals still run
and just skip this step.

Usage (log report folders that already exist, e.g. runs made before tracking was added):
    python -m evals.tracking reports/<run-folder> [reports/<run-folder> ...]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TRACKING_DIR = ROOT / ".mlflow"
TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{TRACKING_DIR / 'mlflow.db'}")
PRICING_FILE = Path(__file__).resolve().parent / "pricing.yaml"


def git_info() -> dict:
    """Which code produced this run. 'dirty' means there were uncommitted changes."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    try:
        return {"git_commit": git("rev-parse", "--short", "HEAD") or "unknown",
                "git_dirty": bool(git("status", "--porcelain"))}
    except OSError:
        return {"git_commit": "unknown", "git_dirty": True}


def estimate_cost(model_id: str, tokens_in: int, tokens_out: int, pricing: dict) -> float | None:
    """US dollars for the run, from evals/pricing.yaml. None if the model has no price listed."""
    price = pricing.get(model_id)
    if not price:
        return None
    return round(tokens_in / 1e6 * price["input_per_million"] + tokens_out / 1e6 * price["output_per_million"], 6)


def load_pricing(path: Path = PRICING_FILE) -> dict:
    return (yaml.safe_load(path.read_text()) or {}).get("models", {}) if path.is_file() else {}


def diagnosis_record(summary: dict, pricing: dict) -> tuple[dict, dict]:
    """Split a diagnosis-eval summary into (params, metrics)."""
    runs = max(summary.get("runs", 0), 1)
    params = {
        "model": summary["model"],
        "prompt_version": summary.get("prompt_version", "v1"),   # v1 runs predate prompt versioning
        "evidence_version": summary.get("evidence_version", "e1"),   # e1 runs showed the namespace name
        "mode": summary.get("mode", "one-shot"),
        "rag": summary.get("rag", False),
        "repeats": summary.get("repeats"),
        "scenarios": len(summary.get("per_scenario", [])),
    }
    metrics = {k: summary[k] for k in (
        "pass_rate", "root_cause_accuracy", "action_accuracy", "unsafe", "invalid",
        "citation_accuracy", "guardrail_blocks", "overrides", "model_action_accuracy",
        "mean_steps", "mean_tool_calls", "budget_exhausted",
        "input_tokens", "output_tokens", "mean_latency_s") if summary.get(k) is not None}
    metrics["tokens_per_diagnosis"] = round((summary["input_tokens"] + summary["output_tokens"]) / runs, 1)
    cost = estimate_cost(summary["model"], summary["input_tokens"], summary["output_tokens"], pricing)
    if cost is not None:
        metrics["cost_usd"] = cost
        metrics["cost_per_diagnosis_usd"] = round(cost / runs, 6)
    for row in summary.get("per_scenario", []):
        metrics[f"scenario/{row['scenario']}/pass_rate"] = round(row["passed"] / max(row["repeats"], 1), 2)
    return params, metrics


def retrieval_record(summary: dict) -> tuple[dict, dict]:
    """Split a retrieval-eval summary into (params, metrics). MLflow keys can't contain '@'."""
    params = {"embed_model": summary["embed_model"], "scenarios": summary["scenarios"]}
    metrics = {"hit_at_1": summary["hit@1"], "hit_at_3": summary["hit@3"], "mrr": summary["mrr"]}
    if summary.get("min_margin") is not None:
        metrics["min_margin"] = summary["min_margin"]
    for row in summary.get("per_scenario", []):
        if row.get("rank"):
            metrics[f"scenario/{row['scenario']}/rank"] = row["rank"]
    return params, metrics


def log_eval(summary: dict, out_dir: Path, extra_params: dict | None = None, mlflow=None,
             backfilled: bool = False) -> str | None:
    """Log one eval run. Returns the MLflow run id, or None if tracking is off or unavailable.

    backfilled=True is for runs made before tracking existed: today's git commit didn't produce
    them, so the commit is recorded as unknown rather than a misleading one.
    """
    if os.environ.get("KUBEMEDIC_TRACKING", "on").lower() == "off":
        return None
    if mlflow is None:
        try:
            import mlflow  # optional dependency: requirements-tracking.txt
        except ImportError:
            print("note: MLflow not installed, run not tracked (make tracking-setup)", file=sys.stderr)
            return None

    kind = "retrieval" if "hit@1" in summary else "diagnosis"
    params, metrics = retrieval_record(summary) if kind == "retrieval" else diagnosis_record(summary, load_pricing())
    code = {"git_commit": "unknown", "git_dirty": "unknown"} if backfilled else git_info()
    params |= code | (extra_params or {})

    mlflow.set_tracking_uri(TRACKING_URI)
    name = f"kubemedic-{kind}"
    if mlflow.get_experiment_by_name(name) is None:
        mlflow.create_experiment(name, artifact_location=(TRACKING_DIR / "artifacts" / kind).resolve().as_uri())
    mlflow.set_experiment(name)
    with mlflow.start_run(run_name=out_dir.name) as run:
        mlflow.log_params(params)
        mlflow.log_metrics(metrics)
        mlflow.set_tags({"report_dir": str(out_dir), "kind": kind, "backfilled": str(backfilled).lower()})
        mlflow.log_artifacts(str(out_dir))
        return run.info.run_id


def main(argv: list[str] | None = None) -> int:
    dirs = [Path(d) for d in (sys.argv[1:] if argv is None else argv)]
    if not dirs:
        print("usage: python -m evals.tracking reports/<run-folder> [...]", file=sys.stderr)
        return 2
    for d in dirs:
        summary_file = d / "summary.json"
        if not summary_file.is_file():
            print(f"skip {d}: no summary.json", file=sys.stderr)
            continue
        run_id = log_eval(json.loads(summary_file.read_text()), d, backfilled=True)
        print(f"logged {d} -> {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
