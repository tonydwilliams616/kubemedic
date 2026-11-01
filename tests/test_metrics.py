"""Checks metrics are rendered in Prometheus format, pushed to the right Pushgateway group (to a local fake gateway), reflect the audit log faithfully, and never break a run."""
import base64
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from evals.fixrate import push_fixrate
from evals.run import push_eval
from kubemedic.audit import AuditLog, verify
from kubemedic.metrics import Sample, audit_samples, group_path, push, render


@contextmanager
def gateway(status=200):
    """A fake Pushgateway on localhost that records every push."""
    pushes = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):  # noqa: N802
            pushes.append({"path": self.path, "body": self.rfile.read(int(self.headers["Content-Length"])).decode(),
                           "type": self.headers["Content-Type"]})
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", pushes
    finally:
        httpd.shutdown()
        httpd.server_close()


def decode(path):
    """'/metrics/job@base64/<b64>/model@base64/<b64>' -> {'job': ..., 'model': ...}"""
    parts = path.split("/")[2:]
    return {parts[i].removesuffix("@base64"): base64.urlsafe_b64decode(parts[i + 1]).decode()
            for i in range(0, len(parts), 2)}


def test_render_is_prometheus_text_format():
    text = render([Sample("kubemedic_eval_pass_rate", 0.86, {"model": 'm"1', "mode": "agent"}, "Share passed"),
                   Sample("kubemedic_eval_pass_rate", 1, {"model": "m2", "mode": "agent"}),
                   Sample("bad-name.x", 3)])
    lines = text.splitlines()
    assert lines[:2] == ["# HELP kubemedic_eval_pass_rate Share passed", "# TYPE kubemedic_eval_pass_rate gauge"]
    assert lines[2] == 'kubemedic_eval_pass_rate{mode="agent",model="m\\"1"} 0.86'
    assert lines.count("# TYPE kubemedic_eval_pass_rate gauge") == 1      # described once
    assert "bad_name_x 3.0" in lines and text.endswith("\n")


def test_group_path_survives_slashes_and_colons_in_model_ids():
    path = group_path("kubemedic_eval", {"model": "openai:Qwen/Qwen3-8B", "rag": "true"})
    assert decode(path) == {"job": "kubemedic_eval", "model": "openai:Qwen/Qwen3-8B", "rag": "true"}


def test_push_is_off_unless_configured():
    saved = os.environ.pop("KUBEMEDIC_PUSHGATEWAY", None)
    try:
        assert push("j", {}, [Sample("x", 1)]) is False
    finally:
        if saved is not None:
            os.environ["KUBEMEDIC_PUSHGATEWAY"] = saved


def test_push_replaces_the_group_on_the_gateway():
    with gateway() as (url, pushes):
        assert push("kubemedic_eval", {"model": "m"}, [Sample("kubemedic_eval_unsafe", 0)], url=url)
    assert decode(pushes[0]["path"]) == {"job": "kubemedic_eval", "model": "m"}
    assert "kubemedic_eval_unsafe 0.0" in pushes[0]["body"] and pushes[0]["type"].startswith("text/plain")


def test_a_failed_push_warns_but_never_raises(capsys):
    with gateway(status=500) as (url, _):
        assert push("j", {}, [Sample("x", 1)], url=url) is False
    assert push("j", {}, [Sample("x", 1)], url="http://127.0.0.1:9", timeout_s=2) is False


def test_audit_metrics_mirror_the_log(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for event, extra in [("proposed", {}), ("approved", {"action": "rollback", "approver": "anthony"}),
                         ("executed", {}), ("verified", {}), ("proposed", {}), ("escalated", {}),
                         ("approved", {"action": "rollback", "approver": "lab-eval (kind only)"})]:
        log.append(event, **extra)
    samples = {(s.name, tuple(sorted(s.labels.items()))): s.value for s in audit_samples(log.records(), verify(log.path))}
    assert samples[("kubemedic_audit_events", (("event", "proposed"),))] == 2
    assert samples[("kubemedic_audit_events", (("event", "verified"),))] == 1
    assert samples[("kubemedic_audit_approvals", (("action", "rollback"), ("approver", "anthony")))] == 1
    assert samples[("kubemedic_audit_records", ())] == 7 and samples[("kubemedic_audit_chain_intact", ())] == 1


def test_a_tampered_audit_log_shows_as_broken(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("approved", approver="anthony")
    log.append("executed")
    lines = log.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["approver"] = "someone else"
    log.path.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    intact = [s for s in audit_samples(log.records(), verify(log.path)) if s.name == "kubemedic_audit_chain_intact"]
    assert intact[0].value == 0


SUMMARY = {"model": "openai:Qwen/Qwen3-8B", "mode": "agent", "rag": True, "repeats": 1, "runs": 7,
           "pass_rate": 0.86, "root_cause_accuracy": 0.86, "action_accuracy": 1.0, "unsafe": 0, "invalid": 0,
           "input_tokens": 70_000, "output_tokens": 7_000, "mean_latency_s": 6.0,
           "per_scenario": [{"scenario": "oom-killed", "passed": 1, "repeats": 1}]}


def test_eval_results_are_pushed_per_model_mode_and_rag():
    with gateway() as (url, pushes):
        assert push_eval(SUMMARY, url=url)
    assert decode(pushes[0]["path"]) == {"job": "kubemedic_eval", "model": "openai:Qwen/Qwen3-8B",
                                         "mode": "agent", "rag": "true"}
    body = pushes[0]["body"]
    assert "kubemedic_eval_pass_rate 0.86" in body and "kubemedic_eval_unsafe 0.0" in body
    assert "kubemedic_eval_cost_per_diagnosis_usd" in body          # GPU-time cost for the self-hosted model
    assert "scenario" not in body                                    # per-scenario details stay in MLflow


def test_fixrate_results_are_pushed():
    summary = {"model": "us.amazon.nova-lite-v1:0", "mode": "one-shot", "rag": True, "fix_rate": 1.0, "fixed": 4,
               "fixable": 4, "restraint_rate": 1.0, "restrained": 3, "restraint_scenarios": 3,
               "harmful_changes": 0, "skipped": 0}
    with gateway() as (url, pushes):
        assert push_fixrate(summary, url=url)
    assert decode(pushes[0]["path"])["job"] == "kubemedic_fixrate"
    assert "# HELP kubemedic_fixrate_harmful_changes Changes the answer key forbids; must be 0" in pushes[0]["body"]
    assert "kubemedic_fixrate_harmful_changes 0.0" in pushes[0]["body"]
