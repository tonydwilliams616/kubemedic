"""Checks self-hosted runs are priced by GPU time, and that the side-by-side comparison flags mismatched runs and works out the break-even volume."""
from evals.compare import break_even, per_diagnosis, report
from evals.tracking import estimate_cost, load_pricing

PRICING = load_pricing()


def summary(model, latency, tokens_in=40_000, tokens_out=4_000, runs=10, **kw):
    s = {"model": model, "mode": "one-shot", "rag": True, "prompt_version": "v3", "evidence_version": "e2",
         "repeats": 1, "root_cause_grader": "keywords", "runs": runs, "pass_rate": 0.9,
         "root_cause_accuracy": 0.9, "action_accuracy": 1.0, "model_action_accuracy": 0.9, "unsafe": 0,
         "invalid": 0, "mean_latency_s": latency, "input_tokens": tokens_in, "output_tokens": tokens_out}
    return s | kw


BEDROCK = summary("us.amazon.nova-lite-v1:0", 2.0)
SELF = summary("openai:Qwen/Qwen3-8B", 6.0)


def test_self_hosted_cost_is_gpu_time_not_tokens():
    # 10 diagnoses x 6 s = 60 s of a $0.8048/h GPU
    assert estimate_cost("openai:Qwen/Qwen3-8B", 10**9, 10**9, PRICING, seconds=60) == round(60 / 3600 * 0.8048, 6)
    assert estimate_cost("openai:Qwen/Qwen3-8B", 1000, 1000, PRICING) is None        # no time, no cost


def test_per_diagnosis_and_break_even():
    pa, pb = per_diagnosis(BEDROCK, PRICING), per_diagnosis(SELF, PRICING)
    assert pa["billing"] == "per token" and pb["billing"] == "per GPU-hour"
    assert pa["cost"] == round(40_000 / 1e6 * 0.06 + 4_000 / 1e6 * 0.24, 6) / 10    # $0.000336
    be = break_even(pa, pb, SELF)
    assert be == {"per_hour": round(0.8048 / pa["cost"]), "one_at_a_time_capacity": 600}


def test_report_puts_runs_side_by_side_with_break_even():
    text = "\n".join(report(BEDROCK, SELF, PRICING))
    assert "WARNING" not in text
    assert "us.amazon.nova-lite-v1:0" in text and "openai:Qwen/Qwen3-8B" in text
    assert "(per GPU-hour)" in text and "break-even" in text and "~600 an hour" in text


def test_report_warns_when_runs_are_not_like_for_like():
    text = "\n".join(report(BEDROCK, summary("openai:Qwen/Qwen3-8B", 6.0, mode="agent"), PRICING))
    assert text.startswith("WARNING: not like-for-like") and "mode (one-shot vs agent)" in text


def test_order_of_the_two_runs_does_not_matter_for_break_even():
    assert [l for l in report(SELF, BEDROCK, PRICING) if "break-even" in l] == \
           [l for l in report(BEDROCK, SELF, PRICING) if "break-even" in l]
