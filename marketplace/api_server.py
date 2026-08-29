#!/usr/bin/env python3
"""Read-only HTTP JSON API over the pr-latency-reporter datastore.

Stdlib-only (``http.server``), consistent with the core tool's zero-deps
design. Exists because both marketplace surfaces need one:

- The **Forge app** (``marketplace/forge-app/``) runs its resolver in
  Atlassian's cloud sandbox, which cannot read a local SQLite file — it can
  only call out to an HTTPS API Karl hosts himself.
- The **GitHub App** (``marketplace/github-app/``) can use this for any
  dashboard surface beyond the existing Slack digest / Markdown summary.

This does not replace ``pr_latency_reporter.py``'s own collection/reporting
loop — it is a thin read layer on top of the same SQLite datastore, run as a
separate long-lived process alongside the cron job.

Run:

    API_AUTH_TOKEN=some-long-random-value python3 marketplace/api_server.py

Endpoints (GET only, all require ``Authorization: Bearer $API_AUTH_TOKEN``):

    /api/v1/health
    /api/v1/summary?window=week|month&team=<slug>   (team optional -> all teams)
    /api/v1/teams?window=week|month
    /api/v1/seniority?window=week|month

Manual step for Karl: this process needs to be reachable over HTTPS from
Atlassian's Forge sandbox (a public URL — e.g. behind a reverse proxy /
tunnel / small VM you control). Nothing in this repo can provision that.
Never bind it to 0.0.0.0 without a token set and a proxy in front of it.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.parse
from dataclasses import asdict
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pr_latency_reporter as plr  # noqa: E402

API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "")
PORT = int(os.getenv("API_PORT", "8090"))


def _window(name: str, cfg: plr.Config, now: datetime) -> tuple[datetime, datetime]:
    if name == "week":
        return plr.week_window(now, cfg.tz)
    if name == "month":
        month_label = f"{now.year:04d}-{now.month:02d}"
        return plr.month_window(month_label, cfg.tz)
    raise ValueError("window must be 'week' or 'month'")


def build_summary_payload(cfg: plr.Config, *, window: str, team: str | None) -> dict:
    now = datetime.now(tz=cfg.tz)
    start, end = _window(window, cfg, now)
    store = plr.Store(cfg.db_path)
    try:
        records = store.fetch_between(start, end)
        if team:
            records = [r for r in records if r.team_slug == team]
        agg = plr.aggregate(records, cfg)
        payload = asdict(agg)
        payload.pop("breaches")
        payload["breach_count"] = len(agg.breaches)
        payload["window"] = window
        payload["team"] = team or "all"
        return payload
    finally:
        store.close()


def build_teams_payload(cfg: plr.Config, *, window: str) -> dict:
    now = datetime.now(tz=cfg.tz)
    start, end = _window(window, cfg, now)
    store = plr.Store(cfg.db_path)
    try:
        records = store.fetch_between(start, end)
        team_aggs = plr.aggregate_by_team(records, cfg)
        return {
            "window": window,
            "teams": [
                {
                    "team_slug": ta.team_slug,
                    "total": ta.agg.total,
                    "median_tta_bh": ta.agg.median_tta,
                    "median_ttfr_bh": ta.agg.median_ttfr,
                    "sla_approval_pct": ta.agg.sla_approval_pct,
                    "defect_pct": ta.agg.defect_pct,
                }
                for ta in team_aggs
            ],
        }
    finally:
        store.close()


def build_seniority_payload(cfg: plr.Config, *, window: str) -> dict:
    now = datetime.now(tz=cfg.tz)
    start, end = _window(window, cfg, now)
    store = plr.Store(cfg.db_path)
    try:
        records = store.fetch_between(start, end)
        review_events = store.fetch_reviews_for(records)
        rollup = plr.seniority_rollup(review_events, records, cfg)
        return {"window": window, "seniority": [asdict(s) for s in rollup]}
    finally:
        store.close()


class ApiHandler(http.server.BaseHTTPRequestHandler):
    cfg: plr.Config = None  # set by main() before serve_forever

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming convention)
        if not API_AUTH_TOKEN:
            self._json(500, {"error": "API_AUTH_TOKEN not configured on the server"})
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {API_AUTH_TOKEN}":
            self._unauthorized()
            return

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        window = qs.get("window", ["week"])[0]
        team = qs.get("team", [None])[0]

        try:
            if parsed.path == "/api/v1/health":
                self._json(200, {"status": "ok"})
            elif parsed.path == "/api/v1/summary":
                self._json(200, build_summary_payload(self.cfg, window=window, team=team))
            elif parsed.path == "/api/v1/teams":
                self._json(200, build_teams_payload(self.cfg, window=window))
            elif parsed.path == "/api/v1/seniority":
                self._json(200, build_seniority_payload(self.cfg, window=window))
            else:
                self._json(404, {"error": "not found"})
        except ValueError as err:
            self._json(400, {"error": str(err)})

    def log_message(self, fmt, *args) -> None:  # quiet by default
        pass


def main() -> int:
    cfg = plr.load_config()
    if not API_AUTH_TOKEN:
        print("API_AUTH_TOKEN is required.", file=sys.stderr)
        return 2
    ApiHandler.cfg = cfg
    server = http.server.HTTPServer(("0.0.0.0", PORT), ApiHandler)
    print(f"pr-latency-reporter API listening on :{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
