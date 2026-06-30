"""Guardrails: no committed secrets, and no employer-specific placeholders."""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Files we scan: the source, tests, README, configs.
SCAN_FILES = [
    "pr_latency_reporter.py",
    "README.md",
    "pyproject.toml",
    "ruff.toml",
]

SECRET_PATTERNS = [
    re.compile(r"xoxb-[A-Za-z0-9-]{10,}"),  # Slack bot token
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub PAT
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # fine-grained PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

# Employer-specific strings that must never be carried through from the spec.
FORBIDDEN_PLACEHOLDERS = ["pass-team", "Europe/Dublin", "prime_to_pass", "IST"]


def _read(name):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_no_committed_secrets():
    for name in SCAN_FILES:
        text = _read(name)
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"possible secret in {name}: {pattern.pattern}"


def test_no_employer_specific_placeholders():
    for name in SCAN_FILES:
        text = _read(name)
        for token in FORBIDDEN_PLACEHOLDERS:
            # Word-boundary match so e.g. "IST" does not hit "EXISTS".
            pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
            assert not pattern.search(text), f"forbidden placeholder '{token}' in {name}"


def test_tests_dir_has_no_secrets():
    tests_dir = os.path.join(ROOT, "tests")
    for fname in os.listdir(tests_dir):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(tests_dir, fname), encoding="utf-8") as fh:
            text = fh.read()
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"possible secret in tests/{fname}"
