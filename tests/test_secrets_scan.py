"""Guardrails: no committed secrets, and no employer-specific placeholders."""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Core files with employer-specific placeholder checks.
PLACEHOLDER_FILES = [
    "pr_latency_reporter.py",
    "README.md",
    "pyproject.toml",
    "ruff.toml",
]

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".js", ".mjs", ".ts"}


def scan_files():
    """Return project text files, including optional marketplace scaffolds."""
    found = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in {".git", ".venv", "node_modules"}]
        for fname in files:
            path = os.path.join(current, fname)
            if os.path.splitext(fname)[1] in TEXT_SUFFIXES:
                found.append(os.path.relpath(path, ROOT))
    return sorted(found)


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
    for name in scan_files():
        text = _read(name)
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"possible secret in {name}: {pattern.pattern}"


def test_tests_dir_has_no_secrets():
    for name in scan_files():
        if "/tests/" not in f"/{name}/" and not name.startswith("tests/"):
            continue
        text = _read(name)
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"possible secret in {name}: {pattern.pattern}"


def test_core_files_have_no_employer_specific_placeholders():
    for name in PLACEHOLDER_FILES:
        text = _read(name)
        for token in FORBIDDEN_PLACEHOLDERS:
            pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
            assert not pattern.search(text), f"forbidden placeholder '{token}' in {name}"
