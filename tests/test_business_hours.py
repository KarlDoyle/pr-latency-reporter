"""Business-hours elapsed calculation: within-day, weekends, holidays, spans."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pr_latency_reporter as plr

TZ = ZoneInfo("Europe/London")


def dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def test_simple_within_one_business_day():
    # 10:00 -> 14:30 on a Wednesday = 4.5h.
    out = plr.business_hours_between(dt(2026, 1, 7, 10), dt(2026, 1, 7, 14, 30), TZ)
    assert out == 4.5


def test_clamped_to_business_window():
    # 07:00 -> 20:00 clamps to 09:00-17:00 = 8h.
    out = plr.business_hours_between(dt(2026, 1, 7, 7), dt(2026, 1, 7, 20), TZ)
    assert out == 8.0


def test_end_before_start_is_zero():
    assert plr.business_hours_between(dt(2026, 1, 7, 14), dt(2026, 1, 7, 10), TZ) == 0.0


def test_weekend_excluded():
    # Friday 16:00 -> Monday 10:00: Fri 16-17 (1h) + Mon 9-10 (1h) = 2h.
    out = plr.business_hours_between(dt(2026, 1, 9, 16), dt(2026, 1, 12, 10), TZ)
    assert out == 2.0


def test_holiday_excluded():
    # Thu 2026-01-01 is a holiday. Wed 16:00 -> Fri 10:00 should skip Thursday:
    # Wed 16-17 (1h) + Fri 9-10 (1h) = 2h (Thu fully excluded).
    holidays = {date(2026, 1, 1)}
    out = plr.business_hours_between(
        dt(2025, 12, 31, 16), dt(2026, 1, 2, 10), TZ, holidays=holidays
    )
    assert out == 2.0


def test_multi_day_full_span():
    # Mon 09:00 -> Wed 17:00 = three full 8h days = 24h.
    out = plr.business_hours_between(dt(2026, 1, 5, 9), dt(2026, 1, 7, 17), TZ)
    assert out == 24.0


def test_overnight_gap_not_counted():
    # Mon 16:00 -> Tue 10:00 = Mon 16-17 (1h) + Tue 9-10 (1h) = 2h.
    out = plr.business_hours_between(dt(2026, 1, 5, 16), dt(2026, 1, 6, 10), TZ)
    assert out == 2.0


def test_custom_hours():
    out = plr.business_hours_between(
        dt(2026, 1, 7, 8), dt(2026, 1, 7, 12), TZ, start_hour=8, end_hour=18
    )
    assert out == 4.0


def test_requires_aware_datetimes():
    naive = datetime(2026, 1, 7, 10)
    try:
        plr.business_hours_between(naive, dt(2026, 1, 7, 12), TZ)
    except ValueError:
        return
    raise AssertionError("expected ValueError for naive datetime")


def test_input_in_utc_is_converted():
    # 09:00 UTC == 09:00 London in January (no DST). 09:00->17:00 UTC = 8h.
    start = datetime(2026, 1, 7, 9, tzinfo=timezone.utc)
    end = datetime(2026, 1, 7, 17, tzinfo=timezone.utc)
    assert plr.business_hours_between(start, end, TZ) == 8.0
