"""Shared pytest fixtures and import path setup."""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pr_latency_reporter as plr  # noqa: E402


@pytest.fixture
def cfg():
    """A deterministic, secret-free config for pure-logic tests."""
    return plr.Config(
        gh_token="x",
        gh_org="acme-corp",
        team_slug="your-team",
        slack_bot_token="x",
        slack_channel_id="C0000000000",
        tz_name="Europe/London",
        holidays=frozenset({date(2026, 1, 1)}),
    )
