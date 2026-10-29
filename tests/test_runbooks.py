"""Checks every runbook follows the format, offers only menu actions, and contains nothing copied from the test scenarios."""
import re

import pytest

from kubemedic.runbooks import load_all, validate
from lab import scenarios

RUNBOOKS = load_all()
IDS = {r.id for r in RUNBOOKS}


@pytest.mark.parametrize("rb", RUNBOOKS, ids=lambda r: r.id)
def test_runbook_follows_the_format(rb):
    assert validate(rb, IDS) == []


def test_there_are_more_runbooks_than_scenarios():
    # Lookalike pages make retrieval a real choice, not a one-to-one lookup.
    assert len(RUNBOOKS) > len(scenarios.load_all())


def test_runbooks_do_not_contain_scenario_specific_details():
    """Anti-overfitting guard: runbooks must be general knowledge, not answers to our test set.

    Collects names and distinctive values from every scenario (deployment names, Secret and
    ConfigMap names, image tags, env var names and values, file paths) and fails if any appears
    in a runbook. Generic Kubernetes terms (OOMKilled, CrashLoopBackOff ...) and plain English
    words are fine.
    """
    import yaml

    banned = set()
    for sc in scenarios.load_all():
        banned.add(sc.deployment)
        for f in [sc.break_file] + ([sc.base_file] if sc.has_base else []):
            for doc in yaml.safe_load_all(f.read_text()):
                if not doc:
                    continue
                banned.add(doc["metadata"]["name"])
                for c in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                    banned.add(c["image"])
                    for e in c.get("env", []):
                        banned.add(e["name"])
                        banned.update(re.findall(r"/[\w./-]+\.\w+", e.get("value", "")))  # file paths
                        ref = e.get("valueFrom", {}).get("secretKeyRef", {})
                        banned.add(ref.get("name", ""))
    # Keep only things that look like identifiers (contain a digit, _ - . / : or are ALL CAPS).
    # Plain words such as "worker" or "reports" are deployment names here but also normal English.
    banned = {b for b in banned if b and (re.search(r"[\d_./:-]", b) or b.isupper())}
    leaks = [(rb.id, b) for rb in RUNBOOKS for b in banned
             if re.search(rf"(?<![\w-]){re.escape(b)}(?![\w-])", rb.body + " ".join(rb.signals))]
    assert leaks == [], f"scenario details found in runbooks: {leaks}"
