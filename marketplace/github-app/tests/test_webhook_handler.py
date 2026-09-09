"""Pure-logic tests for the GitHub App webhook handler (no network, no live App)."""

import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import webhook_handler as wh  # noqa: E402

import pr_latency_reporter as plr  # noqa: E402


def test_verify_signature_accepts_correct_hmac():
    secret = "shh"
    body = b'{"hello":"world"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"
    assert wh.verify_signature(secret, body, header) is True


def test_verify_signature_rejects_wrong_hmac():
    assert wh.verify_signature("shh", b"body", "sha256=deadbeef") is False


def test_verify_signature_rejects_missing_header():
    assert wh.verify_signature("shh", b"body", None) is False
    assert wh.verify_signature("", b"body", "sha256=x") is False


def test_repo_and_number_from_pull_request_event():
    body = {
        "pull_request": {"number": 42},
        "repository": {"full_name": "acme-corp/checkout"},
    }
    assert wh.repo_and_number_from_event("pull_request", body) == ("acme-corp/checkout", 42)


def test_repo_and_number_from_issue_comment_on_pr():
    body = {
        "issue": {"number": 42, "pull_request": {"url": "..."}},
        "repository": {"full_name": "acme-corp/checkout"},
    }
    assert wh.repo_and_number_from_event("issue_comment", body) == ("acme-corp/checkout", 42)


def test_repo_and_number_from_issue_comment_on_plain_issue_is_none():
    body = {
        "issue": {"number": 42},  # no "pull_request" key -> not a PR comment
        "repository": {"full_name": "acme-corp/checkout"},
    }
    assert wh.repo_and_number_from_event("issue_comment", body) is None


def test_handle_event_ignores_irrelevant_event_types(cfg=None):
    assert wh.handle_event(None, "push", {}) is None


def test_handle_event_stores_pr_via_shared_builder(monkeypatch, tmp_path):
    cfg = plr.Config(
        gh_token="x",
        gh_org="acme-corp",
        team_slug="your-team",
        data_dir=str(tmp_path),
    )
    body = {
        "pull_request": {
            "number": 7,
            "title": "Fix thing",
            "user": {"login": "alice"},
            "html_url": "https://github.com/acme-corp/checkout/pull/7",
            "created_at": "2026-01-05T09:00:00Z",
            "updated_at": "2026-01-05T12:00:00Z",
            "merged_at": None,
        },
        "repository": {"full_name": "acme-corp/checkout"},
    }

    def fake_fetch_pr_detail(_cfg, repo, number):
        assert repo == "acme-corp/checkout"
        assert number == 7
        events = [
            {
                "event": "review_requested",
                "created_at": "2026-01-05T09:00:00Z",
                "requested_team": {"slug": "your-team"},
            }
        ]
        reviews = [
            {"user": {"login": "bob"}, "state": "APPROVED", "submitted_at": "2026-01-05T10:00:00Z"}
        ]
        return events, reviews, [], {"merge_commit_sha": "abc1234"}

    monkeypatch.setattr(plr, "fetch_pr_detail", fake_fetch_pr_detail)

    updated = wh.handle_event(cfg, "pull_request", body)
    assert updated == "acme-corp/checkout#7"

    store = plr.Store(cfg.db_path)
    try:
        records = store.fetch_all()
        assert len(records) == 1
        assert records[0].first_approver_login == "bob"
        assert records[0].merge_commit_sha == "abc1234"
        reviews = store.fetch_reviews_for(records)
        assert len(reviews) == 1
        assert reviews[0].reviewer == "bob"
    finally:
        store.close()
