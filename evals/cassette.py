"""Records every model call during a real eval and replays it later, so CI can rerun the whole eval with no AWS, no cluster and no cost.

A recording (evals/recordings/<name>/) holds:
- meta.json:                       which model, prompt version, repeats, and when it was recorded
- calls.json:                      fingerprint of each request -> the replies the model gave, in order
- <namespace>.bundle.json:         the evidence the model was shown
- <namespace>.runbooks.json:       which runbook pages were retrieved (RAG runs only)

Replay pushes the same evidence through today's code (diagnose, contract checks, citation
checks, guardrails, scoring) and answers each model call from calls.json instead of Bedrock.

The fingerprint covers the model id, the system prompt and every message. So if the prompt,
the evidence format or a runbook page changes, the request no longer matches anything recorded
and replay stops with a "re-record" error instead of silently reusing out-of-date answers.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Callable

from kubemedic.runbooks import load_all as load_runbooks

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
RERECORD_HINT = "the code now sends the model something that was never recorded; re-record with `make record`"


class NotRecorded(RuntimeError):
    """Replay was asked for something the recording doesn't contain."""


def fingerprint(model_id: str, system: str, messages: list[dict]) -> str:
    """A short, stable ID for one exact request. Any change to the request gives a different ID."""
    raw = json.dumps([model_id, system, messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Recorder:
    """Stands between the eval and the real model: passes every call through and remembers the reply."""

    def __init__(self, model, path: Path):
        self.inner, self.model_id, self.final = model, model.model_id, path
        self.calls: dict[str, list[dict]] = {}
        # Write into <name>.partial and only replace the real recording in save(), so a run that
        # fails halfway (cluster not broken, login expired) never destroys a good recording.
        self.path = path.with_name(path.name + ".partial")
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.mkdir(parents=True)

    def converse(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        text, usage = self.inner.converse(system, messages)
        # The same request is sent once per repeat, and the model may answer differently each
        # time, so every reply is kept in order.
        self.calls.setdefault(fingerprint(self.model_id, system, messages), []).append(
            {"text": text, "usage": usage})
        return text, usage

    def gathering(self, gather_fn: Callable[[str], dict]) -> Callable[[str], dict]:
        """Wrap evidence gathering so each bundle is saved as it is collected."""
        def gather(namespace: str) -> dict:
            bundle = gather_fn(namespace)
            (self.path / f"{namespace}.bundle.json").write_text(json.dumps(bundle, indent=1))
            return bundle
        return gather

    def retrieving(self, retrieve_fn: Callable[[dict], list[dict]]) -> Callable[[dict], list[dict]]:
        """Wrap runbook search so the chosen page ids are saved (the page text lives in runbooks/)."""
        def retrieve(bundle: dict) -> list[dict]:
            pages = retrieve_fn(bundle)
            (self.path / f"{bundle['namespace']}.runbooks.json").write_text(
                json.dumps([{"id": p["id"], "score": p["score"]} for p in pages], indent=1))
            return pages
        return retrieve

    def save(self, summary: dict) -> None:
        meta = {k: summary[k] for k in ("model", "prompt_version", "rag", "repeats")}
        meta |= {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "scenarios": [row["scenario"] for row in summary["per_scenario"]]}
        (self.path / "meta.json").write_text(json.dumps(meta, indent=2))
        (self.path / "calls.json").write_text(json.dumps(self.calls, indent=1, ensure_ascii=False))
        if self.final.exists():
            shutil.rmtree(self.final)  # a recording is always one complete run, never a mix of two
        self.path.rename(self.final)


class Player:
    """Pretends to be the model (and the cluster, and the runbook search) using a recording."""

    def __init__(self, path: Path):
        if not (path / "meta.json").is_file():
            raise NotRecorded(f"no recording at {path}; create one with `make record`")
        self.path = path
        self.meta = json.loads((path / "meta.json").read_text())
        self.calls = json.loads((path / "calls.json").read_text())
        self.model_id = self.meta["model"]
        self._used: dict[str, int] = {}     # how many replies of each request have been handed out

    def converse(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        key = fingerprint(self.model_id, system, messages)
        replies = self.calls.get(key)
        if not replies:
            raise NotRecorded(f"request {key} not in the recording: {RERECORD_HINT}")
        n = self._used.get(key, 0)
        if n >= len(replies):
            raise NotRecorded(f"request {key} was sent more times than recorded: {RERECORD_HINT}")
        self._used[key] = n + 1
        return replies[n]["text"], replies[n]["usage"]

    def gather(self, namespace: str) -> dict:
        f = self.path / f"{namespace}.bundle.json"
        if not f.is_file():
            raise NotRecorded(f"no recorded evidence for {namespace} (new scenario?): {RERECORD_HINT}")
        return json.loads(f.read_text())

    def retrieve(self, bundle: dict) -> list[dict]:
        """The pages recorded for this namespace, with their CURRENT text from runbooks/.

        If a page has been edited since recording, the prompt changes, its fingerprint no longer
        matches and replay asks for a re-record. That is deliberate: a runbook edit can change answers.
        """
        hits = json.loads((self.path / f"{bundle['namespace']}.runbooks.json").read_text())
        pages = {rb.id: rb for rb in load_runbooks()}
        missing = [h["id"] for h in hits if h["id"] not in pages]
        if missing:
            raise NotRecorded(f"recorded runbooks no longer exist: {missing}: {RERECORD_HINT}")
        return [{"id": h["id"], "title": pages[h["id"]].title, "score": h["score"],
                 "body": pages[h["id"]].body} for h in hits]
