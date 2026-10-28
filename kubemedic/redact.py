"""Scrubs secrets out of text before it is shown to the LLM, and caps its size.

Why this exists: pod logs, env vars and ConfigMaps can contain credentials. Anything
the agent reads is sent to a model provider, so it must be cleaned first. Redaction
is pattern-based, so it is a safety net, not a guarantee: the first line of defence
is still RBAC (the agent cannot read Secrets at all).
"""
from __future__ import annotations

import re

MASK = "[REDACTED]"

# Env var / config key names whose VALUES should never be shown.
SENSITIVE_NAME = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth)",
    re.IGNORECASE,
)

# (label, pattern). Order matters: specific formats first, generic key=value last.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("aws-access-key-id", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("url-credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s:/@]+:[^\s@/]+@")),
    # key=value / key: value / "key": "value" where the key name looks sensitive.
    ("sensitive-assignment", re.compile(
        r"""(?ix)
        (?P<key>["']?[\w.-]*(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*["']?)
        (?P<sep>\s*[:=]\s*)
        (?P<val>"[^"]*"|'[^']*'|[^\s,;}&]+)
        """)),
]


def redact(text: str) -> tuple[str, int]:
    """Return (cleaned text, number of redactions made)."""
    count = 0

    def sub(label: str, m: re.Match) -> str:
        nonlocal count
        count += 1
        if label == "bearer":
            return f"{m.group(1)} {MASK}"
        if label == "url-credentials":
            return f"{m.group(1)}{MASK}@"
        if label == "sensitive-assignment":
            return f"{m.group('key')}{m.group('sep')}{MASK}"
        return MASK

    for label, pattern in _PATTERNS:
        text = pattern.sub(lambda m, label=label: sub(label, m), text)
    return text, count


def redact_value(name: str, value: str) -> str:
    """Hide a value outright if its name looks sensitive; otherwise pattern-redact it."""
    if SENSITIVE_NAME.search(name):
        return MASK
    return redact(value)[0]


def cap(text: str, max_chars: int = 4000) -> str:
    """Keep the END of long text (the most recent log lines matter most) and say what was cut."""
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    return f"[... {cut} earlier characters omitted ...]\n" + text[-max_chars:]
