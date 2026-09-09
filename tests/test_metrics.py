"""Metric primitives and aggregation: TTFR/TTA, SLA%, P95, rework, defects."""

import pytest

import pr_latency_reporter as plr


def make_record(ttfr, tta, *, changes=0, defect=0, merged=True, cfg=None):
    sla_r = int(ttfr is not None and ttfr <= (cfg.sla_response_hrs if cfg else 2.0))
    sla_a = int(tta is not None and tta <= (cfg.sla_approval_hrs if cfg else 4.0))
    return plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/pr/1",
        created_at="2026-01-05T09:00:00Z",
        review_requested_at="2026-01-05T09:00:00Z",
        first_response_at="2026-01-05T10:00:00Z",
        first_approval_at="2026-01-05T11:00:00Z",
        merged_at="2026-01-05T12:00:00Z" if merged else None,
        changes_requested=changes,
        defect_flag=defect,
        ttfr_bh=ttfr,
        tta_bh=tta,
        sla_response_met=sla_r,
        sla_approval_met=sla_a,
    )


def test_percentile_interpolation():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert plr.percentile(values, 95) == pytest.approx(9.55)


def test_percentile_edges():
    assert plr.percentile([], 95) == 0.0
    assert plr.percentile([4.2], 95) == 4.2
    assert plr.percentile([0, 10], 100) == 10.0
    assert plr.percentile([0, 10], 0) == 0.0


def test_median_or_zero():
    assert plr.median_or_zero([]) == 0.0
    assert plr.median_or_zero([1, 3]) == 2.0


def test_sla_compliance_pct():
    assert plr.sla_compliance_pct([]) == 0.0
    assert plr.sla_compliance_pct([True, True, False, False]) == 50.0
    assert plr.sla_compliance_pct([True, True, True, True]) == 100.0


def test_rework_and_defect_rates():
    assert plr.rework_rate_pct([True, False, False, False]) == 25.0
    assert plr.defect_escape_pct([False, False, False, True]) == 25.0
    assert plr.rework_rate_pct([]) == 0.0


# ---------------------------------------------------------------------------
# Defect-escape signal: fixes the hardcoded-0 fake metric. Real, revert-based,
# an explicit lower bound — see defect_escape_pct's docstring.
# ---------------------------------------------------------------------------


def test_extract_reverted_sha_from_github_revert_body():
    body = "This reverts commit abc1234deadbeef1234567890abcdef12345678.\n"
    assert plr.extract_reverted_sha(body) == "abc1234"


def test_extract_reverted_sha_no_match():
    assert plr.extract_reverted_sha("Just a regular PR description.") is None
    assert plr.extract_reverted_sha(None) is None
    assert plr.extract_reverted_sha("") is None


def test_defect_escape_shas_collects_all_matches():
    revert_items = [
        {
            "repository_url": "https://api.github.com/repos/acme-corp/checkout",
            "body": "This reverts commit aaaa1112222.\n",
            "created_at": "2026-01-06T09:00:00Z",
        },
        {
            "repository_url": "https://api.github.com/repos/acme-corp/checkout",
            "body": "no marker here",
            "created_at": "2026-01-06T09:00:00Z",
        },
        {
            "repository_url": "https://api.github.com/repos/acme-corp/mobile",
            "body": "This reverts commit bbbb2223333.\n",
            "created_at": "2026-01-06T09:00:00Z",
        },
    ]
    shas = plr.defect_escape_shas(revert_items)
    assert set(shas["acme-corp/checkout"]) == {"aaaa111"}
    assert set(shas["acme-corp/mobile"]) == {"bbbb222"}


def test_apply_defect_flags_sets_flag_on_matching_merge_commit_sha(cfg):
    matched = plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/1",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="deadbeef123456",
    )
    unmatched = plr.PRRecord(
        repo="acme-corp/checkout",
        number=2,
        title="t2",
        author="a",
        html_url="https://example.test/2",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="ffffffff9999",
    )
    reverted_by_repo = {
        "acme-corp/checkout": {"deadbee": plr.parse_iso8601("2026-01-06T12:00:00Z")},
    }
    plr.apply_defect_flags([matched, unmatched], reverted_by_repo, 14)
    assert matched.defect_flag == 1
    assert unmatched.defect_flag == 0


def test_apply_defect_flags_no_op_when_no_sha():
    record = plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/1",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="",
    )
    reverted_by_repo = {
        "acme-corp/checkout": {"deadbee": plr.parse_iso8601("2026-01-06T12:00:00Z")},
    }
    plr.apply_defect_flags([record], reverted_by_repo, 14)
    assert record.defect_flag == 0


# ---------------------------------------------------------------------------
# Bug 1 regression — cross-repo defect false-positive.
#
# Before the fix, defect_escape_shas() returned one org-wide set of reverted
# short SHAs and apply_defect_flags() matched every record against it with
# no repo scoping at all. Two PRs in unrelated repos that happen to share a
# 7-char merge-commit-SHA prefix would collide, and the WRONG repo's PR got
# defect_flag=1 for a revert that has nothing to do with it.
# ---------------------------------------------------------------------------


def test_apply_defect_flags_does_not_cross_repo_collide():
    # Same short SHA prefix, two different repos. Only "mobile" was actually
    # reverted (per reverted_by_repo); "checkout" sharing the prefix must NOT
    # be flagged.
    checkout_pr = plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/1",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="cafebabe000000",
    )
    mobile_pr = plr.PRRecord(
        repo="acme-corp/mobile",
        number=2,
        title="t2",
        author="a",
        html_url="https://example.test/2",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="cafebabe999999",  # same 7-char prefix as checkout_pr
    )
    reverted_by_repo = {
        "acme-corp/mobile": {"cafebab": plr.parse_iso8601("2026-01-06T09:00:00Z")},
    }
    plr.apply_defect_flags([checkout_pr, mobile_pr], reverted_by_repo, 14)
    assert mobile_pr.defect_flag == 1  # the actual revert
    assert checkout_pr.defect_flag == 0  # must NOT collide across repos


def test_defect_escape_shas_scopes_by_repo_end_to_end():
    """End-to-end: two revert PRs in different repos referencing SHAs that
    share a 7-char prefix must never cross-match the other repo's record."""
    revert_items = [
        {
            "repository_url": "https://api.github.com/repos/acme-corp/checkout",
            "body": "This reverts commit cafebabe000000deadbeef.\n",
            "created_at": "2026-01-06T09:00:00Z",
        },
    ]
    shas_by_repo = plr.defect_escape_shas(revert_items)
    other_repo_record = plr.PRRecord(
        repo="acme-corp/mobile",
        number=9,
        title="t",
        author="a",
        html_url="https://example.test/9",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-05T12:00:00Z",
        merge_commit_sha="cafebabe999999",  # same 7-char prefix, different repo
    )
    plr.apply_defect_flags([other_repo_record], shas_by_repo, 14)
    assert other_repo_record.defect_flag == 0


# ---------------------------------------------------------------------------
# Bug 2 regression — defect_lookback_days was dead code.
#
# DEFECT_LOOKBACK_DAYS / Config.defect_lookback_days was defined and
# documented in defect_escape_pct's docstring ("within defect_lookback_days
# of the original merge") but never actually consulted anywhere in the
# matching path — a revert matched any amount of time later still set
# defect_flag=1. Now apply_defect_flags() bounds the match window for real.
# ---------------------------------------------------------------------------


def test_apply_defect_flags_respects_lookback_window():
    record = plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/1",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-01T12:00:00Z",
        merge_commit_sha="deadbeef123456",
    )
    # Revert landed 400 days after the original merge — well outside any
    # sane lookback window, so this must NOT count as the same defect
    # escaping.
    reverted_by_repo = {
        "acme-corp/checkout": {"deadbee": plr.parse_iso8601("2027-02-05T12:00:00Z")},
    }
    plr.apply_defect_flags([record], reverted_by_repo, 14)
    assert record.defect_flag == 0


def test_apply_defect_flags_within_lookback_window_still_flags():
    record = plr.PRRecord(
        repo="acme-corp/checkout",
        number=1,
        title="t",
        author="a",
        html_url="https://example.test/1",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at="2026-01-01T12:00:00Z",
        merge_commit_sha="deadbeef123456",
    )
    # Revert landed 5 days after the original merge — within the 14-day
    # lookback, so this must still count.
    reverted_by_repo = {
        "acme-corp/checkout": {"deadbee": plr.parse_iso8601("2026-01-06T12:00:00Z")},
    }
    plr.apply_defect_flags([record], reverted_by_repo, 14)
    assert record.defect_flag == 1


def test_aggregate_end_to_end(cfg):
    records = [
        make_record(1.0, 2.0, cfg=cfg),  # both within SLA
        make_record(1.5, 3.5, cfg=cfg),  # both within SLA
        make_record(3.0, 12.0, changes=1, defect=1, cfg=cfg),  # breach
        make_record(0.5, 5.0, cfg=cfg),  # approval breach
    ]
    agg = plr.aggregate(records, cfg)

    assert agg.total == 4
    assert agg.median_ttfr == 1.25  # median of [1.0, 1.5, 3.0, 0.5]
    assert agg.median_tta == 4.25  # median of [2.0, 3.5, 12.0, 5.0]
    # response met: 1.0,1.5,0.5 <=2 (3 of 4) -> 75%
    assert agg.sla_response_pct == 75.0
    # approval met: 2.0,3.5 <=4 (2 of 4) -> 50%
    assert agg.sla_approval_pct == 50.0
    assert agg.rework_pct == 25.0
    assert agg.defect_pct == 25.0  # 1 defect of 4 merged
    # two breaches (tta > 4), sorted slowest first
    assert [b.tta_bh for b in agg.breaches] == [12.0, 5.0]


def test_aggregate_ignores_missing_values(cfg):
    records = [make_record(None, None, merged=False, cfg=cfg)]
    agg = plr.aggregate(records, cfg)
    assert agg.total == 1
    assert agg.median_tta == 0.0
    assert agg.p95_tta == 0.0
    assert agg.breaches == []
