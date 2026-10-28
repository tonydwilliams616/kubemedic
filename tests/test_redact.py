"""Checks secrets are scrubbed from text before the LLM sees it, and ordinary diagnostic text is left alone."""
import pytest

from kubemedic.redact import MASK, cap, redact, redact_value

# Fake credentials, split so secret scanners don't flag this test file.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
GH = "ghp_" + "a" * 36


@pytest.mark.parametrize("text, secret", [
    (f"using key {AWS_KEY} for upload", AWS_KEY),
    (f"Authorization: Bearer {JWT}", JWT),
    (f"token={GH}", GH),
    ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ('{"api_key": "sk-live-123456"}', "sk-live-123456"),
    ("client_secret: abc123def456", "abc123def456"),
    ("connecting to postgres://admin:s3cr3t@db:5432/app", "s3cr3t"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
])
def test_secrets_are_removed(text, secret):
    cleaned, count = redact(text)
    assert secret not in cleaned
    assert MASK in cleaned
    assert count >= 1


@pytest.mark.parametrize("text", [
    "FATAL: config file /etc/app/settings.yaml not found",
    "Readiness probe failed: HTTP probe failed with statuscode: 404",
    "0/2 nodes are available: 2 Insufficient cpu.",
    "Failed to pull image \"nginx:1.27.99-alpine\": not found",
    "image sha256:4c0fdaa8b6341bfdeca5f18f7837462c80cff90527ee35ef185571e1c327beac",
    "worker started, cache=200MB",
    "Error: secret \"payments-db\" not found",
])
def test_diagnostic_text_is_unchanged(text):
    assert redact(text) == (text, 0)


def test_key_name_is_kept_so_the_model_still_sees_what_was_set():
    cleaned, _ = redact("DB_PASSWORD=hunter2hunter2")
    assert cleaned == f"DB_PASSWORD={MASK}"


def test_redact_value_hides_sensitive_names_outright():
    assert redact_value("DB_PASSWORD", "anything") == MASK
    assert redact_value("API_TOKEN", "x") == MASK
    assert redact_value("CONFIG_PATH", "/etc/app/settings.yaml") == "/etc/app/settings.yaml"


def test_cap_keeps_the_end_and_says_how_much_was_cut():
    text = "a" * 100 + "LAST LINE"
    out = cap(text, max_chars=20)
    assert out.endswith("LAST LINE")
    assert "89 earlier characters omitted" in out
    assert cap("short", max_chars=20) == "short"
