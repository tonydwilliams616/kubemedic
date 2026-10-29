"""Loads the runbooks in runbooks/*.md (the agent's manual) and checks each one follows the format in runbooks/README.md."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .contract import ACTIONS

RUNBOOKS_DIR = Path(__file__).resolve().parent.parent / "runbooks"
REQUIRED_SECTIONS = ["Symptoms", "Likely causes", "How to confirm", "Mitigation", "Permanent fix (needs a human)"]
MAX_CHARS = 4000


@dataclass
class Runbook:
    id: str
    title: str
    signals: list[str]
    mitigations: list[str]
    related: list[str]
    body: str
    path: Path

    @property
    def sections(self) -> list[str]:
        return re.findall(r"^## (.+)$", self.body, re.MULTILINE)


def parse(path: Path) -> Runbook:
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.name}: missing '---' front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    return Runbook(id=meta.get("id", ""), title=meta.get("title", ""), signals=meta.get("signals", []),
                   mitigations=meta.get("mitigations", []), related=meta.get("related", []),
                   body=match.group(2).strip(), path=path)


def load_all(root: Path = RUNBOOKS_DIR) -> list[Runbook]:
    return [parse(p) for p in sorted(root.glob("*.md")) if p.name != "README.md"]


def validate(rb: Runbook, known_ids: set[str]) -> list[str]:
    """Return every way a runbook breaks the format; an empty list means valid."""
    problems = []
    if rb.id != rb.path.stem:
        problems.append(f"id '{rb.id}' does not match file name '{rb.path.stem}'")
    if len(rb.title) < 10:
        problems.append("title missing or too short")
    if not rb.signals:
        problems.append("signals must list at least one search string")
    bad = [m for m in rb.mitigations if m not in ACTIONS]
    if not rb.mitigations or bad:
        problems.append(f"mitigations must be a non-empty subset of {ACTIONS}; bad: {bad}")
    unknown = [r for r in rb.related if r not in known_ids]
    if unknown:
        problems.append(f"related ids not found: {unknown}")
    missing = [s for s in REQUIRED_SECTIONS if s not in rb.sections]
    if missing:
        problems.append(f"missing sections: {missing}")
    if len(rb.body) > MAX_CHARS:
        problems.append(f"body is {len(rb.body)} chars; keep it under {MAX_CHARS}")
    return problems
