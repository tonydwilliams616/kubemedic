"""Checks the audit log is an unbroken hash chain, and that editing, deleting or reordering any record is detected."""
import json
import tempfile
from pathlib import Path

from kubemedic.audit import GENESIS, AuditLog, verify


def log_with(n=3):
    log = AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl")
    for i in range(n):
        log.append("event", step=i)
    return log


def lines(log):
    return log.path.read_text().splitlines()


def test_records_link_into_a_chain():
    log = log_with(3)
    r = log.records()
    assert [x["seq"] for x in r] == [1, 2, 3] and r[0]["prev_hash"] == GENESIS
    assert r[1]["prev_hash"] == r[0]["hash"] and r[2]["prev_hash"] == r[1]["hash"]
    assert verify(log.path) == []


def test_editing_a_record_is_detected():
    log = log_with(3)
    ls = lines(log)
    edited = json.loads(ls[1]) | {"step": 99}           # someone "corrects" what happened
    log.path.write_text("\n".join([ls[0], json.dumps(edited), ls[2]]) + "\n")
    assert any("line 2: content does not match its hash" in p for p in verify(log.path))


def test_deleting_a_record_is_detected():
    log = log_with(3)
    ls = lines(log)
    log.path.write_text("\n".join([ls[0], ls[2]]) + "\n")
    problems = verify(log.path)
    assert any("line 2" in p and "removed" in p for p in problems)


def test_reordering_records_is_detected():
    log = log_with(3)
    ls = lines(log)
    log.path.write_text("\n".join([ls[0], ls[2], ls[1]]) + "\n")
    assert verify(log.path)


def test_an_empty_or_missing_log_is_intact_and_new_records_append():
    log = AuditLog(Path(tempfile.mkdtemp()) / "sub" / "audit.jsonl")
    assert verify(log.path) == [] and log.records() == []
    log.append("first")
    log2 = AuditLog(log.path)                            # a later process keeps extending the same chain
    log2.append("second")
    assert [r["seq"] for r in log2.records()] == [1, 2] and verify(log.path) == []
