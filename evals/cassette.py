"""Records every model call during a real eval and replays it later, so CI can rerun the whole eval with no AWS, no cluster and no cost.

A recording (evals/recordings/<name>/) holds:
- meta.json:                       which model, prompt version, repeats, and when it was recorded
- calls.json:                      fingerprint of each request -> the replies the model gave, in order
- <namespace>.bundle.json:         the evidence the model was shown
- <namespace>.runbooks.json:       which runbook pages were retrieved (RAG runs only)

(The tool-using agent is recorded differently, as a "tape" of both sides of its conversation:
see the bottom of this file.)

Replay pushes the same evidence through today's code (diagnose, contract checks, citation
checks, guardrails, scoring) and answers each model call from calls.json instead of Bedrock.

The fingerprint covers the model id, the system prompt and every message. So if the prompt,
the evidence format or a runbook page changes, the request no longer matches anything recorded
and replay stops with a "re-record" error instead of silently reusing out-of-date answers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kubemedic.runbooks import load_all as load_runbooks

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
RERECORD_HINT = "the code now sends the model something that was never recorded; re-record with `make record`"


class NotRecorded(RuntimeError):
    """Replay was asked for something the recording doesn't contain."""


def read_meta(path: Path) -> dict:
    if not (path / "meta.json").is_file():
        raise NotRecorded(f"no recording at {path}; create one with `make record` or `make record-agent`")
    return json.loads((path / "meta.json").read_text())


def _start(path: Path) -> Path:
    """Recordings are written to <name>.partial and only swapped in by _finish, so a failed run never destroys a good one."""
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    return partial


def _finish(partial: Path, final: Path) -> None:
    if final.exists():
        shutil.rmtree(final)    # a recording is always one complete run, never a mix of two
    partial.rename(final)


def fingerprint(model_id: str, system: str, messages: list[dict]) -> str:
    """A short, stable ID for one exact request. Any change to the request gives a different ID."""
    raw = json.dumps([model_id, system, messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Recorder:
    """Stands between the eval and the real model: passes every call through and remembers the reply."""

    def __init__(self, model, path: Path):
        self.inner, self.model_id, self.final = model, model.model_id, path
        self.calls: dict[str, list[dict]] = {}
        self.path = _start(path)      # see _start: a failed run never destroys a good recording

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
        _finish(self.path, self.final)


class Player:
    """Pretends to be the model (and the cluster, and the runbook search) using a recording."""

    def __init__(self, path: Path):
        self.path = path
        self.meta = read_meta(path)
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


# ---------- the agent: tape both sides of the conversation ----------
#
# The one-shot diagnoser gets one fixed bundle, so recording the bundle and the model's replies is
# enough. The agent asks its own questions, so two channels are taped instead:
# - "model:<id>"       every model step, keyed by the full request (system prompt, messages, tools)
# - "tools:<ns>"       every tool answer, recorded AFTER our code has summarised and redacted it,
#                      so a recording never holds raw cluster data
# Identical calls (the same tool in every repeat) replay in the order they were recorded.

def call_key(channel: str, args: tuple) -> str:
    raw = json.dumps([channel, list(args)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class TapedModel:
    """Stands in for BedrockModel in agent runs: same model_id, converse_tools goes through the tape."""
    model_id: str
    converse_tools: Callable


class TapeRecorder:
    def __init__(self, path: Path):
        self.final, self.path = path, _start(path)
        self.calls: dict[str, list] = {}

    def taped(self, channel: str, fn: Callable) -> Callable:
        """Wrap fn so every call passes through and its answer is kept."""
        def wrapper(*args):
            out = fn(*args)
            # Store a copy now: the agent later edits objects it got back (e.g. a guardrail override).
            self.calls.setdefault(call_key(channel, args), []).append(json.loads(json.dumps(out)))
            return out
        return wrapper

    def model(self, model) -> TapedModel:
        return TapedModel(model.model_id, self.taped(f"model:{model.model_id}", model.converse_tools))

    def save(self, summary: dict) -> None:
        meta = {k: summary[k] for k in ("model", "prompt_version", "evidence_version", "rag", "repeats")}
        meta |= {"mode": "agent", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "scenarios": [row["scenario"] for row in summary["per_scenario"]]}
        (self.path / "meta.json").write_text(json.dumps(meta, indent=2))
        (self.path / "tape.json").write_text(json.dumps(self.calls, indent=1, ensure_ascii=False))
        _finish(self.path, self.final)


class TapePlayer:
    def __init__(self, path: Path):
        self.meta = read_meta(path)
        self.calls = json.loads((path / "tape.json").read_text())
        self._used: dict[str, int] = {}

    def taped(self, channel: str, fn: Callable | None = None) -> Callable:
        """A function that answers from the tape (fn, the real thing, is ignored: nothing live is touched)."""
        def replay(*args):
            key = call_key(channel, args)
            answers = self.calls.get(key)
            if not answers:
                raise NotRecorded(f"{channel} call {key} not in the recording: {RERECORD_HINT.replace('make record', 'make record-agent')}")
            n = self._used.get(key, 0)
            if n >= len(answers):
                raise NotRecorded(f"{channel} call {key} made more times than recorded: "
                                  + RERECORD_HINT.replace("make record", "make record-agent"))
            self._used[key] = n + 1
            return copy.deepcopy(answers[n])     # a fresh copy, so the agent can't alter the tape
        return replay

    def model(self) -> TapedModel:
        return TapedModel(self.meta["model"], self.taped(f"model:{self.meta['model']}"))


# ---------- the LLM judge: a cache of verdicts, shared by every recording ----------

JUDGE_CACHE = RECORDINGS_DIR / "judge" / "cache.json"


class JudgeCache:
    """Remembers the judge's verdict for each exact request, so the same answer is only ever paid for once.

    A verdict depends only on what the judge is sent (judge model, rubric, reference, answer), so
    one cache serves the one-shot and agent recordings alike. The key covers the rubric, so a new
    rubric starts with an empty cache rather than reusing old verdicts.

    offline=True (CI): never call the judge; a verdict that isn't cached is an error, like any
    other stale recording.
    """

    def __init__(self, path: Path | None = None, offline: bool = False):
        self.path, self.offline = path or JUDGE_CACHE, offline
        self.verdicts: dict[str, dict] = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.new = 0

    def wrap(self, model, model_id: str | None = None):
        """A stand-in for the judge model whose converse() answers from the cache when it can.

        Offline there is no real model, so model_id says which judge's verdicts to look up.
        """
        cache = self
        judge_id = model.model_id if model else model_id   # outside the class: class bodies can't see this function's names they reassign

        class Cached:
            model_id = judge_id

            def converse(self, system: str, messages: list[dict]) -> tuple[str, dict]:
                key = fingerprint(self.model_id, system, messages)
                if key in cache.verdicts:
                    hit = cache.verdicts[key]
                    return hit["text"], {"input_tokens": 0, "output_tokens": 0}   # nothing spent
                if cache.offline or model is None:
                    raise NotRecorded(f"judge verdict {key} not cached: an answer or the rubric changed; "
                                      "refresh the cache with `make judge-cache`")
                text, usage = model.converse(system, messages)
                cache.verdicts[key] = {"text": text}
                cache.new += 1
                return text, usage
        return Cached()

    def save(self) -> None:
        if self.new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.verdicts, indent=1, sort_keys=True, ensure_ascii=False))
