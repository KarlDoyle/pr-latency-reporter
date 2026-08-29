#!/usr/bin/env python3
"""GitHub App webhook receiver for pr-latency-reporter.

Stdlib-only (``http.server``, ``hmac``). Trades the core tool's polling loop
(``search_review_requested`` on a cron) for push-triggered collection: when
GitHub fires ``pull_request`` / ``pull_request_review`` / ``issue_comment``,
this re-collects and stores just that one PR using the SAME
``build_pr_record`` / ``build_review_events`` path the poller uses — one
source of truth for what a PRRecord looks like, regardless of how collection
was triggered.

This is the GitHub Marketplace wrapper's entrypoint — it is a separate
process from ``pr_latency_reporter.py``'s own CLI, run alongside (or instead
of) the cron job once a GitHub App is registered and installed:

    GH_WEBHOOK_SECRET=... GH_TOKEN=... GH_ORG=... TEAM_SLUGS=... \\
        python3 marketplace/github-app/webhook_handler.py

See ``marketplace/github-app/README.md`` for what this cannot do without a
live, registered GitHub App on Karl's own account (app creation, webhook
secret, installation, hosting).
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import pr_latency_reporter as plr  # noqa: E402

WEBHOOK_SECRET = os.getenv("GH_WEBHOOK_SECRET", "")
RELEVANT_EVENTS = {"pull_request", "pull_request_review", "issue_comment"}


def verify_signature(secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` header: HMAC-SHA256 over the
    raw request body, keyed by the webhook secret. Constant-time compare.
    """
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


def repo_and_number_from_event(event_type: str, body: dict) -> tuple[str, int] | None:
    """Extract ``(repo_full_name, pr_number)`` from a webhook payload, or
    ``None`` if the event isn't PR-shaped (e.g. a comment on a plain issue).
    """
    if event_type in ("pull_request", "pull_request_review"):
        pr = body.get("pull_request") or {}
        repo = (body.get("repository") or {}).get("full_name", "")
        number = pr.get("number")
        if repo and number:
            return repo, int(number)
        return None
    if event_type == "issue_comment":
        issue = body.get("issue") or {}
        if "pull_request" not in issue:  # comment on a plain issue, not a PR
            return None
        repo = (body.get("repository") or {}).get("full_name", "")
        number = issue.get("number")
        if repo and number:
            return repo, int(number)
        return None
    return None


def issue_shaped_item(repo: str, number: int, body: dict) -> dict:
    """Adapt a webhook's ``pull_request``/``issue.pull_request`` object into
    the Search-API issue-item shape ``build_pr_record`` expects, so both
    collection paths (poll and webhook) feed the exact same builder.
    """
    pr = body.get("pull_request") or (body.get("issue") or {}).get("pull_request") or {}
    return {
        "number": number,
        "title": pr.get("title", ""),
        "user": pr.get("user", {}),
        "html_url": pr.get("html_url", ""),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "repository_url": f"https://api.github.com/repos/{repo}",
        "pull_request": {"merged_at": pr.get("merged_at")},
    }


def handle_event(cfg: plr.Config, event_type: str, body: dict) -> str | None:
    """Re-collect and store the one PR this event refers to.

    Returns the ``"repo#number"`` that was updated, or ``None`` if the event
    was ignored (wrong type, or not PR-shaped).
    """
    if event_type not in RELEVANT_EVENTS:
        return None
    target = repo_and_number_from_event(event_type, body)
    if target is None:
        return None
    repo, number = target

    search_item = issue_shaped_item(repo, number, body)
    events, reviews, comments, pr_detail = plr.fetch_pr_detail(cfg, repo, number)
    merge_commit_sha = pr_detail.get("merge_commit_sha") or ""
    record = plr.build_pr_record(
        search_item, events, reviews, comments, cfg, merge_commit_sha=merge_commit_sha
    )
    anchor = plr.parse_iso8601(record.review_requested_at) or plr.parse_iso8601(record.created_at)
    review_events = plr.build_review_events(repo, number, reviews, anchor, cfg)

    store = plr.Store(cfg.db_path)
    try:
        collected_at = plr.to_iso8601(datetime.now(tz=timezone.utc)) or ""
        store.upsert_pr(record, collected_at)
        store.replace_reviews(repo, number, review_events)
    finally:
        store.close()
    return f"{repo}#{number}"


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    cfg: plr.Config = None  # set by main() before serve_forever

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming convention)
        if self.path.rstrip("/") != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body_bytes = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256")
        if not verify_signature(WEBHOOK_SECRET, body_bytes, signature):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return
        event_type = self.headers.get("X-GitHub-Event", "")
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        updated = handle_event(self.cfg, event_type, body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"updated": updated}).encode("utf-8"))

    def log_message(self, fmt, *args) -> None:  # quiet by default; no token risk either way
        pass


def main() -> int:
    cfg = plr.load_config()
    if not WEBHOOK_SECRET:
        print("GH_WEBHOOK_SECRET is required.", file=sys.stderr)
        return 2
    WebhookHandler.cfg = cfg
    port = int(os.getenv("PORT", "8080"))
    server = http.server.HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"pr-latency-reporter webhook handler listening on :{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
