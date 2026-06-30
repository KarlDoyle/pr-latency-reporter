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
    assert "SLA Breaches: 1" in text
    assert "acme-corp/checkout #245" in text
    assert "(12.0h)" in text


def test_monthly_summary_render(cfg):
    text = plr.render_monthly_summary(canned_aggregate(), "2026-01", cfg)
    assert text.startswith("# Monthly Review Metrics - 2026-01")
    assert "Median Time to Approval (TTA): 3.7h" in text
    assert "SLA Compliance (<=4h): 82%" in text
    assert "P95 TTA: 7.5h" in text
    assert "Defect Escape Rate: 0.6%" in text


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
