"""Checks eval summaries become the right MLflow params and metrics, costs are computed from pricing.yaml, and tracking can be turned off."""
import tempfile
from pathlib import Path

from evals import tracking
from evals.tracking import diagnosis_record, estimate_cost, load_pricing, log_eval, retrieval_record

SUMMARY = {"model": "us.amazon.nova-lite-v1:0", "prompt_version": "v3", "rag": True, "repeats": 3, "runs": 18,
           "pass_rate": 1.0, "root_cause_accuracy": 1.0, "action_accuracy": 1.0, "unsafe": 0, "invalid": 0,
           "citation_accuracy": 1.0, "guardrail_blocks": 0, "overrides": 0,
           "input_tokens": 87499, "output_tokens": 5434, "mean_latency_s": 2.02,
           "per_scenario": [{"scenario": "oom-killed", "passed": 2, "repeats": 3}]}


class FakeMlflow:
    """Records what would have been sent to MLflow."""

    def __init__(self):
        self.experiments, self.logged = {}, {}

    def set_tracking_uri(self, uri): self.logged["uri"] = uri
    def get_experiment_by_name(self, name): return self.experiments.get(name)
    def create_experiment(self, name, artifact_location): self.experiments[name] = artifact_location
    def set_experiment(self, name): self.logged["experiment"] = name
    def log_params(self, p): self.logged["params"] = p
    def log_metrics(self, m): self.logged["metrics"] = m
    def set_tags(self, t): self.logged["tags"] = t
    def log_artifacts(self, d): self.logged["artifacts"] = d

    def start_run(self, run_name):
        fake = self

        class Run:
            info = type("Info", (), {"run_id": "run-123"})

            def __enter__(self):
                fake.logged["run_name"] = run_name
                return self

            def __exit__(self, *a):
                return False
        return Run()


def test_cost_comes_from_the_pricing_file():
    pricing = load_pricing()
    assert estimate_cost("us.amazon.nova-lite-v1:0", 1_000_000, 1_000_000, pricing) == 0.30
    assert estimate_cost("some-unlisted-model", 1000, 1000, pricing) is None


def test_diagnosis_summary_becomes_params_and_metrics():
    params, metrics = diagnosis_record(SUMMARY, load_pricing())
    assert params == {"model": "us.amazon.nova-lite-v1:0", "prompt_version": "v3", "rag": True, "repeats": 3, "scenarios": 1}
    assert metrics["pass_rate"] == 1.0 and metrics["unsafe"] == 0
    assert metrics["tokens_per_diagnosis"] == round((87499 + 5434) / 18, 1)
    assert metrics["cost_usd"] == round(87499 / 1e6 * 0.06 + 5434 / 1e6 * 0.24, 6)
    assert metrics["scenario/oom-killed/pass_rate"] == 0.67


def test_old_v1_summaries_without_newer_fields_still_log():
    old = {k: v for k, v in SUMMARY.items() if k not in ("prompt_version", "rag", "citation_accuracy", "guardrail_blocks", "overrides")}
    params, metrics = diagnosis_record(old, {})
    assert params["prompt_version"] == "v1" and params["rag"] is False
    assert "citation_accuracy" not in metrics and "cost_usd" not in metrics


def test_retrieval_summary_uses_mlflow_safe_names():
    params, metrics = retrieval_record({"embed_model": "titan", "scenarios": 6, "hit@1": 0.83, "hit@3": 1.0, "mrr": 0.92,
                                        "min_margin": 0.064, "per_scenario": [{"scenario": "oom-killed", "rank": 2}]})
    assert metrics == {"hit_at_1": 0.83, "hit_at_3": 1.0, "mrr": 0.92, "min_margin": 0.064, "scenario/oom-killed/rank": 2}


def test_log_eval_creates_the_experiment_and_logs_everything():
    fake, out = FakeMlflow(), Path(tempfile.mkdtemp()) / "20261030-run"
    assert log_eval(SUMMARY, out, {"embed_model": "titan"}, mlflow=fake) == "run-123"
    assert fake.logged["experiment"] == "kubemedic-diagnosis" and "kubemedic-diagnosis" in fake.experiments
    assert fake.logged["params"]["embed_model"] == "titan" and "git_commit" in fake.logged["params"]
    assert fake.logged["run_name"] == "20261030-run" and fake.logged["artifacts"] == str(out)
    assert fake.logged["uri"].startswith("sqlite:///")
    assert fake.logged["tags"]["backfilled"] == "false"


def test_backfilled_runs_do_not_claim_todays_git_commit():
    fake = FakeMlflow()
    log_eval(SUMMARY, Path(tempfile.mkdtemp()), mlflow=fake, backfilled=True)
    assert fake.logged["params"]["git_commit"] == "unknown" and fake.logged["tags"]["backfilled"] == "true"


def test_tracking_can_be_switched_off():
    import os
    os.environ["KUBEMEDIC_TRACKING"] = "off"
    try:
        assert log_eval(SUMMARY, Path("x"), mlflow=FakeMlflow()) is None
    finally:
        del os.environ["KUBEMEDIC_TRACKING"]
