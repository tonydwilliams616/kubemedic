"""Checks the retrieval eval finds each correct page's rank and computes hit@1, hit@3, MRR and margin correctly."""
import shutil
import tempfile
from pathlib import Path

import yaml

from evals.retrieval import run_retrieval_eval, score_scenario, summarise
from kubemedic.retrieve import build_index
from lab import scenarios
from test_retrieve import WordEmbedder, rb

OOM = scenarios.load("oom-killed")          # relevant_runbooks: [oomkilled]
IMAGE = scenarios.load("bad-image-tag")     # relevant_runbooks: [image-pull-errors]


def ranked(*ids):
    return [{"id": i, "title": i, "score": round(0.9 - n * 0.1, 3)} for n, i in enumerate(ids)]


def test_rank_hits_and_margin():
    r = score_scenario(OOM, ranked("liveness-probe-restarts", "oomkilled", "crashloopbackoff", "x"))
    assert (r["rank"], r["hit@1"], r["hit@3"], r["margin"]) == (2, False, True, 0.1)
    assert r["top3"] == ["liveness-probe-restarts", "oomkilled", "crashloopbackoff"]


def test_missing_page_has_no_rank():
    r = score_scenario(OOM, ranked("a", "b", "c", "d"))
    assert r["rank"] is None and not r["hit@1"] and not r["hit@3"]


def test_mrr_is_the_average_of_one_over_rank():
    rows = [{"rank": 1, "hit@1": True, "hit@3": True, "margin": 0.3},
            {"rank": 2, "hit@1": False, "hit@3": True, "margin": 0.05},
            {"rank": None, "hit@1": False, "hit@3": False, "margin": 0.2}]
    assert summarise(rows) == {"scenarios": 3, "hit@1": 0.33, "hit@3": 0.67, "mrr": 0.5, "min_margin": 0.05}


def test_end_to_end_with_fake_search():
    pages = [rb("oomkilled", "Killed for memory", "OOMKilled exit code 137 memory limit"),
             rb("image-pull-errors", "Image cannot be pulled", "ImagePullBackOff ErrImagePull image not found"),
             rb("pending-unschedulable", "Pod Pending", "FailedScheduling Insufficient cpu")]
    e = WordEmbedder()
    bundles = {
        "km-oom-killed": {"pods": [{"containers": [{"state": {}, "last_terminated": {"reason": "OOMKilled", "exit_code": 137}}]}]},
        "km-bad-image-tag": {"pods": [{"containers": [{"state": {"waiting": "ImagePullBackOff", "message": "image not found"}}]}]},
    }
    result = run_retrieval_eval([OOM, IMAGE], e, bundles.__getitem__, index=build_index(e, pages, cache_dir=None))
    assert result["hit@1"] == 1.0 and result["mrr"] == 1.0
    assert [r["top3"][0] for r in result["per_scenario"]] == ["oomkilled", "image-pull-errors"]


def test_scenario_validation_rejects_an_unknown_runbook():
    d = Path(tempfile.mkdtemp()) / "oom-killed"
    shutil.copytree(scenarios.SCENARIOS_DIR / "oom-killed", d)
    spec = yaml.safe_load((d / "expected.yaml").read_text())
    spec["relevant_runbooks"] = ["no-such-page"]
    (d / "expected.yaml").write_text(yaml.safe_dump(spec))
    errors = scenarios.validate(scenarios.load("oom-killed", d.parent))
    assert any("no-such-page" in e for e in errors)
