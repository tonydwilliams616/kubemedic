"""Decides whether an eval result is good enough to merge, using the minimum scores in evals/gate.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

GATE_FILE = Path(__file__).resolve().parent / "gate.yaml"


def load(path: Path = GATE_FILE) -> dict:
    return yaml.safe_load(path.read_text()) or {}


SECTIONS = ("core", "all")


def check(summary: dict, rules: dict) -> list[str]:
    """Return every rule the summary breaks; an empty list means the gate is passed.

    "all" rules are checked against the whole run; "core" rules against the core scenarios only
    (summary["by_tier"]["core"]). A metric that is missing counts as a failure: a gate must
    never pass just because something stopped being measured.
    """
    failures = []
    for section, section_rules in rules.items():
        if section not in SECTIONS:
            failures.append(f"unknown gate section {section!r}; use one of {SECTIONS}")
            continue
        measured = summary if section == "all" else summary.get("by_tier", {}).get(section)
        if measured is None:
            failures.append(f"{section}: no {section} scenarios were measured")
            continue
        for metric, rule in section_rules.items():
            value = measured.get(metric)
            if value is None:
                failures.append(f"{section} {metric}: not measured")
                continue
            if "min" in rule and value < rule["min"]:
                failures.append(f"{section} {metric} {value} is below the minimum {rule['min']}")
            if "max" in rule and value > rule["max"]:
                failures.append(f"{section} {metric} {value} is above the maximum {rule['max']}")
    return failures
