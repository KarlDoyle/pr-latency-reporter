"""Tests for the read-only JSON API that feeds the Forge dashboard.

No network — writes directly to a temp-file SQLite store via the same
Store/PRRecord/ReviewEvent primitives pr_latency_reporter.py uses, then
asserts the payload builders in api_server.py compute the right numbers.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import api_server as api  # noqa: E402

import pr_latency_reporter as plr  # noqa: E402


def seeded_cfg(tmp_path):
    cfg = plr.Config(
        gh_token="x",
        gh_org="acme-corp",
        team_slug="your-team",
        team_slugs=("checkout-team", "mobile-team"),
        data_dir=str(tmp_path),
        reviewer_seniority={"alice": "senior"},
    )
    store = plr.Store(cfg.db_path)
    try:
        now = plr.datetime.now(tz=cfg.tz)
        anchor_iso = plr.to_iso8601(now)
        store.upsert_pr(
            plr.PRRecord(
                repo="acme-corp/checkout",
                number=1,
                title="t1",
                author="x",
                html_url="https://example.test/1",
                created_at=anchor_iso,
                review_requested_at=anchor_iso,
                first_response_at=anchor_iso,
                first_approval_at=anchor_iso,
                merged_at=anchor_iso,
                team_slug="checkout-team",
                first_approver_login="alice",
                ttfr_bh=1.0,
                tta_bh=2.0,
                sla_response_met=1,
                sla_approval_met=1,
            ),
            anchor_iso,
        )
        store.upsert_pr(
            plr.PRRecord(
                repo="acme-corp/mobile",
                number=2,
                title="t2",
                author="x",
                html_url="https://example.test/2",
                created_at=anchor_iso,
                review_requested_at=anchor_iso,
                first_response_at=anchor_iso,
                first_approval_at=anchor_iso,
                merged_at=anchor_iso,
                team_slug="mobile-team",
                first_approver_login="bob",
                ttfr_bh=3.0,
                tta_bh=6.0,
                sla_response_met=0,
                sla_approval_met=0,
            ),
            anchor_iso,
        )
        store.replace_reviews(
            "acme-corp/checkout",
            1,
            [
                plr.ReviewEvent(
                    repo="acme-corp/checkout",
                    number=1,
                    reviewer="alice",
                    state="APPROVED",
                    submitted_at=anchor_iso,
                    response_bh=1.0,
                )
            ],
        )
    finally:
        store.close()
    return cfg


def test_build_summary_payload_all_teams(tmp_path):
    cfg = seeded_cfg(tmp_path)
    payload = api.build_summary_payload(cfg, window="week", team=None)
    assert payload["total"] == 2
    assert payload["team"] == "all"
    assert "breaches" not in payload  # breach objects aren't JSON-safe as-is
    # The mobile-team PR has tta_bh=6.0h, over the default 4h SLA -> 1 breach.
    assert payload["breach_count"] == 1


def test_build_summary_payload_scoped_to_one_team(tmp_path):
    cfg = seeded_cfg(tmp_path)
    payload = api.build_summary_payload(cfg, window="week", team="checkout-team")
    assert payload["total"] == 1
    assert payload["team"] == "checkout-team"


def test_build_teams_payload_lists_both_teams(tmp_path):
    cfg = seeded_cfg(tmp_path)
    payload = api.build_teams_payload(cfg, window="week")
    slugs = {t["team_slug"] for t in payload["teams"]}
    assert slugs == {"checkout-team", "mobile-team"}


def test_build_seniority_payload_reflects_reviewer_seniority_map(tmp_path):
    cfg = seeded_cfg(tmp_path)
    payload = api.build_seniority_payload(cfg, window="week")
    tiers = {s["tier"] for s in payload["seniority"]}
    assert "senior" in tiers


def test_window_rejects_unknown_name(tmp_path):
    cfg = seeded_cfg(tmp_path)
    try:
        api.build_summary_payload(cfg, window="year", team=None)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown window")
