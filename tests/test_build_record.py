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
    # Reviewer attribution fix: each review carries a distinct reviewer login,
    # and summarise_reviews must return the correct login for the first
    # response AND (separately) for the first approval, not just the login of
    # whichever review happens to be first in the list.
    reviews = [
        {
            "user": {"login": "bob"},
            "state": "COMMENTED",
            "submitted_at": "2026-01-05T09:30:00Z",
        },
        {
            "user": {"login": "carol"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-01-05T10:00:00Z",
        },
        {
            "user": {"login": "alice"},
            "state": "APPROVED",
            "submitted_at": "2026-01-05T11:00:00Z",
        },
    ]
    first_review, first_review_login, first_approval, first_approval_login, changes = (
        plr.summarise_reviews(reviews)
    )
    assert plr.to_iso8601(first_review) == "2026-01-05T09:30:00Z"
    assert first_review_login == "bob"
    assert plr.to_iso8601(first_approval) == "2026-01-05T11:00:00Z"
    assert first_approval_login == "alice"
    assert changes is True


# ---------------------------------------------------------------------------
# Bug 3 regression — self-review not excluded.
#
# Before the fix, summarise_reviews() had no author-exclusion check: a
# self-approval by the PR's own author became first_response_at/
# first_approval_at, artificially driving TTFR/TTA toward zero and inflating
# SLA%, and would pollute any seniority-tiered "first approvals" rollup.
# ---------------------------------------------------------------------------


def test_summarise_reviews_excludes_self_review():
    reviews = [
        # The PR author "alice" self-approves immediately...
        {
            "user": {"login": "alice"},
            "state": "APPROVED",
            "submitted_at": "2026-01-05T09:01:00Z",
        },
        # ...but the real first external response/approval is bob, later.
        {
            "user": {"login": "bob"},
            "state": "APPROVED",
            "submitted_at": "2026-01-05T11:00:00Z",
        },
    ]
    first_review, first_review_login, first_approval, first_approval_login, _ = (
        plr.summarise_reviews(reviews, author="alice")
    )
    assert first_review_login == "bob"
    assert first_approval_login == "bob"
    assert plr.to_iso8601(first_approval) == "2026-01-05T11:00:00Z"


def test_summarise_reviews_all_self_reviews_yields_no_response():
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-01-05T09:00:00Z"},
    ]
    first_review, first_review_login, first_approval, first_approval_login, _ = (
        plr.summarise_reviews(reviews, author="alice")
    )
    assert first_review is None
    assert first_approval is None
    assert first_review_login == ""
    assert first_approval_login == ""


def test_summarise_reviews_no_author_filters_nothing():
    # Back-compat: omitting `author` must not exclude anything (existing
    # callers/tests that don't pass it keep working unchanged).
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-01-05T09:00:00Z"},
    ]
    _, _, _, first_approval_login, _ = plr.summarise_reviews(reviews)
    assert first_approval_login == "alice"


def test_build_pr_record_excludes_self_approval():
    """End-to-end: an author's self-approval must not become
    first_approval_at/first_approver_login, and must not artificially
    satisfy the approval SLA."""
    item = {
        "number": 50,
        "title": "Self-approved PR",
        "user": {"login": "alice"},
        "html_url": "https://github.com/acme-corp/checkout/pull/50",
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
        # Author self-approves instantly at the anchor - would otherwise
        # yield tta_bh == 0.0 and a trivially "met" SLA.
        {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-01-05T09:00:01Z"},
        # Real external approval, well outside the SLA window.
        {
            "user": {"login": "carol"},
            "state": "APPROVED",
            "submitted_at": "2026-01-05T13:00:00Z",
        },
    ]
    rec = plr.build_pr_record(item, events, reviews, [], cfg=plr.Config(gh_org="acme-corp"))
    assert rec.first_approver_login == "carol"
    # 09:00 -> 13:00 (carol's real approval), not the self-approval's ~0h,
    # which would have wrongly and trivially satisfied the SLA.
    assert rec.tta_bh == 4.0
    assert rec.first_approver_login != "alice"


def test_summarise_reviews_distinguishes_reviewers_out_of_order():
    # Approval arrives from a different, earlier-listed reviewer than the
    # first response — must not conflate the two logins.
    reviews = [
        {"user": {"login": "dave"}, "state": "APPROVED", "submitted_at": "2026-01-05T09:00:00Z"},
        {
            "user": {"login": "erin"},
            "state": "COMMENTED",
            "submitted_at": "2026-01-05T08:00:00Z",
        },
    ]
    _, first_review_login, _, first_approval_login, _ = plr.summarise_reviews(reviews)
    assert first_review_login == "erin"
    assert first_approval_login == "dave"


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
        {"user": {"login": "bob"}, "state": "COMMENTED", "submitted_at": "2026-01-05T10:00:00Z"},
        {
            "user": {"login": "carol"},
            "state": "APPROVED",
            "submitted_at": "2026-01-05T11:30:00Z",
        },
    ]
    comments = [{"created_at": "2026-01-05T10:30:00Z"}]

    rec = plr.build_pr_record(item, events, reviews, comments, cfg, merge_commit_sha="abc1234")

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
    # Reviewer attribution: the PR *author* (alice) must never be confused
    # with who actually reviewed it (bob responded first, carol approved).
    assert rec.first_responder_login == "bob"
    assert rec.first_approver_login == "carol"
    assert rec.author != rec.first_responder_login
    assert rec.team_slug == "your-team"
    assert rec.merge_commit_sha == "abc1234"


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
