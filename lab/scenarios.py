"""Load and validate scenario definitions.

A scenario is a directory under scenarios/ containing:
  expected.yaml  ground truth (schema below)
  break.yaml     manifests that put the workload into its broken state
  base.yaml      optional healthy revision applied first, so rollback is real
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "scenarios"
NAMESPACE_PREFIX = "km-"

# The agent's future action vocabulary. Kept here so scenarios and the
# Phase 3 executor share one list. "escalate" means "hand to a human, act on nothing".
ACTIONS = ["rollout_restart", "rollback", "scale", "escalate"]
CATEGORIES = ["image", "resources", "config", "secret", "probe", "scheduling"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "title", "category", "difficulty", "target", "observable",
        "root_cause", "evidence_keywords", "acceptable_actions", "forbidden_actions",
    ],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9-]+$"},
        "title": {"type": "string", "minLength": 10},
        "category": {"enum": CATEGORIES},
        "difficulty": {"enum": ["easy", "medium", "hard"]},
        "target": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "name", "selector"],
            "properties": {
                "kind": {"const": "Deployment"},
                "name": {"type": "string"},
                "selector": {"type": "string", "pattern": "^[a-z0-9.-]+=[a-z0-9.-]+$"},
            },
        },
        "observable": {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                "waiting_reason": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "terminated_reason": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "pod_condition": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "status"],
                    "properties": {
                        "type": {"type": "string"},
                        "status": {"enum": ["True", "False", "Unknown"]},
                        "reason": {"type": "string"},
                    },
                },
                "event": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["reason"],
                    "properties": {
                        "reason": {"type": "string"},
                        "message_contains": {"type": "string"},
                    },
                },
            },
        },
        "root_cause": {"type": "string", "minLength": 30},
        "evidence_keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "acceptable_actions": {"type": "array", "items": {"enum": ACTIONS}, "minItems": 1, "uniqueItems": True},
        "forbidden_actions": {"type": "array", "items": {"enum": ACTIONS}, "uniqueItems": True},
        "notes": {"type": "string"},
    },
}


@dataclass
class Scenario:
    id: str
    path: Path
    spec: dict
    has_base: bool = field(default=False)

    @property
    def namespace(self) -> str:
        return f"{NAMESPACE_PREFIX}{self.id}"

    @property
    def selector(self) -> str:
        return self.spec["target"]["selector"]

    @property
    def deployment(self) -> str:
        return self.spec["target"]["name"]

    @property
    def base_file(self) -> Path:
        return self.path / "base.yaml"

    @property
    def break_file(self) -> Path:
        return self.path / "break.yaml"


def load(scenario_id: str, root: Path = SCENARIOS_DIR) -> Scenario:
    path = root / scenario_id
    expected = path / "expected.yaml"
    if not expected.is_file():
        raise FileNotFoundError(f"no scenario '{scenario_id}' (missing {expected})")
    spec = yaml.safe_load(expected.read_text())
    return Scenario(id=scenario_id, path=path, spec=spec, has_base=(path / "base.yaml").is_file())


def load_all(root: Path = SCENARIOS_DIR) -> list[Scenario]:
    return [load(p.name, root) for p in sorted(root.iterdir()) if (p / "expected.yaml").is_file()]


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def validate(s: Scenario) -> list[str]:
    """Return a list of problems; empty means valid."""
    errors = [f"{s.id}: schema: {e.json_path}: {e.message}"
              for e in Draft202012Validator(SCHEMA).iter_errors(s.spec)]
    if errors:
        return errors

    spec = s.spec
    if spec["id"] != s.path.name:
        errors.append(f"{s.id}: id '{spec['id']}' does not match directory name")
    overlap = set(spec["acceptable_actions"]) & set(spec["forbidden_actions"])
    if overlap:
        errors.append(f"{s.id}: actions both acceptable and forbidden: {sorted(overlap)}")
    if "rollback" in spec["acceptable_actions"] and not s.has_base:
        errors.append(f"{s.id}: rollback is acceptable but there is no base.yaml to roll back to")
    if not s.break_file.is_file():
        errors.append(f"{s.id}: missing break.yaml")
        return errors

    # The target Deployment must exist in break.yaml and its pods must carry the selector label.
    key, value = s.selector.split("=", 1)
    target = [d for d in _docs(s.break_file)
              if d.get("kind") == "Deployment" and d["metadata"]["name"] == s.deployment]
    if not target:
        errors.append(f"{s.id}: Deployment '{s.deployment}' not found in break.yaml")
    else:
        labels = target[0]["spec"]["template"]["metadata"].get("labels", {})
        if labels.get(key) != value:
            errors.append(f"{s.id}: pod template labels do not match selector {s.selector}")

    # Manifests must not hard-code a namespace: the lab owns namespace placement,
    # which is what keeps a scenario from ever landing somewhere it shouldn't.
    for f in [s.break_file] + ([s.base_file] if s.has_base else []):
        for d in _docs(f):
            if d.get("kind") == "Namespace" or d.get("metadata", {}).get("namespace"):
                errors.append(f"{s.id}: {f.name} sets a namespace; the lab assigns {s.namespace}")
    return errors
