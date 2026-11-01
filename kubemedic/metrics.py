"""Publishes kubemedic's numbers (eval quality, cost, latency, and what the audit log says happened) to a Prometheus Pushgateway, so Grafana can chart them.

Why a Pushgateway: Prometheus normally PULLS metrics from long-running services. kubemedic is a
command that runs and exits (an eval, a fix), so there is nothing to pull from. The standard
answer for batch jobs is to PUSH the results to a Pushgateway when the job finishes; Prometheus
scrapes the gateway like any other target.

What is pushed, each as a "group" that the next push of the same group replaces:
- job="kubemedic_eval",    grouped by model/mode/rag: the latest eval's quality, safety, latency,
                           tokens and cost per diagnosis (the same numbers MLflow records).
- job="kubemedic_fixrate", grouped by model/mode/rag: fix rate, restraint, harmful changes.
- job="kubemedic_audit":   totals of every event in the audit log and whether its hash chain is
                           intact. The log is the source of truth; these are just a view of it.

Off unless KUBEMEDIC_PUSHGATEWAY is set (e.g. http://localhost:9091 via `make monitoring-forward`).
A metrics problem never fails the run that produced the numbers: it prints a warning instead.
Standard library only.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ENV = "KUBEMEDIC_PUSHGATEWAY"
NAME = re.compile(r"[^a-zA-Z0-9_]")


@dataclass
class Sample:
    name: str
    value: float
    labels: dict = field(default_factory=dict)
    help: str = ""


def _escape(value: str) -> str:
    return str(value).replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def render(samples: list[Sample]) -> str:
    """Prometheus text exposition format. Everything here is a gauge: a value as of the last push."""
    out, described = [], set()
    for s in samples:
        name = NAME.sub("_", s.name)
        if name not in described:
            out += [f"# HELP {name} {s.help or name}", f"# TYPE {name} gauge"]
            described.add(name)
        labels = ",".join(f'{NAME.sub("_", k)}="{_escape(v)}"' for k, v in sorted(s.labels.items()))
        out.append(f"{name}{{{labels}}} {float(s.value)}" if labels else f"{name} {float(s.value)}")
    return "\n".join(out) + "\n"


def group_path(job: str, grouping: dict) -> str:
    """URL path for a push group. Values are base64url-encoded (the Pushgateway's '@base64' form)
    because model IDs contain '/' and ':' that would otherwise break the path."""
    parts = [f"/metrics/job@base64/{_b64(job)}"]
    for k, v in sorted(grouping.items()):
        parts.append(f"/{NAME.sub('_', k)}@base64/{_b64(v)}")
    return "".join(parts)


def _b64(value) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode() or "="


def push(job: str, grouping: dict, samples: list[Sample], url: str | None = None, timeout_s: float = 5) -> bool:
    """Replace this group's metrics on the Pushgateway. Returns True if pushed; never raises."""
    url = url if url is not None else os.environ.get(ENV)
    if not url:
        return False
    req = urllib.request.Request(url.rstrip("/") + group_path(job, grouping), data=render(samples).encode(),
                                 headers={"Content-Type": "text/plain; version=0.0.4"}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s):
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
        print(f"warning: metrics not pushed to {url} ({e}); is `make monitoring-forward` running?", file=sys.stderr)
        return False


# ---- what gets pushed ---------------------------------------------------------------------

def run_labels(summary: dict) -> dict:
    return {"model": summary["model"], "mode": summary.get("mode", "one-shot"),
            "rag": str(bool(summary.get("rag"))).lower()}


def from_numbers(prefix: str, numbers: dict, help_text: dict | None = None) -> list[Sample]:
    """Every plain number in a dict becomes a gauge; per-scenario and per-tier details are left out."""
    help_text = help_text or {}
    return [Sample(f"{prefix}_{k}", v, help=help_text.get(k, ""))
            for k, v in numbers.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and "/" not in k]


def audit_samples(records: list[dict], problems: list[str]) -> list[Sample]:
    counts = Counter(r.get("event", "unknown") for r in records)
    samples = [Sample("kubemedic_audit_events", n, {"event": e},
                      "Audit log records by event (proposed, approved, executed, verified, ...)")
               for e, n in sorted(counts.items())]
    changes = Counter((r.get("action", "?"), r.get("approver", "?")) for r in records if r.get("event") == "approved")
    samples += [Sample("kubemedic_audit_approvals", n, {"action": a, "approver": who},
                       "Approved changes by action and approver")
                for (a, who), n in sorted(changes.items())]
    samples.append(Sample("kubemedic_audit_records", len(records), help="Records in the audit log"))
    samples.append(Sample("kubemedic_audit_chain_intact", 0 if problems else 1,
                          help="1 if every record links to the one before it (make audit-verify), else 0"))
    return samples


def publish_audit(path: Path | None = None) -> bool:
    from .audit import AuditLog, verify
    log = AuditLog(path)
    return push("kubemedic_audit", {}, audit_samples(log.records(), verify(log.path)))
