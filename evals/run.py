"""Runs the diagnoser against every broken scenario several times, scores each answer, and writes a scorecard.

Design choices:
- Fail fast if any fault isn't actually observable (run `make break-all && make check-all` first),
  so the model is never scored against a cluster that isn't broken yet.
- Gather the evidence ONCE per scenario and reuse it for every repeat. Then the only thing that
  varies between repeats is the model, which is exactly what repeats are meant to measure.
- Several repeats per scenario, because the same input can produce different answers.
- Everything is written to reports/<timestamp>-<model>/ so runs can be compared later.
- --record saves the evidence and every model reply; --replay reruns them through the current
  code with no AWS or cluster (see evals/cassette.py); --gate fails the run if scores drop
  below evals/gate.yaml. CI uses --replay with --gate.

- --agent scores the tool-using agent (kubemedic/agent.py) instead: it gathers its own evidence,
  so there is no bundle, and its recording is a tape of both sides of the conversation.

- --judge grades each root cause with the calibrated LLM judge (evals/judge.py) instead of
  keywords; verdicts are cached in evals/recordings/judge/, and --judge-offline (CI) uses only
  the cache.

Usage: python -m evals.run [--model MODEL_ID] [--repeats 3] [--scenario ID ...] [--rag] [--agent] [--judge]
       python -m evals.run --rag --record evals/recordings/<name>
       python -m evals.run --replay evals/recordings/<name> --gate evals/gate.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from kubemedic.diagnose import DEFAULT_MODEL, PROMPT_VERSION, RAG_PROMPT_VERSION, diagnose
from kubemedic.evidence import EVIDENCE_VERSION
from kubemedic.retrieve import EMBED_MODEL
from lab import scenarios
from lab.scenarios import Scenario

from . import gate
from .cassette import JudgeCache, Player, Recorder, TapePlayer, TapeRecorder, read_meta
from .score import Score, score, summarise

REPORTS_DIR = Path("reports")


def score_row(s: Score) -> dict:
    return asdict(s) | {"root_cause_ok": s.root_cause_ok, "passed": s.passed}


def run_eval(selected: list[Scenario], model, gather_fn: Callable[[str], dict] | None, repeats: int,
             out_dir: Path, check_fn: Callable[[Scenario], tuple[bool, str]] | None = None,
             retrieve_fn: Callable[[dict], list[dict]] | None = None,
             investigate_fn: Callable[[str], object] | None = None, agent_rag: bool = False,
             judge_fn: Callable[[dict, dict], object] | None = None) -> dict:
    """retrieve_fn given = RAG run (prompt v3): pages are retrieved once per scenario, like the evidence.

    investigate_fn given = agent run: called once per repeat with the namespace; the agent gathers
    its own evidence with tools, so gather_fn and retrieve_fn are not used.

    judge_fn given = each valid diagnosis's root cause is graded by the LLM judge: called with the
    scenario's answer key and the diagnosis, it returns a judge Verdict.
    """
    agent = investigate_fn is not None
    rag = agent_rag if agent else bool(retrieve_fn)
    # 1. Every fault must be really there before we spend anything on the model.
    if check_fn:
        not_ready = [(sc.id, detail) for sc in selected for ok, detail in [check_fn(sc)] if not ok]
        if not_ready:
            raise RuntimeError("faults not observable, run `make break-all && make check-all` first: "
                               + "; ".join(f"{i}: {d}" for i, d in not_ready))

    out_dir.mkdir(parents=True, exist_ok=True)
    all_scores: list[Score] = []
    all_results = []
    per_scenario = []
    tokens_in = tokens_out = 0
    judge_tokens, judge_errors = [0, 0], 0
    latencies = []

    for sc in selected:
        bundle = pages = None
        if not agent:   # one-shot: gather the evidence once and reuse it for every repeat
            bundle = gather_fn(sc.namespace)
            (out_dir / f"{sc.id}.bundle.json").write_text(json.dumps(bundle, indent=1))
            pages = retrieve_fn(bundle) if retrieve_fn else None
            if pages:
                (out_dir / f"{sc.id}.runbooks.json").write_text(
                    json.dumps([{k: p[k] for k in ("id", "score")} for p in pages], indent=1))
        scores, results = [], []
        for r in range(1, repeats + 1):
            result = investigate_fn(sc.namespace) if agent else diagnose(bundle, model, runbooks=pages)
            results.append(result)
            s = score(result.diagnosis, sc.spec)
            verdict = None
            if judge_fn and s.valid:
                verdict = judge_fn(sc.spec, result.diagnosis)
                s.judge_verdict = verdict.correct      # None if the judge failed: keywords are used
                judge_tokens[0] += verdict.input_tokens
                judge_tokens[1] += verdict.output_tokens
                judge_errors += verdict.correct is None
            scores.append(s)
            tokens_in += result.input_tokens
            tokens_out += result.output_tokens
            latencies.append(result.latency_s)
            (out_dir / f"{sc.id}.run{r}.json").write_text(
                json.dumps({"result": asdict(result), "score": score_row(s),
                            "judge": asdict(verdict) if verdict else None}, indent=2))
        all_scores += scores
        all_results += results
        read = sorted({c["args"]["id"] for res in results for c in res.tool_calls
                       if c["tool"] == "read_runbook" and c["ok"]})
        per_scenario.append({
            "scenario": sc.id,
            "passed": sum(s.passed for s in scores),
            "root_cause_ok": sum(s.root_cause_ok for s in scores),
            "keyword_root_cause_ok": sum(s.keyword_root_cause_ok for s in scores),
            "action_ok": sum(s.action_ok for s in scores),
            "unsafe": sum(s.unsafe for s in scores),
            "actions": sorted({s.action or "invalid" for s in scores}),
            "guardrail_blocks": sum(bool(res.guardrail_violations) for res in results),
            "overrides": sum(res.overridden for res in results),
            # The model's own choice was right: correct action that a guardrail did NOT have to override.
            "model_action_ok": sum(s.action_ok and not res.overridden for s, res in zip(scores, results)),
            "retrieved": [p["id"] for p in pages] if pages else (read if agent and rag else None),
            "cited_relevant": sum(bool(s.cited_relevant) for s in scores) if rag else None,
            "mean_steps": round(sum(res.steps for res in results) / len(results), 1) if agent else None,
            "repeats": repeats,
        })

    n = max(len(all_scores), 1)
    if agent:
        from kubemedic.agent import AGENT_PROMPT_VERSION
        version = AGENT_PROMPT_VERSION + ("-rag" if rag else "")
    else:
        version = RAG_PROMPT_VERSION if rag else PROMPT_VERSION
    summary = {
        "model": model.model_id,
        "mode": "agent" if agent else "one-shot",
        "prompt_version": version,
        "evidence_version": EVIDENCE_VERSION,
        "rag": rag,
        "citation_accuracy": round(sum(bool(s.cited_relevant) for s in all_scores) / n, 2) if rag else None,
        "repeats": repeats,
        "runs": len(all_scores),
        **summarise(all_scores),
        "pass_rate": round(sum(s.passed for s in all_scores) / max(len(all_scores), 1), 2),
        # Runs where the model proposed something a guardrail blocked: the "unsafe before guardrails" count.
        "guardrail_blocks": sum(row["guardrail_blocks"] for row in per_scenario),
        "overrides": sum(row["overrides"] for row in per_scenario),
        # System accuracy (action_accuracy) counts guardrail rescues; model accuracy doesn't.
        "model_action_accuracy": round(sum(row["model_action_ok"] for row in per_scenario) / n, 2),
        "mean_steps": round(sum(r.steps for r in all_results) / n, 1) if agent else None,
        "mean_tool_calls": round(sum(len(r.tool_calls) for r in all_results) / n, 1) if agent else None,
        "budget_exhausted": sum(any("budget exhausted" in p for p in r.problems) for r in all_results) if agent else None,
        "repeated_calls": sum(r.repeated_calls for r in all_results) if agent else None,
        "root_cause_grader": f"judge:{judge_fn.rubric}" if judge_fn else "keywords",
        "judge_errors": judge_errors if judge_fn else None,
        "judge_model": judge_fn.model_id if judge_fn else None,
        "judge_input_tokens": judge_tokens[0] if judge_fn else None,
        "judge_output_tokens": judge_tokens[1] if judge_fn else None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "mean_latency_s": round(sum(latencies) / max(len(latencies), 1), 2),
        "per_scenario": per_scenario,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def print_scorecard(summary: dict) -> None:
    n = summary["repeats"]
    print(f"\nmodel: {summary['model']}   mode: {summary.get('mode', 'one-shot')}   "
          f"prompt: {summary['prompt_version']}   repeats: {n}")
    print(f"{'scenario':<28} {'pass':<6} {'cause':<6} {'action':<7} {'unsafe':<7} actions chosen")
    for row in summary["per_scenario"]:
        print(f"{row['scenario']:<28} {row['passed']}/{n:<4} {row['root_cause_ok']}/{n:<4} "
              f"{row['action_ok']}/{n:<5} {row['unsafe']:<7} {', '.join(row['actions'])}")
    if summary.get("rag"):
        label = "runbooks the agent read" if summary.get("mode") == "agent" else "retrieved"
        print(f"\n{label}, and how often the right page was cited:")
        for row in summary["per_scenario"]:
            print(f"  {row['scenario']:<28} cited right page {row['cited_relevant']}/{n}   "
                  f"{label}: {', '.join(row['retrieved'] or []) or 'none'}")
    grader = summary.get("root_cause_grader", "keywords")
    if grader != "keywords":
        print(f"\nroot cause graded by the LLM judge ({grader.split(':')[1]}): {summary['root_cause_accuracy']:.0%} "
              f"| keywords would have said {summary['keyword_root_cause_accuracy']:.0%}"
              + (f" | judge errors {summary['judge_errors']} (keywords used)" if summary.get("judge_errors") else ""))
    print(f"\npass rate {summary['pass_rate']:.0%} | root cause {summary['root_cause_accuracy']:.0%} | "
          f"action {summary['action_accuracy']:.0%} | UNSAFE {summary['unsafe']} | invalid {summary['invalid']}"
          + (f" | citations {summary['citation_accuracy']:.0%}" if summary.get("rag") else ""))
    print(f"guardrails: blocked a proposed action in {summary['guardrail_blocks']} runs "
          f"(model fixed it in {summary['guardrail_blocks'] - summary['overrides']}, overridden to escalate in {summary['overrides']})")
    if "model_action_accuracy" in summary:
        print(f"action accuracy: system {summary['action_accuracy']:.0%} | model alone (no guardrail rescues) "
              f"{summary['model_action_accuracy']:.0%}")
    if summary.get("mode") == "agent":
        print(f"agent: mean steps {summary['mean_steps']} | mean tool calls {summary['mean_tool_calls']} | "
              f"ran out of budget {summary['budget_exhausted']} | repeated questions {summary.get('repeated_calls')}")
    print(f"tokens in/out {summary['input_tokens']}/{summary['output_tokens']} | "
          f"mean latency {summary['mean_latency_s']}s")


def _cluster_check(context: str) -> Callable[[Scenario], tuple[bool, str]]:
    from lab.check import evaluate
    from lab.kube import Kubectl

    k = Kubectl(context)

    def check(sc: Scenario) -> tuple[bool, str]:
        pods = k.get_json("pods", "-n", sc.namespace, "-l", sc.selector)["items"]
        events = k.get_json("events", "-n", sc.namespace)["items"]
        r = evaluate(pods, events, sc.spec["observable"])
        return r.ok, r.detail

    return check


def main(argv: list[str] | None = None) -> int:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:   # a replay needs no AWS libraries at all
        BotoCoreError = ClientError = type("AWSNotInstalled", (Exception,), {})  # noqa: N806

    p = argparse.ArgumentParser(description="Score the diagnoser on every broken scenario")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--scenario", action="append", help="limit to these scenario ids (repeatable)")
    p.add_argument("--context", default="kind-kubemedic")
    p.add_argument("--rag", action="store_true", help="retrieve the top 3 runbooks per scenario (prompt v3)")
    p.add_argument("--agent", action="store_true", help="score the tool-using agent instead of the one-shot diagnoser")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", type=Path, metavar="DIR", help="also save evidence and every model reply to DIR")
    mode.add_argument("--replay", type=Path, metavar="DIR",
                      help="rerun a recording through the current code: no AWS, no cluster, no cost")
    p.add_argument("--gate", type=Path, metavar="FILE", help="exit 1 if the scores break the rules in FILE")
    p.add_argument("--judge", action="store_true", help="grade root causes with the calibrated LLM judge")
    p.add_argument("--judge-offline", action="store_true",
                   help="with --judge: use only cached verdicts and never call the judge (CI)")
    args = p.parse_args(argv)

    selected = [scenarios.load(i) for i in args.scenario] if args.scenario else scenarios.load_all()

    try:
        investigate_fn, agent_rag, recorder, meta = None, False, None, None
        if args.replay:
            meta = read_meta(args.replay)
        if meta and meta.get("mode") == "agent":
            tape = TapePlayer(args.replay)
            model, gather_fn, retrieve_fn, check_fn = tape.model(), None, None, None
            agent_rag, repeats, label = meta["rag"], meta["repeats"], "agent-replay"
            # In a replay only the PRESENCE of a retriever matters (it switches the runbook tools on);
            # the runbook answers themselves come from the tape.
            investigate_fn = _agent_runner(model, None, _REPLAYED_RUNBOOKS if agent_rag else None, tape)
        elif args.replay:
            player = Player(args.replay)
            model, gather_fn, check_fn = player, player.gather, None
            retrieve_fn = player.retrieve if player.meta["rag"] else None
            repeats, label = player.meta["repeats"], "replay"
        else:
            from kubemedic.diagnose import BedrockModel
            from kubemedic.evidence import gather
            model, check_fn, repeats = BedrockModel(args.model), _cluster_check(args.context), args.repeats
            gather_fn = lambda ns: gather(ns, args.context)  # noqa: E731
            retriever = None
            if args.rag:
                from kubemedic.retrieve import Retriever
                retriever = Retriever()
            retrieve_fn = retriever.top_runbooks if retriever else None
            label = "prompt" + (RAG_PROMPT_VERSION if args.rag else PROMPT_VERSION)
            if args.agent:
                from kubemedic.agent import AGENT_PROMPT_VERSION
                from kubemedic.evidence import AgentKubectl
                retrieve_fn, gather_fn, agent_rag = None, None, args.rag   # the agent searches for itself
                label = "agent" + AGENT_PROMPT_VERSION + ("-rag" if args.rag else "")
                if args.record:
                    recorder = TapeRecorder(args.record)
                    model = recorder.model(model)
                investigate_fn = _agent_runner(model, AgentKubectl(args.context), retriever, recorder)
            elif args.record:
                model = recorder = Recorder(model, args.record)
                gather_fn = model.gathering(gather_fn)
                retrieve_fn = model.retrieving(retrieve_fn) if retrieve_fn else None
        safe_model = model.model_id.replace(":", "_").replace("/", "_")
        out_dir = REPORTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_model}-{label}"
        cache = judge_fn = None
        if args.judge:
            cache, judge_fn = _judge(args.judge_offline)
        summary = run_eval(selected, model, gather_fn, repeats, out_dir, check_fn, retrieve_fn,
                           investigate_fn, agent_rag, judge_fn)
        if cache:
            cache.save()
    except RuntimeError as e:   # includes NotRecorded: replay hit something the recording lacks
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BotoCoreError as e:
        print(f"AWS credentials problem: {e}\nFix: source ./aws-login.sh", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"Bedrock error ({args.model}): {e.response['Error']['Message']}", file=sys.stderr)
        return 2

    print_scorecard(summary)
    print(f"\nfull results: {out_dir}/")
    if recorder:
        recorder.save(summary)
        print(f"recording saved: {args.record}/ (commit it so CI replays these answers)")
    if args.replay:
        print(f"replayed: {args.replay}/ recorded {meta['recorded_at']} (latency is not meaningful in a replay)")
    else:  # a replay is a re-check of an old run, not a new experiment, so it isn't tracked
        _track(summary, out_dir, {"embed_model": EMBED_MODEL} if args.rag else None)
    if args.gate:
        failures = gate.check(summary, gate.load(args.gate))
        for f in failures:
            print(f"GATE FAIL: {f}", file=sys.stderr)
        if failures:
            return 1
        print(f"gate passed ({args.gate})")
    return 0


_REPLAYED_RUNBOOKS = object()   # stands in for a Retriever in agent replays; see main()


def _judge(offline: bool):
    """The judge as run_eval wants it: (answer key, diagnosis) -> Verdict, answering from the cache first."""
    from .judge import JUDGE_MODEL, JUDGE_PROMPT_VERSION, judge

    cache = JudgeCache(offline=offline)
    if offline:
        model = cache.wrap(None, JUDGE_MODEL)
    else:
        from kubemedic.diagnose import BedrockModel
        model = cache.wrap(BedrockModel(JUDGE_MODEL))

    def judge_fn(spec: dict, diagnosis: dict):
        return judge(spec["root_cause"], diagnosis, model)
    judge_fn.rubric, judge_fn.model_id = JUDGE_PROMPT_VERSION, JUDGE_MODEL
    return cache, judge_fn


def _agent_runner(model, kubectl, retriever, tape=None) -> Callable[[str], object]:
    """Build the function run_eval calls once per repeat: a fresh Toolbox, then one investigation.

    With a tape (recording or replay), the toolbox's backend goes through it, so the agent's
    questions are answered live-and-recorded, or from the recording.
    """
    from kubemedic.agent import investigate
    from kubemedic.tools import Toolbox

    def run(namespace: str):
        toolbox = Toolbox(namespace, kubectl, retriever)
        if tape:
            toolbox.backend = tape.taped(f"tools:{namespace}", toolbox.backend)
        return investigate(toolbox, model)
    return run


def _track(summary: dict, out_dir: Path, extra: dict | None) -> None:
    """Log the run to MLflow; a tracking problem must never fail an eval that already ran."""
    from .tracking import log_eval
    try:
        run_id = log_eval(summary, out_dir, extra)
        if run_id:
            print(f"tracked in MLflow: run {run_id} (make mlflow-ui)")
    except Exception as e:  # noqa: BLE001
        print(f"warning: MLflow tracking failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
