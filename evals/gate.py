"""Decides whether an eval result is good enough to merge, using the minimum scores in evals/gate.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

GATE_FILE = Path(__file__).resolve().parent / "gate.yaml"


def load(path: Path = GATE_FILE) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def check(summary: dict, rules: dict) -> list[str]:
    """Return every rule the summary breaks; an empty list means the gate is passed.

    A metric that is missing from the summary counts as a failure: a gate must never pass
    just because something stopped being measured.
    """
    failures = []
    for metric, rule in rules.items():
        value = summary.get(metric)
        if value is None:
            failures.append(f"{metric}: not measured")
            continue
        if "min" in rule and value < rule["min"]:
            failures.append(f"{metric} {value} is below the minimum {rule['min']}")
        if "max" in rule and value > rule["max"]:
            failures.append(f"{metric} {value} is above the maximum {rule['max']}")
    return failures
