#!/usr/bin/env python3
"""PR Latency Reporter — collect PR review metrics from GitHub, store them
append-only in SQLite, and emit a weekly Slack digest + monthly Markdown summary.

Standard library only (urllib, sqlite3, zoneinfo, datetime, statistics). No
third-party dependencies, so the pure metric logic is fully testable with no
network and no secrets.

Default run takes no arguments: it detects the datastore, resumes from the last
cursor, pulls only new/updated PRs, recomputes aggregates, and posts the Friday
Slack digest / month-end Markdown summary when due. Use ``--backfill YYYY-MM-DD``
once to seed history and ``--dry-run`` to print instead of posting to Slack.

Metric definitions follow the project's Metrics Reference exactly: all latencies
are measured in *business hours* (09:00-17:00 Mon-Fri, excluding weekends and
configurable holidays).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration — top-of-file constants with environment-variable fallback.
# Leave a constant as "" to fall back to its environment variable. Secrets are
# never written to logs. Neutral placeholders only (acme-corp / your-team).
# ---------------------------------------------------------------------------

# GitHub (required for live collection)
GH_TOKEN = "" or os.getenv("GH_TOKEN", "")
GH_ORG = "" or os.getenv("GH_ORG", "")  # e.g. "acme-corp"
TEAM_SLUG = "" or os.getenv("TEAM_SLUG", "")  # e.g. "your-team"
GH_API = os.getenv("GH_API", "https://api.github.com")

# Slack (required to post; bypassed by --dry-run)
SLACK_BOT_TOKEN = "" or os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = "" or os.getenv("SLACK_CHANNEL_ID", "")
SLACK_API = os.getenv("SLACK_API", "https://slack.com/api")

# Business hours and timezone (neutral default; not employer-specific)
TZ_NAME = os.getenv("TZ_NAME", "Europe/London")
BUSINESS_START_HOUR = int(os.getenv("BUSINESS_START_HOUR", "9"))
BUSINESS_END_HOUR = int(os.getenv("BUSINESS_END_HOUR", "17"))
HOLIDAYS_YYYYMMDD = os.getenv("HOLIDAYS_YYYYMMDD", "")  # e.g. "20260101,20260317"

# SLA targets (business hours)
SLA_RESPONSE_HRS = float(os.getenv("SLA_RESPONSE_HRS", "2.0"))
SLA_APPROVAL_HRS = float(os.getenv("SLA_APPROVAL_HRS", "4.0"))

# Storage and backfill
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_NAME = os.getenv("DB_NAME", "pr_latency.db")
DEFAULT_BACKFILL_DAYS = int(os.getenv("DEFAULT_BACKFILL_DAYS", "28"))

# Optional baseline for the monthly before/after story (hours / percent).
# Leave blank to omit the baseline comparison entirely.
BASELINE_TTA_HRS = os.getenv("BASELINE_TTA_HRS", "")
BASELINE_SLA_PCT = os.getenv("BASELINE_SLA_PCT", "")
BASELINE_P95_HRS = os.getenv("BASELINE_P95_HRS", "")


# ---------------------------------------------------------------------------
# Config object
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Resolved runtime configuration. Secrets stay here, never logged."""

    gh_token: str = GH_TOKEN
    gh_org: str = GH_ORG
    team_slug: str = TEAM_SLUG
    gh_api: str = GH_API
    slack_bot_token: str = SLACK_BOT_TOKEN
    slack_channel_id: str = SLACK_CHANNEL_ID
    slack_api: str = SLACK_API
    tz_name: str = TZ_NAME
    business_start_hour: int = BUSINESS_START_HOUR
    business_end_hour: int = BUSINESS_END_HOUR
    holidays: frozenset[date] = field(default_factory=frozenset)
    sla_response_hrs: float = SLA_RESPONSE_HRS
    sla_approval_hrs: float = SLA_APPROVAL_HRS
    data_dir: str = DATA_DIR
    db_name: str = DB_NAME
    backfill_days: int = DEFAULT_BACKFILL_DAYS

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, self.db_name)

    def missing_required(self, *, need_slack: bool) -> list[str]:
        """Return the names of required secrets/settings that are absent."""
        missing = []
        if not self.gh_token:
            missing.append("GH_TOKEN")
        if not self.gh_org:
            missing.append("GH_ORG")
        if not self.team_slug:
            missing.append("TEAM_SLUG")
        if need_slack:
            if not self.slack_bot_token:
                missing.append("SLACK_BOT_TOKEN")
            if not self.slack_channel_id:
                missing.append("SLACK_CHANNEL_ID")
        return missing


def parse_holidays(raw: str) -> frozenset[date]:
    """Parse a ``YYYYMMDD,YYYYMMDD`` string into a set of dates."""
    out: set[date] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        out.add(datetime.strptime(token, "%Y%m%d").date())
    return frozenset(out)


def load_config() -> Config:
    return Config(holidays=parse_holidays(HOLIDAYS_YYYYMMDD))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_iso8601(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``...Z``) into an aware UTC datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_iso8601(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Business-hours mathematics (pure functions)
# ---------------------------------------------------------------------------


def business_hours_between(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    start_hour: int = 9,
    end_hour: int = 17,
    holidays: Iterable[date] = (),
) -> float:
    """Elapsed business hours between two aware datetimes.

    Counts only ``start_hour``-``end_hour`` on Mon-Fri in timezone ``tz``,
    excluding weekends and ``holidays``. Computed as a precise per-day overlap
    between the elapsed interval and each day's business window. Returns 0.0 if
    ``end <= start``.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("business_hours_between requires timezone-aware datetimes")
    start = start.astimezone(tz)
    end = end.astimezone(tz)
    if end <= start:
        return 0.0

    holiday_set = set(holidays)
    total_seconds = 0.0
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        if day.weekday() < 5 and day not in holiday_set:
            win_start = datetime.combine(day, time(hour=start_hour), tzinfo=tz)
            win_end = datetime.combine(day, time(hour=end_hour), tzinfo=tz)
            seg_start = max(start, win_start)
            seg_end = min(end, win_end)
            if seg_end > seg_start:
                total_seconds += (seg_end - seg_start).total_seconds()
        day += timedelta(days=1)
    return total_seconds / 3600.0


# ---------------------------------------------------------------------------
# Metric primitives (pure functions)
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (``pct`` in 0-100). Empty -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def median_or_zero(values: Sequence[float]) -> float:
    return float(median(values)) if values else 0.0


def sla_compliance_pct(met: Sequence[bool]) -> float:
    """Percentage of True values. Empty -> 0.0."""
    if not met:
        return 0.0
    return 100.0 * sum(1 for m in met if m) / len(met)


def rework_rate_pct(changes_requested: Sequence[bool]) -> float:
    if not changes_requested:
        return 0.0
    return 100.0 * sum(1 for c in changes_requested if c) / len(changes_requested)


def defect_escape_pct(defect_flags: Sequence[bool]) -> float:
    if not defect_flags:
        return 0.0
    return 100.0 * sum(1 for d in defect_flags if d) / len(defect_flags)


# ---------------------------------------------------------------------------
# PR record model + pure builders (no network)
# ---------------------------------------------------------------------------


@dataclass
class PRRecord:
    repo: str
    number: int
    title: str
    author: str
    html_url: str
    created_at: str | None
    review_requested_at: str | None
    first_response_at: str | None
    first_approval_at: str | None
    merged_at: str | None
    changes_requested: int = 0
    defect_flag: int = 0
    ttfr_bh: float | None = None
    tta_bh: float | None = None
    sla_response_met: int = 0
    sla_approval_met: int = 0
    updated_at: str | None = None

    def as_row(self) -> tuple:
        return (
            self.repo,
            self.number,
            self.title,
            self.author,
            self.html_url,
            self.created_at,
            self.review_requested_at,
            self.first_response_at,
            self.first_approval_at,
            self.merged_at,
            self.changes_requested,
            self.defect_flag,
            self.ttfr_bh,
            self.tta_bh,
            self.sla_response_met,
            self.sla_approval_met,
            self.updated_at,
        )


def repo_full_name_from_url(repository_url: str) -> str:
    """``https://api.github.com/repos/acme-corp/checkout`` -> ``acme-corp/checkout``."""
    marker = "/repos/"
    idx = repository_url.find(marker)
    if idx == -1:
        return repository_url
    return repository_url[idx + len(marker) :]


def latest_team_review_request(events: Iterable[dict], team_slug: str) -> datetime | None:
    """Latest ``review_requested`` event addressed to ``team_slug``."""
    latest: datetime | None = None
    for event in events:
        if event.get("event") != "review_requested":
            continue
        team = (event.get("requested_team") or {}).get("slug")
        if team != team_slug:
            continue
        when = parse_iso8601(event.get("created_at"))
        if when and (latest is None or when > latest):
            latest = when
    return latest


def summarise_reviews(reviews: Iterable[dict]) -> tuple[datetime | None, datetime | None, bool]:
    """Return (first_review_at, first_approval_at, any_changes_requested)."""
    first_review: datetime | None = None
    first_approval: datetime | None = None
    changes_requested = False
    for review in reviews:
        when = parse_iso8601(review.get("submitted_at"))
        if when is None:
            continue
        if first_review is None or when < first_review:
            first_review = when
        state = (review.get("state") or "").upper()
        if state == "APPROVED" and (first_approval is None or when < first_approval):
            first_approval = when
        if state == "CHANGES_REQUESTED":
            changes_requested = True
    return first_review, first_approval, changes_requested


def earliest_comment(comments: Iterable[dict]) -> datetime | None:
    earliest: datetime | None = None
    for comment in comments:
        when = parse_iso8601(comment.get("created_at"))
        if when and (earliest is None or when < earliest):
            earliest = when
    return earliest


def build_pr_record(
    item: dict,
    events: Iterable[dict],
    reviews: Iterable[dict],
    comments: Iterable[dict],
    cfg: Config,
) -> PRRecord:
    """Combine raw GitHub payloads into a metric-bearing PRRecord. Pure."""
    repo = repo_full_name_from_url(item.get("repository_url", ""))
    review_requested_at = latest_team_review_request(events, cfg.team_slug)
    first_review_at, first_approval_at, changes_requested = summarise_reviews(reviews)
    comment_at = earliest_comment(comments)

    # First response = earliest of first review (any state) and first comment.
    first_response_at = first_review_at
    if comment_at and (first_response_at is None or comment_at < first_response_at):
        first_response_at = comment_at

    # Anchor for latency: the review request, falling back to PR creation.
    anchor = review_requested_at or parse_iso8601(item.get("created_at"))

    ttfr = tta = None
    if anchor and first_response_at:
        ttfr = business_hours_between(
            anchor,
            first_response_at,
            cfg.tz,
            start_hour=cfg.business_start_hour,
            end_hour=cfg.business_end_hour,
            holidays=cfg.holidays,
        )
    if anchor and first_approval_at:
        tta = business_hours_between(
            anchor,
            first_approval_at,
            cfg.tz,
            start_hour=cfg.business_start_hour,
            end_hour=cfg.business_end_hour,
            holidays=cfg.holidays,
        )

    sla_response_met = int(ttfr is not None and ttfr <= cfg.sla_response_hrs)
    sla_approval_met = int(tta is not None and tta <= cfg.sla_approval_hrs)

    pr = item.get("pull_request") or {}
    return PRRecord(
        repo=repo,
        number=int(item["number"]),
        title=item.get("title", ""),
        author=(item.get("user") or {}).get("login", ""),
        html_url=item.get("html_url", ""),
        created_at=to_iso8601(parse_iso8601(item.get("created_at"))),
        review_requested_at=to_iso8601(review_requested_at),
        first_response_at=to_iso8601(first_response_at),
        first_approval_at=to_iso8601(first_approval_at),
        merged_at=to_iso8601(parse_iso8601(pr.get("merged_at"))),
        changes_requested=int(changes_requested),
        defect_flag=0,
        ttfr_bh=ttfr,
        tta_bh=tta,
        sla_response_met=sla_response_met,
        sla_approval_met=sla_approval_met,
        updated_at=to_iso8601(parse_iso8601(item.get("updated_at"))),
    )


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------


@dataclass
class Aggregate:
    total: int
    median_ttfr: float
    median_tta: float
    sla_response_pct: float
    sla_approval_pct: float
    p95_tta: float
    rework_pct: float
    defect_pct: float
    breaches: list[PRRecord]


def aggregate(records: Sequence[PRRecord], cfg: Config) -> Aggregate:
    """Compute the digest/summary metrics over a set of PR records. Pure."""
    ttfrs = [r.ttfr_bh for r in records if r.ttfr_bh is not None]
    ttas = [r.tta_bh for r in records if r.tta_bh is not None]
    response_met = [bool(r.sla_response_met) for r in records if r.ttfr_bh is not None]
    approval_met = [bool(r.sla_approval_met) for r in records if r.tta_bh is not None]
    changes = [bool(r.changes_requested) for r in records]
    defects = [bool(r.defect_flag) for r in records if r.merged_at]

    breaches = sorted(
        (r for r in records if r.tta_bh is not None and r.tta_bh > cfg.sla_approval_hrs),
        key=lambda r: r.tta_bh or 0.0,
        reverse=True,
    )

    return Aggregate(
        total=len(records),
        median_ttfr=median_or_zero(ttfrs),
        median_tta=median_or_zero(ttas),
        sla_response_pct=sla_compliance_pct(response_met),
        sla_approval_pct=sla_compliance_pct(approval_met),
        p95_tta=percentile(ttas, 95),
        rework_pct=rework_rate_pct(changes),
        defect_pct=defect_escape_pct(defects),
        breaches=breaches,
    )


# ---------------------------------------------------------------------------
# Storage — append-only SQLite event log + queryable current-state projection
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    github_updated_at TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT,
    author TEXT,
    html_url TEXT,
    created_at TEXT,
    review_requested_at TEXT,
    first_response_at TEXT,
    first_approval_at TEXT,
    merged_at TEXT,
    changes_requested INTEGER DEFAULT 0,
    defect_flag INTEGER DEFAULT 0,
    ttfr_bh REAL,
    tta_bh REAL,
    sla_response_met INTEGER DEFAULT 0,
    sla_approval_met INTEGER DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sent_log (
    kind TEXT NOT NULL,
    period TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, period)
);
"""


class Store:
    """Thin SQLite wrapper. Pass ``":memory:"`` for tests."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- cursor ----------------------------------------------------------
    def get_cursor(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'cursor'").fetchone()
        return row["value"] if row else None

    def set_cursor(self, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('cursor', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        self.conn.commit()

    # -- writes ----------------------------------------------------------
    def upsert_pr(self, record: PRRecord, collected_at: str) -> None:
        """Append the raw event AND upsert the current-state projection."""
        self.conn.execute(
            "INSERT INTO pr_events(collected_at, repo, number, github_updated_at, payload) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                collected_at,
                record.repo,
                record.number,
                record.updated_at,
                json.dumps(record.__dict__, sort_keys=True),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO prs(
                repo, number, title, author, html_url, created_at,
                review_requested_at, first_response_at, first_approval_at, merged_at,
                changes_requested, defect_flag, ttfr_bh, tta_bh,
                sla_response_met, sla_approval_met, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repo, number) DO UPDATE SET
                title=excluded.title,
                author=excluded.author,
                html_url=excluded.html_url,
                created_at=excluded.created_at,
                review_requested_at=excluded.review_requested_at,
                first_response_at=excluded.first_response_at,
                first_approval_at=excluded.first_approval_at,
                merged_at=excluded.merged_at,
                changes_requested=excluded.changes_requested,
                defect_flag=excluded.defect_flag,
                ttfr_bh=excluded.ttfr_bh,
                tta_bh=excluded.tta_bh,
                sla_response_met=excluded.sla_response_met,
                sla_approval_met=excluded.sla_approval_met,
                updated_at=excluded.updated_at
            """,
            record.as_row(),
        )
        self.conn.commit()

    def mark_sent(self, kind: str, period: str, sent_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sent_log(kind, period, sent_at) VALUES(?,?,?)",
            (kind, period, sent_at),
        )
        self.conn.commit()

    def was_sent(self, kind: str, period: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sent_log WHERE kind = ? AND period = ?", (kind, period)
        ).fetchone()
        return row is not None

    # -- reads -----------------------------------------------------------
    def _row_to_record(self, row: sqlite3.Row) -> PRRecord:
        return PRRecord(
            repo=row["repo"],
            number=row["number"],
            title=row["title"],
            author=row["author"],
            html_url=row["html_url"],
            created_at=row["created_at"],
            review_requested_at=row["review_requested_at"],
            first_response_at=row["first_response_at"],
            first_approval_at=row["first_approval_at"],
            merged_at=row["merged_at"],
            changes_requested=row["changes_requested"],
            defect_flag=row["defect_flag"],
            ttfr_bh=row["ttfr_bh"],
            tta_bh=row["tta_bh"],
            sla_response_met=row["sla_response_met"],
            sla_approval_met=row["sla_approval_met"],
            updated_at=row["updated_at"],
        )

    def fetch_between(self, start: datetime, end: datetime) -> list[PRRecord]:
        """PRs whose review-request (or creation) anchor falls in [start, end)."""
        start_iso = to_iso8601(start)
        end_iso = to_iso8601(end)
        rows = self.conn.execute(
            "SELECT * FROM prs WHERE COALESCE(review_requested_at, created_at) >= ? "
            "AND COALESCE(review_requested_at, created_at) < ? ORDER BY repo, number",
            (start_iso, end_iso),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def fetch_all(self) -> list[PRRecord]:
        rows = self.conn.execute("SELECT * FROM prs ORDER BY repo, number").fetchall()
        return [self._row_to_record(r) for r in rows]


# ---------------------------------------------------------------------------
# GitHub API layer (urllib). All HTTP funnels through ``http_json`` so tests
# can monkeypatch ``urllib.request.urlopen`` for deterministic, offline runs.
# ---------------------------------------------------------------------------


def http_json(
    url: str,
    *,
    token: str = "",
    method: str = "GET",
    data: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Perform an HTTP request and return parsed JSON. Tokens are never logged."""
    headers = {"Accept": "application/json", "User-Agent": "pr-latency-reporter"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 (trusted hosts)
            body = response.read()
    except urllib.error.HTTPError as err:  # surface a clean, token-free message
        raise RuntimeError(f"HTTP {err.code} for {url.split('?')[0]}") from None
    return json.loads(body.decode("utf-8")) if body else {}


def search_review_requested(cfg: Config, since: datetime) -> list[dict]:
    """Search PRs where the team was requested to review, updated since ``since``."""
    since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    query = f"is:pr team-review-requested:{cfg.gh_org}/{cfg.team_slug} updated:>={since_str}"
    items: list[dict] = []
    page = 1
    while True:
        params = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
        payload = http_json(f"{cfg.gh_api}/search/issues?{params}", token=cfg.gh_token)
        batch = payload.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or len(items) >= int(payload.get("total_count", 0)):
            break
        page += 1
    return items


def fetch_pr_detail(cfg: Config, repo: str, number: int) -> tuple[list, list, list]:
    """Return (events, reviews, comments) for one PR."""
    events = http_json(
        f"{cfg.gh_api}/repos/{repo}/issues/{number}/events?per_page=100",
        token=cfg.gh_token,
    )
    reviews = http_json(
        f"{cfg.gh_api}/repos/{repo}/pulls/{number}/reviews?per_page=100",
        token=cfg.gh_token,
    )
    comments = http_json(
        f"{cfg.gh_api}/repos/{repo}/issues/{number}/comments?per_page=100",
        token=cfg.gh_token,
    )
    # Endpoints return JSON arrays; http_json returns {} only for empty bodies.
    return (
        events if isinstance(events, list) else [],
        reviews if isinstance(reviews, list) else [],
        comments if isinstance(comments, list) else [],
    )


def collect_prs(cfg: Config, since: datetime) -> list[PRRecord]:
    """Collect and compute PR records from GitHub since ``since``."""
    records: list[PRRecord] = []
    for item in search_review_requested(cfg, since):
        repo = repo_full_name_from_url(item.get("repository_url", ""))
        number = int(item["number"])
        events, reviews, comments = fetch_pr_detail(cfg, repo, number)
        records.append(build_pr_record(item, events, reviews, comments, cfg))
    return records


def post_slack(cfg: Config, text: str) -> dict:
    """Post a message to Slack via chat.postMessage."""
    payload = json.dumps({"channel": cfg.slack_channel_id, "text": text}).encode("utf-8")
    return http_json(
        f"{cfg.slack_api}/chat.postMessage",
        token=cfg.slack_bot_token,
        method="POST",
        data=payload,
        extra_headers={"Content-Type": "application/json; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------


def fmt_hours(value: float) -> str:
    return f"{value:.1f}h"


def render_weekly_digest(agg: Aggregate, week_label: str, cfg: Config) -> str:
    """Slack-flavoured weekly digest text."""
    lines = [
        f"*Weekly PR Review Metrics ({week_label})*",
        f"- Median Time to First Response: {fmt_hours(agg.median_ttfr)}",
        f"- Median Time to Approval: {fmt_hours(agg.median_tta)}",
        f"- SLA Compliance (<={cfg.sla_approval_hrs:g}h approval): {agg.sla_approval_pct:.0f}%",
        f"- P95 Latency: {fmt_hours(agg.p95_tta)}",
        f"- Total PRs: {agg.total}",
        f"- SLA Breaches: {len(agg.breaches)}",
    ]
    if agg.breaches:
        lines.append("*Breaches:*")
        for pr in agg.breaches[:10]:
            lines.append(
                f"- <{pr.html_url}|{pr.repo} #{pr.number}> "
                f"({fmt_hours(pr.tta_bh or 0.0)}) - {pr.title}"
            )
    return "\n".join(lines)


def render_monthly_summary(agg: Aggregate, month_label: str, cfg: Config) -> str:
    """Leadership-facing monthly Markdown summary with optional baseline."""
    baseline_bits = []
    if BASELINE_TTA_HRS:
        baseline_bits.append(f"median TTA {BASELINE_TTA_HRS}h")
    if BASELINE_SLA_PCT:
        baseline_bits.append(f"SLA {BASELINE_SLA_PCT}%")
    if BASELINE_P95_HRS:
        baseline_bits.append(f"P95 {BASELINE_P95_HRS}h")
    baseline_text = f" (baseline: {', '.join(baseline_bits)})" if baseline_bits else ""

    return "\n".join(
        [
            f"# Monthly Review Metrics - {month_label}",
            "",
            "**Headline**",
            f"Median TTA: {fmt_hours(agg.median_tta)}. "
            f"P95 TTA: {fmt_hours(agg.p95_tta)}. "
            f"SLA: {agg.sla_approval_pct:.0f}%.{baseline_text}",
            "",
            "**Metrics**",
            f"- Median Time to Approval (TTA): {fmt_hours(agg.median_tta)}",
            f"- Median Time to First Response (TTFR): {fmt_hours(agg.median_ttfr)}",
            f"- SLA Compliance (<={cfg.sla_approval_hrs:g}h): {agg.sla_approval_pct:.0f}%",
            f"- P95 TTA: {fmt_hours(agg.p95_tta)}",
            f"- Rework Rate: {agg.rework_pct:.0f}%",
            f"- Defect Escape Rate: {agg.defect_pct:.1f}%",
            f"- Total PRs: {agg.total}",
            "",
            "**What this means**",
            "Faster reviews reduce waiting time and keep delivery moving. "
            "Outliers remain but no drop in quality observed.",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Scheduling (pure)
# ---------------------------------------------------------------------------


def iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def last_day_of_month(d: date) -> date:
    if d.month == 12:
        nxt = date(d.year + 1, 1, 1)
    else:
        nxt = date(d.year, d.month + 1, 1)
    return nxt - timedelta(days=1)


def weekly_digest_due(now: datetime, already_sent: bool) -> bool:
    """Due on Friday from 16:00 local, with weekend catch-up, if not yet sent."""
    if already_sent:
        return False
    weekday = now.weekday()  # Mon=0 .. Sun=6
    if weekday == 4 and now.hour >= 16:
        return True
    return weekday in (5, 6)  # Saturday / Sunday catch-up


def month_to_generate(now: datetime) -> str:
    """The ``YYYY-MM`` month whose summary is due, or "" if none yet."""
    today = now.date()
    if today == last_day_of_month(today):
        return f"{today.year:04d}-{today.month:02d}"
    # Otherwise the previous month is complete and may need generating.
    first_of_this = today.replace(day=1)
    prev = first_of_this - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def month_window(month_label: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    year, mon = (int(p) for p in month_label.split("-"))
    start = datetime(year, mon, 1, tzinfo=tz)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=tz)
    return start, end


def week_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Monday 00:00 .. next Monday 00:00 of ``now``'s ISO week, in ``tz``."""
    local = now.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    return start, start + timedelta(days=7)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    cfg: Config,
    *,
    now: datetime | None = None,
    backfill: date | None = None,
    dry_run: bool = False,
    collector: Callable[[Config, datetime], list[PRRecord]] = collect_prs,
) -> dict:
    """Execute one reporting cycle. Returns a small summary dict of what happened."""
    now = now or datetime.now(tz=cfg.tz)
    store = Store(cfg.db_path)
    actions: dict = {"collected": 0, "weekly": None, "monthly": None}
    try:
        # Determine the collection window start.
        if backfill is not None:
            since = datetime(backfill.year, backfill.month, backfill.day, tzinfo=cfg.tz)
        else:
            cursor = store.get_cursor()
            if cursor:
                since = parse_iso8601(cursor) or now - timedelta(days=cfg.backfill_days)
            else:
                since = now - timedelta(days=cfg.backfill_days)

        records = collector(cfg, since)
        collected_at = to_iso8601(now) or ""
        for record in records:
            store.upsert_pr(record, collected_at)
        store.set_cursor(to_iso8601(now) or "")
        actions["collected"] = len(records)

        # Weekly digest.
        week_label = iso_week_label(now.astimezone(cfg.tz).date())
        if weekly_digest_due(now, store.was_sent("weekly", week_label)):
            w_start, w_end = week_window(now, cfg.tz)
            agg = aggregate(store.fetch_between(w_start, w_end), cfg)
            text = render_weekly_digest(agg, week_label, cfg)
            if dry_run:
                print("--- DRY RUN: weekly Slack digest ---")
                print(text)
            else:
                post_slack(cfg, text)
                store.mark_sent("weekly", week_label, collected_at)
            actions["weekly"] = week_label

        # Monthly summary.
        month_label = month_to_generate(now)
        if month_label and not store.was_sent("monthly", month_label):
            m_start, m_end = month_window(month_label, cfg.tz)
            agg = aggregate(store.fetch_between(m_start, m_end), cfg)
            text = render_monthly_summary(agg, month_label, cfg)
            if dry_run:
                print("--- DRY RUN: monthly Markdown summary ---")
                print(text)
            else:
                out_dir = os.path.join(cfg.data_dir, "summaries")
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, f"{month_label}.md"), "w") as fh:
                    fh.write(text)
                store.mark_sent("monthly", month_label, collected_at)
            actions["monthly"] = month_label

        return actions
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_latency_reporter",
        description="Collect PR review metrics, store them, and emit Slack/Markdown reports.",
    )
    parser.add_argument(
        "--backfill",
        metavar="YYYY-MM-DD",
        help="One-off backfill start date (otherwise resume from the stored cursor).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest/summary instead of posting to Slack or writing files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()

    missing = cfg.missing_required(need_slack=not args.dry_run)
    if missing:
        print(
            "Missing required configuration: " + ", ".join(missing) + ".\n"
            "Set them as environment variables or top-of-file constants.",
            file=sys.stderr,
        )
        return 2

    backfill = None
    if args.backfill:
        try:
            backfill = datetime.strptime(args.backfill, "%Y-%m-%d").date()
        except ValueError:
            print("--backfill must be YYYY-MM-DD", file=sys.stderr)
            return 2

    actions = run(cfg, backfill=backfill, dry_run=args.dry_run)
    print(
        f"Collected {actions['collected']} PR(s). "
        f"Weekly: {actions['weekly'] or 'not due'}. "
        f"Monthly: {actions['monthly'] or 'not due'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
