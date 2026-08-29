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
        {"body": "This reverts commit aaaa1112222.\n"},
        {"body": "no marker here"},
        {"body": "This reverts commit bbbb2223333.\n"},
    ]
    shas = plr.defect_escape_shas(revert_items)
    assert shas == {"aaaa111", "bbbb222"}


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
    plr.apply_defect_flags([matched, unmatched], {"deadbee"})
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
    plr.apply_defect_flags([record], {"deadbee"})
    assert record.defect_flag == 0


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
