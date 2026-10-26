"""Thin kubectl wrapper. Every call is pinned to an explicit --context."""
from __future__ import annotations

import json
import subprocess


class KubeError(RuntimeError):
    pass


def ensure_safe_context(context: str, allow_non_kind: bool) -> None:
    """The lab deliberately breaks workloads. Refuse to point it at anything
    but a local kind cluster unless the caller explicitly opts in."""
    if not context.startswith("kind-") and not allow_non_kind:
        raise KubeError(
            f"refusing to inject faults into context '{context}': not a kind cluster. "
            "Pass --allow-non-kind only for a disposable test cluster."
        )


class Kubectl:
    def __init__(self, context: str):
        self.context = context

    def run(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        cmd = ["kubectl", "--context", self.context, *args]
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise KubeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def get_json(self, *args: str) -> dict:
        return json.loads(self.run("get", *args, "-o", "json"))

    def apply_namespace(self, name: str, labels: dict[str, str]) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name, "labels": labels},
        }
        self.run("apply", "-f", "-", stdin=json.dumps(manifest))
