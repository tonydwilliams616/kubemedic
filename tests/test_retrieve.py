"""Checks the runbook index caches correctly, the query keeps only failure signals, and search ranks by meaning, using a fake embedder."""
import math
import re
import tempfile
from pathlib import Path

from kubemedic.retrieve import build_index, cosine, query_text, search
from kubemedic.runbooks import Runbook


class WordEmbedder:
    """Fake embedder: counts words into 64 buckets. Similar wording -> similar vectors. Counts calls."""
    model_id = "fake-words"

    def __init__(self):
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        v = [0.0] * 64
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[hash(w) % 64] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        return [x / n for x in v]


def rb(id, title, body):
    return Runbook(id=id, title=title, signals=[], mitigations=["escalate"], related=[], body=body, path=Path(f"{id}.md"))


PAGES = [
    rb("memory", "Container killed for using too much memory", "OOMKilled exit code 137 memory limit exceeded"),
    rb("image", "Image cannot be pulled", "ErrImagePull ImagePullBackOff image tag not found registry"),
    rb("schedule", "Pod stuck Pending", "FailedScheduling Insufficient cpu taint nodes unschedulable"),
]


def tmp():
    return Path(tempfile.mkdtemp())


def test_index_is_cached_and_reused():
    cache, e = tmp(), WordEmbedder()
    build_index(e, PAGES, cache)
    assert e.calls == 3
    build_index(e, PAGES, cache)
    assert e.calls == 3                      # second build came from the cache: no new embeddings


def test_changing_a_page_or_the_model_rebuilds_the_index():
    cache, e = tmp(), WordEmbedder()
    build_index(e, PAGES, cache)
    edited = PAGES[:2] + [rb("schedule", "Pod stuck Pending", "FailedScheduling and something new")]
    build_index(e, edited, cache)
    assert e.calls == 6
    e.model_id = "another-model"
    build_index(e, PAGES, cache)
    assert e.calls == 9
    assert len(list(cache.glob("runbook-index-*.json"))) == 3


def test_search_ranks_the_matching_page_first():
    e = WordEmbedder()
    index = build_index(e, PAGES, cache_dir=None)
    hits = search(index, e, "last terminated: OOMKilled exit code 137", k=2)
    assert [h["id"] for h in hits][0] == "memory" and len(hits) == 2
    assert hits[0]["score"] >= hits[1]["score"]


def test_cosine():
    assert cosine([1, 0], [1, 0]) == 1.0 and cosine([1, 0], [0, 1]) == 0.0 and cosine([0, 0], [1, 0]) == 0.0


BUNDLE = {
    "pods": [
        {"containers": [{"state": {"running": True}}], "problem_conditions": []},             # healthy: ignored
        {"containers": [{"state": {"waiting": "CrashLoopBackOff", "message": "back-off"},
                         "last_terminated": {"reason": "OOMKilled", "exit_code": 137}}],
         "problem_conditions": [{"type": "Ready", "reason": "ContainersNotReady", "message": None}]},
    ],
    "events": [
        {"type": "Normal", "reason": "Pulled", "message": "Successfully pulled image"},      # ignored
        {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container"},
        {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container"},
    ],
    "deployments": [{"conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}],
                     "rollout_history": [
                         {"current": False, "changes_from_previous": None},
                         {"current": True, "changes_from_previous": ['container w: env CACHE_MB "16" -> "200"']}]}],
    "logs": [{"text": "line1\nline2\nline3\nline4\nline5\nline6\nworker started, cache=200MB\n"}],
}


def test_query_keeps_failure_signals_and_drops_noise():
    q = query_text(BUNDLE)
    assert "container waiting: CrashLoopBackOff back-off" in q
    assert "last terminated: OOMKilled exit code 137" in q
    assert "pod condition Ready=False ContainersNotReady" in q
    assert "deployment Available=False MinimumReplicasUnavailable" in q
    assert 'current revision changed: container w: env CACHE_MB "16" -> "200"' in q
    assert "log: worker started, cache=200MB" in q and "log: line1" not in q   # only the last 5 log lines
    assert "Successfully pulled image" not in q and "running" not in q          # healthy noise dropped
    assert q.count("Back-off restarting failed container") == 1                 # duplicates removed


def test_retriever_returns_full_pages_best_first():
    from kubemedic.retrieve import Retriever
    r = Retriever(WordEmbedder(), PAGES)
    bundle = {"pods": [{"containers": [{"state": {}, "last_terminated": {"reason": "OOMKilled", "exit_code": 137}}]}]}
    top = r.top_runbooks(bundle, k=2)
    assert top[0]["id"] == "memory" and "memory limit" in top[0]["body"] and len(top) == 2
