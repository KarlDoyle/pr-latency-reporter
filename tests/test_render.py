"""Digest/summary rendering and scheduling from canned data."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pr_latency_reporter as plr

TZ = ZoneInfo("Europe/London")


def canned_aggregate():
    breach = plr.PRRecord(
        repo="acme-corp/checkout",
        number=245,
        title="Fix checkout bug",
        author="alice",
        html_url="https://github.com/acme-corp/checkout/pull/245",
        created_at=None,
        review_requested_at=None,
        first_response_at=None,
        first_approval_at=None,
        merged_at=None,
        tta_bh=12.0,
    )
    return plr.Aggregate(
        total=20,
        median_ttfr=1.9,
        median_tta=3.7,
        sla_response_pct=88.0,
        sla_approval_pct=82.0,
        p95_tta=7.5,
        rework_pct=25.0,
        defect_pct=0.6,
        breaches=[breach],
    )


def test_weekly_digest_render(cfg):
    text = plr.render_weekly_digest(canned_aggregate(), "2026-W03", cfg)
    assert "Weekly PR Review Metrics (2026-W03)" in text
    assert "Median Time to First Response: 1.9h" in text
    assert "Median Time to Approval: 3.7h" in text
    assert "SLA Compliance (<=4h approval): 82%" in text
    assert "P95 Latency: 7.5h" in text
    assert "Latency Outliers (>4h approval): 1" in text
    assert "acme-corp/checkout #245" in text
    assert "(12.0h)" in text


def test_weekly_digest_render_multi_team(cfg):
    checkout_agg = canned_aggregate()
    mobile_agg = plr.Aggregate(
        total=5,
        median_ttfr=0.9,
        median_tta=1.7,
        sla_response_pct=100.0,
        sla_approval_pct=100.0,
        p95_tta=2.5,
        rework_pct=0.0,
        defect_pct=0.0,
        breaches=[],
    )
    team_aggs = [
        plr.TeamAggregate(team_slug="checkout-team", agg=checkout_agg),
        plr.TeamAggregate(team_slug="mobile-team", agg=mobile_agg),
    ]
    text = plr.render_weekly_digest(checkout_agg, "2026-W03", cfg, team_aggs=team_aggs)
    assert "2 teams" in text
    assert "By team (awareness, not a ranking)" in text
    assert "checkout-team" in text
    assert "mobile-team" in text
    # Multi-team framing, not a single-team performance verdict.
    assert "worst" not in text.lower()


def test_monthly_summary_render(cfg):
    text = plr.render_monthly_summary(canned_aggregate(), "2026-01", cfg)
    assert text.startswith("# Monthly Review Metrics - 2026-01")
    assert "Median Time to Approval (TTA): 3.7h" in text
    assert "SLA Compliance (<=4h): 82%" in text
    assert "P95 TTA: 7.5h" in text
    # Defect-escape is now a real (revert-based, lower-bound) signal, honestly
    # labelled as such rather than presented as a complete measure.
    assert "Defect Escape Rate (revert-based, lower bound): 0.6%" in text
    assert "not a single-team performance verdict" in text


def test_monthly_summary_render_seniority_section(cfg):
    seniority_stats = [
        plr.SeniorityStat(
            tier="senior",
            reviewer_count=2,
            review_count=8,
            median_response_bh=1.2,
            share_of_reviews_pct=80.0,
            share_of_first_approvals_pct=75.0,
        ),
        plr.SeniorityStat(
            tier="unspecified",
            reviewer_count=1,
            review_count=2,
            median_response_bh=3.0,
            share_of_reviews_pct=20.0,
            share_of_first_approvals_pct=25.0,
        ),
    ]
    text = plr.render_monthly_summary(
        canned_aggregate(), "2026-01", cfg, seniority_stats=seniority_stats
    )
    assert "Reviewer leadership by seniority" in text
    assert "senior: 2 reviewer(s), 8 reviews" in text
    assert "75% of first approvals" in text


def test_weekly_digest_due_friday_afternoon():
    fri_16 = datetime(2026, 1, 9, 16, 0, tzinfo=TZ)
    assert plr.weekly_digest_due(fri_16, already_sent=False) is True
    assert plr.weekly_digest_due(fri_16, already_sent=True) is False


def test_weekly_digest_not_due_friday_morning():
    fri_10 = datetime(2026, 1, 9, 10, 0, tzinfo=TZ)
    assert plr.weekly_digest_due(fri_10, already_sent=False) is False


def test_weekly_digest_weekend_catchup():
    sat = datetime(2026, 1, 10, 9, 0, tzinfo=TZ)
    assert plr.weekly_digest_due(sat, already_sent=False) is True


def test_iso_week_label():
    assert plr.iso_week_label(date(2026, 1, 9)) == "2026-W02"


def test_last_day_of_month():
    assert plr.last_day_of_month(date(2026, 2, 10)) == date(2026, 2, 28)
    assert plr.last_day_of_month(date(2026, 12, 1)) == date(2026, 12, 31)


def test_month_to_generate():
    # Last day of January -> generate January.
    assert plr.month_to_generate(datetime(2026, 1, 31, 18, tzinfo=TZ)) == "2026-01"
    # Mid-February -> previous month (January) due.
    assert plr.month_to_generate(datetime(2026, 2, 12, 9, tzinfo=TZ)) == "2026-01"


def test_month_window():
    start, end = plr.month_window("2026-01", TZ)
    assert start == datetime(2026, 1, 1, tzinfo=TZ)
    assert end == datetime(2026, 2, 1, tzinfo=TZ)
