"""An append-only, hash-chained audit log of every proposal, approval and change, so any later edit, deletion or reordering is detectable.

Each record is one JSON line. It carries the hash of the record before it, and its own hash
covers its content plus that link, like links in a chain:

    {"seq": 3, "time": ..., "event": "approved", ..., "prev_hash": "<hash of record 2>", "hash": "<hash of this>"}

Change any old record, delete one or swap two, and every hash after it stops matching:
`verify()` (make audit-verify) reports where the chain breaks.

Tamper-EVIDENT, not tamper-PROOF: someone who can write the file could rebuild the whole chain
consistently. Production systems close that gap by shipping each record somewhere the writer
can't modify (for example S3 with Object Lock, or a separate logging account), or by publishing
the latest hash elsewhere. That is noted here rather than pretended away.

Usage: python -m kubemedic.audit verify [--path audit/audit.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

AUDIT_FILE = Path(__file__).resolve().parent.parent / "audit" / "audit.jsonl"
GENESIS = "0" * 64      # the "previous hash" of the very first record


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path or AUDIT_FILE

    def _last(self) -> dict | None:
        if not self.path.is_file():
            return None
        lines = [line for line in self.path.read_text().splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None

    def append(self, event: str, **data) -> dict:
        """Add one record to the end of the chain and return it."""
        last = self._last()
        record = {"seq": (last["seq"] + 1) if last else 1,
                  "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "event": event, **data,
                  "prev_hash": last["hash"] if last else GENESIS}
        record["hash"] = _digest(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:          # append only: existing lines are never rewritten
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def verify(path: Path | None = None) -> list[str]:
    """Walk the chain; return every break found (an empty list means intact)."""
    problems, prev = [], GENESIS
    for i, record in enumerate(AuditLog(path).records(), start=1):
        if record.get("seq") != i:
            problems.append(f"line {i}: sequence number is {record.get('seq')}, expected {i} (a record was removed or moved)")
        if record.get("prev_hash") != prev:
            problems.append(f"line {i}: does not link to the record before it (a record was removed, moved or edited)")
        if _digest(record) != record.get("hash"):
            problems.append(f"line {i}: content does not match its hash (this record was edited)")
        prev = record.get("hash")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check the audit log's hash chain")
    p.add_argument("command", choices=["verify"])
    p.add_argument("--path", type=Path, default=None)
    args = p.parse_args(argv)
    log = AuditLog(args.path)
    problems = verify(log.path)
    for problem in problems:
        print(f"BROKEN: {problem}")
    n = len(log.records())
    print(f"{n} records in {log.path}: " + ("chain intact" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
