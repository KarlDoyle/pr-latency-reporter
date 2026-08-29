"""Per-reviewer / per-seniority attribution and multi-team rollup.

Covers the two productization fixes with the most riding on them:
1. Reviewer attribution — `ReviewEvent`, `reviewer_aggregate`, `seniority_rollup`
   must correctly distinguish reviewers from a realistic mocked payload, not
   just report the PR author (the original bug).
2. Multi-team aggregation — `aggregate_by_team` groups by the team a PR was
   actually requested from, for director/TPM-level, multi-team observation.
"""

from datetime import date

import pytest

import pr_latency_reporter as plr


def make_pr(number, team_slug, first_approver, merged=True):
    return plr.PRRecord(
        repo="acme-corp/checkout",
        number=number,
        title=f"PR {number}",
        author="pr-author",
        html_url=f"https://github.com/acme-corp/checkout/pull/{number}",
        created_at="2026-01-05T09:00:00Z",
        review_requested_at="2026-01-05T09:00:00Z",
        first_response_at="2026-01-05T10:00:00Z",
        first_approval_at="2026-01-05T11:00:00Z",
        merged_at="2026-01-05T12:00:00Z" if merged else None,
        team_slug=team_slug,
        first_approver_login=first_approver,
        ttfr_bh=1.0,
        tta_bh=2.0,
        sla_response_met=1,
        sla_approval_met=1,
    )


def make_review(number, reviewer, state, response_bh):
    return plr.ReviewEvent(
        repo="acme-corp/checkout",
        number=number,
        reviewer=reviewer,
        state=state,
        submitted_at="2026-01-05T10:00:00Z",
        response_bh=response_bh,
    )


# ---------------------------------------------------------------------------
# build_review_events — the core reviewer-attribution fix
# ---------------------------------------------------------------------------


def test_build_review_events_attributes_each_reviewer(cfg):
    # Before the fix: only the PR author was ever stored, no reviewer login
    # at all. After: one ReviewEvent per review, each carrying the real
    # reviewer login pulled from review.user.login.
    anchor = plr.parse_iso8601("2026-01-05T09:00:00Z")
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-01-05T10:00:00Z"},
        {
            "user": {"login": "bob"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-01-05T09:30:00Z",
        },
        # A review with no login (e.g. a deleted/bot account edge case) must
        # not be silently attributed to anyone.
        {"user": None, "state": "COMMENTED", "submitted_at": "2026-01-05T09:15:00Z"},
    ]
    events = plr.build_review_events("acme-corp/checkout", 245, reviews, anchor, cfg)
    assert len(events) == 2  # the login-less review is dropped, not mis-attributed
    reviewers = {e.reviewer: e for e in events}
    assert reviewers["alice"].state == "APPROVED"
    assert reviewers["alice"].response_bh == 1.0  # 09:00 -> 10:00
    assert reviewers["bob"].state == "CHANGES_REQUESTED"
    assert reviewers["bob"].response_bh == 0.5  # 09:00 -> 09:30


# ---------------------------------------------------------------------------
# reviewer_aggregate / seniority_rollup
# ---------------------------------------------------------------------------


def test_reviewer_aggregate_distinguishes_reviewers():
    cfg = plr.Config(
        gh_org="acme-corp",
        team_slug="your-team",
        holidays=frozenset({date(2026, 1, 1)}),
        reviewer_seniority={"alice": "senior", "bob": "mid"},
    )
    events = [
        make_review(1, "alice", "APPROVED", 1.0),
        make_review(2, "alice", "APPROVED", 2.0),
        make_review(3, "bob", "CHANGES_REQUESTED", 4.0),
    ]
    stats = plr.reviewer_aggregate(events, cfg)
    by_login = {s.reviewer: s for s in stats}
    assert by_login["alice"].review_count == 2
    assert by_login["alice"].approval_count == 2
    assert by_login["alice"].seniority == "senior"
    assert by_login["alice"].median_response_bh == 1.5
    assert by_login["bob"].review_count == 1
    assert by_login["bob"].approval_count == 0
    assert by_login["bob"].seniority == "mid"


def test_reviewer_aggregate_unmapped_login_is_unspecified(cfg):
    events = [make_review(1, "zoe", "APPROVED", 1.0)]
    stats = plr.reviewer_aggregate(events, cfg)
    assert stats[0].seniority == "unspecified"


def test_seniority_rollup_measures_senior_leadership():
    """This is the number behind "are seniors leading on review": share of
    review volume and first-approvals coming from the senior tier, plus
    their response speed — computed from real attribution, not asserted."""
    cfg = plr.Config(
        gh_org="acme-corp",
        team_slug="your-team",
        holidays=frozenset(),
        reviewer_seniority={"alice": "senior", "bob": "senior", "carol": "mid"},
    )
    events = [
        make_review(1, "alice", "APPROVED", 1.0),
        make_review(2, "bob", "APPROVED", 3.0),
        make_review(3, "carol", "COMMENTED", 2.0),
        make_review(4, "carol", "APPROVED", 5.0),
    ]
    records = [
        make_pr(1, "checkout-team", "alice"),
        make_pr(2, "checkout-team", "bob"),
        make_pr(4, "checkout-team", "carol"),
    ]
    rollup = plr.seniority_rollup(events, records, cfg)
    by_tier = {s.tier: s for s in rollup}

    assert by_tier["senior"].reviewer_count == 2
    assert by_tier["senior"].review_count == 2
    assert by_tier["senior"].share_of_reviews_pct == 50.0  # 2 of 4 reviews
    # 2 of 3 first-approvals (alice, bob) came from seniors
    assert by_tier["senior"].share_of_first_approvals_pct == pytest.approx(66.6667, abs=0.01)
    assert by_tier["mid"].reviewer_count == 1
    assert by_tier["mid"].review_count == 2
    assert by_tier["mid"].share_of_first_approvals_pct == pytest.approx(33.3333, abs=0.01)


def test_seniority_rollup_empty_is_zero_not_error(cfg):
    rollup = plr.seniority_rollup([], [], cfg)
    assert rollup == []


# ---------------------------------------------------------------------------
# aggregate_by_team — multi-team rollup
# ---------------------------------------------------------------------------


def test_aggregate_by_team_groups_correctly(cfg):
    records = [
        make_pr(1, "checkout-team", "alice"),
        make_pr(2, "checkout-team", "bob"),
        make_pr(3, "mobile-team", "carol"),
    ]
    by_team = plr.aggregate_by_team(records, cfg)
    slugs = [t.team_slug for t in by_team]
    assert slugs == ["checkout-team", "mobile-team"]  # stable, non-ranked order
    checkout = next(t for t in by_team if t.team_slug == "checkout-team")
    assert checkout.agg.total == 2
    mobile = next(t for t in by_team if t.team_slug == "mobile-team")
    assert mobile.agg.total == 1


def test_aggregate_by_team_buckets_unmatched_team_as_unassigned(cfg):
    records = [make_pr(1, "", "alice")]  # no team ever matched (fallback anchor)
    by_team = plr.aggregate_by_team(records, cfg)
    assert by_team[0].team_slug == "unassigned"
    assert by_team[0].agg.total == 1  # nothing silently dropped


# ---------------------------------------------------------------------------
# Config parsing for multi-team / seniority
# ---------------------------------------------------------------------------


def test_parse_team_slugs():
    assert plr.parse_team_slugs("checkout-team, mobile-team,checkout-team") == (
        "checkout-team",
        "mobile-team",
    )
    assert plr.parse_team_slugs("") == ()


def test_parse_reviewer_seniority():
    out = plr.parse_reviewer_seniority("alice:senior, bob:mid,, carol:senior")
    assert out == {"alice": "senior", "bob": "mid", "carol": "senior"}


def test_config_team_slugs_falls_back_to_single_team_slug():
    cfg = plr.Config(gh_org="acme-corp", team_slug="your-team")
    assert cfg.teams == ("your-team",)


def test_config_team_slugs_wins_when_set():
    cfg = plr.Config(gh_org="acme-corp", team_slug="your-team", team_slugs=("a", "b"))
    assert cfg.teams == ("a", "b")


def test_config_missing_required_reports_team_slugs(cfg):
    cfg.team_slug = ""
    cfg.team_slugs = ()
    missing = cfg.missing_required(need_slack=False)
    assert "TEAM_SLUG or TEAM_SLUGS" in missing
