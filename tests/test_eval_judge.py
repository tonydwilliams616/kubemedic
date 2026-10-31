"""Checks the LLM judge grades root causes inside the evals (keywords kept for comparison), that its verdicts are cached so each answer is paid for once, and that CI can replay with cached verdicts only."""
import json
import tempfile
from pathlib import Path

import pytest

from evals import cassette, gate
from evals import run as run_module
from evals.cassette import JudgeCache, NotRecorded, Player
from evals.judge import judge
from evals.run import run_eval
from test_cassette import GOOD_OOM, GOOD_SECRET, OOM, SECRET, record

SCENARIOS = ["--scenario", "oom-killed", "--scenario", "missing-secret"]
from test_judge import FakeJudge, verdict


def judge_fn_with(model, rubric="j3"):
    def judge_fn(spec, diagnosis):
        return judge(spec["root_cause"], diagnosis, model, rubric=rubric)
    judge_fn.rubric, judge_fn.model_id = rubric, model.model_id
    return judge_fn


def test_the_judge_decides_root_cause_and_the_keyword_score_is_kept():
    tmp = Path(tempfile.mkdtemp())
    _, rec = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    player = Player(rec)
    strict = FakeJudge(rule=lambda msg: verdict("incorrect"))         # disagrees with the keyword check
    s = run_eval([OOM, SECRET], player, player.gather, 2, tmp / "j", None, player.retrieve, judge_fn=judge_fn_with(strict))
    assert s["root_cause_grader"] == "judge:j3" and s["root_cause_accuracy"] == 0.0
    assert s["keyword_root_cause_accuracy"] == 1.0 and s["pass_rate"] == 0.0 and s["judge_errors"] == 0
    assert s["judge_input_tokens"] == 4 * 500
    run_file = json.loads((tmp / "j" / "oom-killed.run1.json").read_text())
    assert run_file["judge"]["correct"] is False and run_file["judge"]["prompt_version"] == "j3"


def test_a_failed_judge_falls_back_to_keywords_and_is_counted():
    tmp = Path(tempfile.mkdtemp())
    _, rec = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    player = Player(rec)
    broken = FakeJudge(rule=lambda msg: "not json at all")
    s = run_eval([OOM, SECRET], player, player.gather, 2, tmp / "j", None, player.retrieve, judge_fn=judge_fn_with(broken))
    assert s["judge_errors"] == 4 and s["root_cause_accuracy"] == s["keyword_root_cause_accuracy"] == 1.0


def test_the_cache_pays_for_each_answer_once_and_offline_refuses_misses():
    path = Path(tempfile.mkdtemp()) / "cache.json"
    real = FakeJudge(rule=lambda msg: verdict("correct"))
    online = JudgeCache(path).wrap(real)
    for _ in range(3):                                   # the same answer in three repeats
        online.converse("rubric", [{"role": "user", "content": [{"text": "answer A"}]}])
    assert len(real.requests) == 1
    cache = JudgeCache(path)
    cache.wrap(real).converse("rubric", [{"role": "user", "content": [{"text": "answer B"}]}])
    cache.save()
    offline = JudgeCache(path, offline=True).wrap(None, real.model_id)
    text, usage = offline.converse("rubric", [{"role": "user", "content": [{"text": "answer B"}]}])
    assert json.loads(text)["verdict"] == "correct" and usage == {"input_tokens": 0, "output_tokens": 0}
    with pytest.raises(NotRecorded, match="make judge-cache"):
        offline.converse("a new rubric", [{"role": "user", "content": [{"text": "answer B"}]}])


def test_ci_replays_with_cached_verdicts_only_and_applies_the_gate():
    tmp = Path(tempfile.mkdtemp())
    _, rec = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    saved_cache, saved_reports = cassette.JUDGE_CACHE, run_module.REPORTS_DIR
    cassette.JUDGE_CACHE, run_module.REPORTS_DIR = tmp / "judge" / "cache.json", tmp / "reports"
    try:
        # Fill the cache once with a (fake) live judge, as `make judge-cache` does with Nova Pro.
        cache = JudgeCache()
        from evals.judge import JUDGE_MODEL
        live = FakeJudge(rule=lambda msg: verdict("correct"))
        live.model_id = JUDGE_MODEL
        player = Player(rec)
        run_eval([OOM, SECRET], player, player.gather, 2, tmp / "fill", None, player.retrieve,
                 judge_fn=judge_fn_with(cache.wrap(live)))
        cache.save()
        args = ["--replay", str(rec), "--judge", "--judge-offline", "--gate", str(gate.GATE_FILE)] + SCENARIOS
        assert run_module.main(args) == 0
        cassette.JUDGE_CACHE = tmp / "empty" / "cache.json"            # nothing cached: CI must fail, not guess
        assert run_module.main(args) == 2
    finally:
        cassette.JUDGE_CACHE, run_module.REPORTS_DIR = saved_cache, saved_reports
