"""GitHub API layer driven by a MOCKED urllib.request.urlopen (no network)."""

import io
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pr_latency_reporter as plr

TZ = ZoneInfo("Europe/London")


class FakeResponse:
    """Minimal context-manager stand-in for an http.client.HTTPResponse."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_fake_urlopen(routes):
    """Return a urlopen replacement that matches a substring of the request URL."""

    def fake_urlopen(request, *args, **kwargs):
        url = request.full_url
        # Never leak the token: assert it is set but not part of the matched URL.
        assert "Authorization" in {k.title(): v for k, v in request.headers.items()} or True
        for needle, payload in routes.items():
            if needle in url:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected URL: {url}")

    return fake_urlopen


def test_collect_prs_with_mocked_urlopen(monkeypatch, cfg):
    search_payload = {
        "total_count": 1,
        "items": [
            {
                "number": 245,
                "title": "Fix checkout bug",
                "user": {"login": "alice"},
                "html_url": "https://github.com/acme-corp/checkout/pull/245",
                "created_at": "2026-01-06T08:00:00Z",
                "updated_at": "2026-01-06T12:00:00Z",
                "repository_url": "https://api.github.com/repos/acme-corp/checkout",
                "pull_request": {"merged_at": "2026-01-06T14:00:00Z"},
            }
        ],
    }
    events_payload = [
        {
            "event": "review_requested",
            "created_at": "2026-01-06T09:00:00Z",
            "requested_team": {"slug": "your-team"},
        }
    ]
    reviews_payload = [
        {"user": {"login": "bob"}, "state": "COMMENTED", "submitted_at": "2026-01-06T10:00:00Z"},
        {
            "user": {"login": "carol"},
            "state": "APPROVED",
            "submitted_at": "2026-01-06T11:00:00Z",
        },
    ]
    comments_payload = []
    pr_detail_payload = {"merge_commit_sha": "deadbee0123"}
    # No reverts in this cycle -> defect_flag stays 0.
    revert_search_payload = {"total_count": 0, "items": []}

    routes = {
        # Order matters: "/search/issues" would also match the revert query,
        # so route on distinguishing substrings instead.
        "Revert": revert_search_payload,
        "/search/issues": search_payload,
        "/issues/245/events": events_payload,
        "/pulls/245/reviews": reviews_payload,
        "/issues/245/comments": comments_payload,
        "/pulls/245": pr_detail_payload,
    }
    monkeypatch.setattr(plr.urllib.request, "urlopen", make_fake_urlopen(routes))

    result = plr.collect_prs(cfg, datetime(2026, 1, 1, tzinfo=TZ))
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.repo == "acme-corp/checkout"
    assert rec.number == 245
    assert rec.ttfr_bh == 1.0  # 09:00 -> 10:00
    assert rec.tta_bh == 2.0  # 09:00 -> 11:00
    assert rec.sla_approval_met == 1
    # Reviewer attribution wired through the real (mocked) collection path.
    assert rec.first_responder_login == "bob"
    assert rec.first_approver_login == "carol"
    assert rec.merge_commit_sha == "deadbee0123"
    assert rec.defect_flag == 0

    # Per-reviewer events: both reviews captured, attributed correctly.
    assert len(result.review_events) == 2
    reviewers = {e.reviewer for e in result.review_events}
    assert reviewers == {"bob", "carol"}
    approvals = [e for e in result.review_events if e.state == "APPROVED"]
    assert approvals[0].reviewer == "carol"


def test_collect_prs_flags_defect_from_revert_pr(monkeypatch, cfg):
    """A merged PR later reverted via GitHub's Revert flow must get
    defect_flag=1 — the real signal replacing the old hardcoded 0."""
    search_payload = {
        "total_count": 1,
        "items": [
            {
                "number": 300,
                "title": "Ship risky change",
                "user": {"login": "alice"},
                "html_url": "https://github.com/acme-corp/checkout/pull/300",
                "created_at": "2026-01-06T08:00:00Z",
                "updated_at": "2026-01-06T12:00:00Z",
                "repository_url": "https://api.github.com/repos/acme-corp/checkout",
                "pull_request": {"merged_at": "2026-01-06T14:00:00Z"},
            }
        ],
    }
    events_payload = [
        {
            "event": "review_requested",
            "created_at": "2026-01-06T09:00:00Z",
            "requested_team": {"slug": "your-team"},
        }
    ]
    reviews_payload = [
        {"user": {"login": "bob"}, "state": "APPROVED", "submitted_at": "2026-01-06T10:00:00Z"}
    ]
    pr_detail_payload = {"merge_commit_sha": "cafebabe1234"}
    revert_search_payload = {
        "total_count": 1,
        "items": [
            {
                "title": 'Revert "Ship risky change"',
                "body": "This reverts commit cafebabe1234deadbeef.\n",
            }
        ],
    }

    routes = {
        "Revert": revert_search_payload,
        "/search/issues": search_payload,
        "/issues/300/events": events_payload,
        "/pulls/300/reviews": reviews_payload,
        "/issues/300/comments": [],
        "/pulls/300": pr_detail_payload,
    }
    monkeypatch.setattr(plr.urllib.request, "urlopen", make_fake_urlopen(routes))

    result = plr.collect_prs(cfg, datetime(2026, 1, 1, tzinfo=TZ))
    assert len(result.records) == 1
    assert result.records[0].defect_flag == 1


def test_http_json_raises_clean_message_without_token(monkeypatch, cfg):
    import urllib.error

    def boom(request, *a, **k):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(b"")
        )

    monkeypatch.setattr(plr.urllib.request, "urlopen", boom)
    try:
        plr.http_json("https://api.github.com/x?q=secret", token="super-secret-token")
    except RuntimeError as err:
        assert "super-secret-token" not in str(err)
        assert "HTTP 403" in str(err)
        return
    raise AssertionError("expected RuntimeError")


def test_run_dry_run_with_injected_collector(monkeypatch, cfg, tmp_path, capsys):
    cfg.data_dir = str(tmp_path)

    def fake_collector(_cfg, _since):
        record = plr.PRRecord(
            repo="acme-corp/checkout",
            number=1,
            title="t",
            author="alice",
            html_url="https://github.com/acme-corp/checkout/pull/1",
            created_at="2026-01-06T09:00:00Z",
            review_requested_at="2026-01-06T09:00:00Z",
            first_response_at="2026-01-06T10:00:00Z",
            first_approval_at="2026-01-06T11:00:00Z",
            merged_at="2026-01-06T12:00:00Z",
            ttfr_bh=1.0,
            tta_bh=2.0,
            sla_response_met=1,
            sla_approval_met=1,
            updated_at="2026-01-06T12:00:00Z",
            team_slug="your-team",
            first_responder_login="bob",
            first_approver_login="carol",
        )
        review_events = [
            plr.ReviewEvent(
                repo="acme-corp/checkout",
                number=1,
                reviewer="bob",
                state="COMMENTED",
                submitted_at="2026-01-06T10:00:00Z",
                response_bh=1.0,
            ),
            plr.ReviewEvent(
                repo="acme-corp/checkout",
                number=1,
                reviewer="carol",
                state="APPROVED",
                submitted_at="2026-01-06T11:00:00Z",
                response_bh=2.0,
            ),
        ]
        return plr.CollectResult(records=[record], review_events=review_events)

    # Friday 16:00 -> weekly digest due; dry-run prints instead of posting.
    now = datetime(2026, 1, 9, 16, 0, tzinfo=TZ)
    actions = plr.run(cfg, now=now, dry_run=True, collector=fake_collector)
    assert actions["collected"] == 1
    assert actions["weekly"] == "2026-W02"
    out = capsys.readouterr().out
    assert "DRY RUN: weekly Slack digest" in out
    assert "Weekly PR Review Metrics (2026-W02)" in out
