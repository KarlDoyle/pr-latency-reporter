"""Pure assembly of a PRRecord from raw GitHub payloads (no network)."""

import pr_latency_reporter as plr


def test_repo_full_name_from_url():
    url = "https://api.github.com/repos/acme-corp/checkout"
    assert plr.repo_full_name_from_url(url) == "acme-corp/checkout"


def test_latest_team_review_request(cfg):
    events = [
        {
            "event": "review_requested",
            "created_at": "2026-01-05T09:00:00Z",
            "requested_team": {"slug": "your-team"},
        },
        {
            "event": "review_requested",
            "created_at": "2026-01-05T11:00:00Z",
            "requested_team": {"slug": "your-team"},
        },
        {
            "event": "review_requested",
            "created_at": "2026-01-05T10:00:00Z",
            "requested_team": {"slug": "other-team"},
        },
    ]
    out = plr.latest_team_review_request(events, "your-team")
    assert plr.to_iso8601(out) == "2026-01-05T11:00:00Z"


def test_summarise_reviews():
    reviews = [
        {"state": "COMMENTED", "submitted_at": "2026-01-05T09:30:00Z"},
        {"state": "CHANGES_REQUESTED", "submitted_at": "2026-01-05T10:00:00Z"},
        {"state": "APPROVED", "submitted_at": "2026-01-05T11:00:00Z"},
    ]
    first_review, first_approval, changes = plr.summarise_reviews(reviews)
    assert plr.to_iso8601(first_review) == "2026-01-05T09:30:00Z"
    assert plr.to_iso8601(first_approval) == "2026-01-05T11:00:00Z"
    assert changes is True


def test_build_pr_record_computes_business_hours(cfg):
    item = {
        "number": 245,
        "title": "Fix checkout bug",
        "user": {"login": "alice"},
        "html_url": "https://github.com/acme-corp/checkout/pull/245",
        "created_at": "2026-01-05T08:00:00Z",
        "updated_at": "2026-01-05T12:00:00Z",
        "repository_url": "https://api.github.com/repos/acme-corp/checkout",
        "pull_request": {"merged_at": "2026-01-05T14:00:00Z"},
    }
    events = [
        {
            "event": "review_requested",
            "created_at": "2026-01-05T09:00:00Z",
            "requested_team": {"slug": "your-team"},
        }
    ]
    reviews = [
        {"state": "COMMENTED", "submitted_at": "2026-01-05T10:00:00Z"},
        {"state": "APPROVED", "submitted_at": "2026-01-05T11:30:00Z"},
    ]
    comments = [{"created_at": "2026-01-05T10:30:00Z"}]

    rec = plr.build_pr_record(item, events, reviews, comments, cfg)

    assert rec.repo == "acme-corp/checkout"
    assert rec.number == 245
    assert rec.author == "alice"
    # 09:00 -> 10:00 first response = 1.0 business hours
    assert rec.ttfr_bh == 1.0
    # 09:00 -> 11:30 approval = 2.5 business hours
    assert rec.tta_bh == 2.5
    assert rec.sla_response_met == 1  # 1.0 <= 2.0
    assert rec.sla_approval_met == 1  # 2.5 <= 4.0
    assert rec.merged_at == "2026-01-05T14:00:00Z"


def test_build_pr_record_falls_back_to_created_at(cfg):
    # No team review-request event -> anchor on created_at.
    item = {
        "number": 9,
        "title": "x",
        "user": {"login": "bob"},
        "html_url": "https://github.com/acme-corp/bff/pull/9",
        "created_at": "2026-01-05T09:00:00Z",
        "updated_at": "2026-01-05T12:00:00Z",
        "repository_url": "https://api.github.com/repos/acme-corp/bff",
    }
    reviews = [{"state": "APPROVED", "submitted_at": "2026-01-05T13:00:00Z"}]
    rec = plr.build_pr_record(item, [], reviews, [], cfg)
    assert rec.review_requested_at is None
    assert rec.tta_bh == 4.0  # 09:00 -> 13:00
