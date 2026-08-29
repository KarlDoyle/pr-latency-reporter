"""SQLite store round-trip: write events -> read back -> aggregate; idempotency."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pr_latency_reporter as plr

TZ = ZoneInfo("Europe/London")


def make_record(number, ttfr, tta, requested="2026-01-06T09:00:00Z"):
    return plr.PRRecord(
        repo="acme-corp/checkout",
        number=number,
        title=f"PR {number}",
        author="alice",
        html_url=f"https://github.com/acme-corp/checkout/pull/{number}",
        created_at=requested,
        review_requested_at=requested,
        first_response_at="2026-01-06T10:00:00Z",
        first_approval_at="2026-01-06T11:00:00Z",
        merged_at="2026-01-06T12:00:00Z",
        changes_requested=0,
        defect_flag=0,
        ttfr_bh=ttfr,
        tta_bh=tta,
        sla_response_met=int(ttfr <= 2.0),
        sla_approval_met=int(tta <= 4.0),
        updated_at="2026-01-06T12:00:00Z",
    )


def test_round_trip_and_aggregate(cfg):
    store = plr.Store(":memory:")
    store.upsert_pr(make_record(1, 1.0, 2.0), "2026-01-06T12:00:00Z")
    store.upsert_pr(make_record(2, 1.5, 3.0), "2026-01-06T12:00:00Z")

    records = store.fetch_all()
    assert len(records) == 2

    start = datetime(2026, 1, 5, tzinfo=TZ)
    end = datetime(2026, 1, 12, tzinfo=TZ)
    in_window = store.fetch_between(start, end)
    assert {r.number for r in in_window} == {1, 2}

    agg = plr.aggregate(in_window, cfg)
    assert agg.total == 2
    assert agg.median_tta == 2.5
    store.close()


def test_upsert_is_idempotent_on_pr_table(cfg):
    store = plr.Store(":memory:")
    store.upsert_pr(make_record(1, 1.0, 2.0), "2026-01-06T12:00:00Z")
    # Re-collect the same PR with an updated approval time.
    updated = make_record(1, 1.0, 3.5)
    store.upsert_pr(updated, "2026-01-06T13:00:00Z")

    records = store.fetch_all()
    assert len(records) == 1  # projection deduped by (repo, number)
    assert records[0].tta_bh == 3.5

    # Append-only event log keeps both observations.
    count = store.conn.execute("SELECT COUNT(*) FROM pr_events").fetchone()[0]
    assert count == 2
    store.close()


def test_cursor_persistence():
    store = plr.Store(":memory:")
    assert store.get_cursor() is None
    store.set_cursor("2026-01-06T12:00:00Z")
    assert store.get_cursor() == "2026-01-06T12:00:00Z"
    store.set_cursor("2026-01-07T12:00:00Z")
    assert store.get_cursor() == "2026-01-07T12:00:00Z"
    store.close()


def test_sent_log():
    store = plr.Store(":memory:")
    assert store.was_sent("weekly", "2026-W02") is False
    store.mark_sent("weekly", "2026-W02", "2026-01-09T16:00:00Z")
    assert store.was_sent("weekly", "2026-W02") is True
    store.close()


def test_replace_reviews_round_trip_and_idempotent():
    store = plr.Store(":memory:")
    events = [
        plr.ReviewEvent(
            repo="acme-corp/checkout",
            number=1,
            reviewer="alice",
            state="APPROVED",
            submitted_at="2026-01-06T11:00:00Z",
            response_bh=2.0,
        ),
        plr.ReviewEvent(
            repo="acme-corp/checkout",
            number=1,
            reviewer="bob",
            state="COMMENTED",
            submitted_at="2026-01-06T10:00:00Z",
            response_bh=1.0,
        ),
    ]
    store.replace_reviews("acme-corp/checkout", 1, events)
    fetched = store.fetch_reviews_for([make_record(1, 1.0, 2.0)])
    assert {e.reviewer for e in fetched} == {"alice", "bob"}

    # Re-collecting the same PR with a dismissed review must replace, not
    # accumulate — the stored set always reflects GitHub's current state.
    store.replace_reviews("acme-corp/checkout", 1, events[:1])
    fetched_again = store.fetch_reviews_for([make_record(1, 1.0, 2.0)])
    assert {e.reviewer for e in fetched_again} == {"alice"}
    store.close()


def test_fetch_reviews_for_scopes_to_requested_prs():
    store = plr.Store(":memory:")
    store.replace_reviews(
        "acme-corp/checkout",
        1,
        [
            plr.ReviewEvent(
                repo="acme-corp/checkout",
                number=1,
                reviewer="alice",
                state="APPROVED",
                submitted_at="2026-01-06T11:00:00Z",
                response_bh=2.0,
            )
        ],
    )
    store.replace_reviews(
        "acme-corp/checkout",
        2,
        [
            plr.ReviewEvent(
                repo="acme-corp/checkout",
                number=2,
                reviewer="bob",
                state="APPROVED",
                submitted_at="2026-01-06T11:00:00Z",
                response_bh=2.0,
            )
        ],
    )
    fetched = store.fetch_reviews_for([make_record(1, 1.0, 2.0)])
    assert {e.reviewer for e in fetched} == {"alice"}
    store.close()


def test_upsert_pr_persists_reviewer_attribution_and_team():
    store = plr.Store(":memory:")
    record = make_record(1, 1.0, 2.0)
    record.team_slug = "checkout-team"
    record.first_responder_login = "bob"
    record.first_approver_login = "alice"
    record.merge_commit_sha = "deadbeef"
    store.upsert_pr(record, "2026-01-06T12:00:00Z")

    fetched = store.fetch_all()[0]
    assert fetched.team_slug == "checkout-team"
    assert fetched.first_responder_login == "bob"
    assert fetched.first_approver_login == "alice"
    assert fetched.merge_commit_sha == "deadbeef"
    store.close()


def test_fetch_between_excludes_out_of_window():
    store = plr.Store(":memory:")
    store.upsert_pr(make_record(1, 1.0, 2.0, requested="2025-12-01T09:00:00Z"), "now")
    store.upsert_pr(make_record(2, 1.0, 2.0, requested="2026-01-06T09:00:00Z"), "now")
    start = datetime(2026, 1, 5, tzinfo=TZ)
    end = datetime(2026, 1, 12, tzinfo=TZ)
    got = store.fetch_between(start, end)
    assert {r.number for r in got} == {2}
    store.close()
