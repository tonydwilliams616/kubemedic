"""Finds the runbooks most relevant to an incident by comparing meaning fingerprints (embeddings) of the evidence and the runbooks.

How it works:
1. Each runbook is turned into an embedding (a list of numbers that captures its meaning) by
   Amazon Titan Text Embeddings V2. These are saved in .cache/ and only rebuilt when a runbook
   or the embedding model changes.
2. The evidence bundle is boiled down to a short symptom description (query_text), which is
   embedded the same way.
3. Runbooks are ranked by cosine similarity: how closely their fingerprint points in the same
   direction as the incident's. The top k are returned.

With ten pages, plain Python is plenty: no vector database needed.

Usage: python -m kubemedic.retrieve --namespace km-oom-killed [--k 3]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .runbooks import Runbook, load_all

EMBED_MODEL = os.environ.get("KUBEMEDIC_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
DIMENSIONS = 512          # Titan V2 supports 256, 512 or 1024; 512 is plenty for ten short pages
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
MAX_QUERY_CHARS = 4000


class Embedder(Protocol):
    model_id: str

    def embed(self, text: str) -> list[float]:
        """Return a unit-length vector for the text."""


class TitanEmbedder:
    def __init__(self, model_id: str = EMBED_MODEL, region: str | None = None):
        import boto3  # imported here so unit tests never need AWS libraries or credentials
        self.model_id = model_id
        self.client = boto3.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1"))

    def embed(self, text: str) -> list[float]:
        resp = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
        )
        return json.loads(resp["body"].read())["embedding"]


# ---------- the index ----------

def runbook_text(rb: Runbook) -> str:
    """What gets fingerprinted for a page: its title, search signals and full text."""
    return f"{rb.title}\nSignals: {', '.join(map(str, rb.signals))}\n\n{rb.body}"


@dataclass
class Index:
    model_id: str
    ids: list[str]
    titles: list[str]
    vectors: list[list[float]]


def _fingerprint(texts: list[str], model_id: str) -> str:
    h = hashlib.sha256(model_id.encode())
    for t in texts:
        h.update(b"\0" + t.encode())
    return h.hexdigest()[:16]


def build_index(embedder: Embedder, runbooks: list[Runbook] | None = None,
                cache_dir: Path | None = CACHE_DIR) -> Index:
    """Embed every runbook, reusing the cached result if nothing has changed."""
    runbooks = runbooks if runbooks is not None else load_all()
    texts = [runbook_text(rb) for rb in runbooks]
    cache_file = cache_dir / f"runbook-index-{_fingerprint(texts, embedder.model_id)}.json" if cache_dir else None

    if cache_file and cache_file.is_file():
        data = json.loads(cache_file.read_text())
        return Index(**data)

    index = Index(model_id=embedder.model_id, ids=[rb.id for rb in runbooks],
                  titles=[rb.title for rb in runbooks], vectors=[embedder.embed(t) for t in texts])
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(index.__dict__))
    return index


# ---------- the query ----------

def query_text(bundle: dict) -> str:
    """Boil an evidence bundle down to the symptoms an engineer would search a runbook for.

    Keeps failure signals (waiting/terminated reasons, unhealthy conditions, warning events,
    what the current revision changed, the last few log lines) and drops healthy noise.
    """
    lines: list[str] = []
    for p in bundle.get("pods", []):
        for c in p.get("containers", []):
            st = c.get("state", {})
            if "waiting" in st:
                lines.append(f"container waiting: {st['waiting']} {st.get('message') or ''}".strip())
            if "terminated" in st:
                lines.append(f"container terminated: {st['terminated']} exit code {st.get('exit_code')}")
            last = c.get("last_terminated")
            if last:
                lines.append(f"last terminated: {last.get('reason')} exit code {last.get('exit_code')}")
        for cond in p.get("problem_conditions", []):
            lines.append(f"pod condition {cond['type']}=False {cond.get('reason') or ''} {cond.get('message') or ''}".strip())
    for e in bundle.get("events", []):
        if e.get("type") == "Warning":
            lines.append(f"event {e.get('reason')}: {e.get('message') or ''}")
    for d in bundle.get("deployments", []):
        for cond in d.get("conditions", []):
            if cond.get("status") != "True":
                lines.append(f"deployment {cond['type']}={cond['status']} {cond.get('reason') or ''}")
        for rev in d.get("rollout_history", []):
            if rev.get("current") and rev.get("changes_from_previous"):
                lines += [f"current revision changed: {c}" for c in rev["changes_from_previous"]]
    for log in bundle.get("logs", []):
        tail = [l for l in (log.get("text") or "").splitlines() if l.strip()][-5:]
        lines += [f"log: {l}" for l in tail]

    seen, unique = set(), []
    for l in lines:                      # events and logs repeat a lot; keep each line once
        if l not in seen:
            seen.add(l)
            unique.append(l)
    return "\n".join(unique)[:MAX_QUERY_CHARS]


# ---------- the search ----------

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def search(index: Index, embedder: Embedder, text: str, k: int = 3) -> list[dict]:
    """Return the k most similar runbooks, best first, with their similarity scores."""
    q = embedder.embed(text)
    scored = sorted(zip(index.ids, index.titles, index.vectors), key=lambda r: cosine(q, r[2]), reverse=True)
    return [{"id": i, "title": t, "score": round(cosine(q, v), 3)} for i, t, v in scored[:k]]


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    from .evidence import gather

    p = argparse.ArgumentParser(description="Show which runbooks the agent would retrieve for a namespace")
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--context", default="kind-kubemedic")
    p.add_argument("--k", type=int, default=3)
    args = p.parse_args(argv)

    try:
        embedder = TitanEmbedder()
        index = build_index(embedder)
        query = query_text(gather(args.namespace, args.context))
        hits = search(index, embedder, query, args.k)
    except BotoCoreError as e:
        print(f"AWS credentials problem: {e}\nFix: source ./aws-login.sh", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"Bedrock error ({EMBED_MODEL}): {e.response['Error']['Message']}", file=sys.stderr)
        return 2

    print("query (what the agent searched for):")
    print("  " + query.replace("\n", "\n  "))
    print(f"\ntop {args.k} runbooks:")
    for rank, h in enumerate(hits, 1):
        print(f"  {rank}. {h['id']:<28} score {h['score']:.3f}  {h['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
