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
import re
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
TEAM_SLUG = "" or os.getenv("TEAM_SLUG", "")  # e.g. "your-team" (single-team; kept for back-compat)
# Multi-team, director/TPM-level observation: comma-separated team slugs, e.g.
# "platform-team,checkout-team,mobile-team". Falls back to TEAM_SLUG if unset,
# so a single-team setup keeps working unchanged.
TEAM_SLUGS = "" or os.getenv("TEAM_SLUGS", "")
GH_API = os.getenv("GH_API", "https://api.github.com")

# Reviewer seniority map for the "are seniors leading on review" thesis.
# Format: "login:tier,login:tier", e.g. "alice:senior,bob:mid,carol:senior".
# Reviewers not listed fall back to the "unspecified" tier. Neutral by design —
# never commit real logins/tiers here; set via env var per deployment.
REVIEWER_SENIORITY = "" or os.getenv("REVIEWER_SENIORITY", "")

# Defect-escape detection window: how many days after merge a "Revert" PR
# referencing the merge commit still counts as a defect escape.
DEFECT_LOOKBACK_DAYS = int(os.getenv("DEFECT_LOOKBACK_DAYS", "14"))

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
    team_slugs: tuple[str, ...] = field(default_factory=tuple)
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
    reviewer_seniority: dict[str, str] = field(default_factory=dict)
    defect_lookback_days: int = DEFECT_LOOKBACK_DAYS

    def __post_init__(self) -> None:
        # Multi-team is additive: TEAM_SLUGS wins when set, otherwise fall
        # back to the single legacy TEAM_SLUG so existing deployments are
        # unaffected. Either way `teams` is the one list the rest of the
        # code should iterate over.
        if not self.team_slugs and self.team_slug:
            self.team_slugs = (self.team_slug,)

    @property
    def teams(self) -> tuple[str, ...]:
        return self.team_slugs

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, self.db_name)

    def seniority_of(self, login: str) -> str:
        return self.reviewer_seniority.get(login, "unspecified")

    def missing_required(self, *, need_slack: bool) -> list[str]:
        """Return the names of required secrets/settings that are absent."""
        missing = []
        if not self.gh_token:
            missing.append("GH_TOKEN")
        if not self.gh_org:
            missing.append("GH_ORG")
        if not self.team_slugs:
            missing.append("TEAM_SLUG or TEAM_SLUGS")
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


def parse_team_slugs(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated team-slug list, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for token in raw.split(","):
        token = token.strip()
        if token:
            seen[token] = None
    return tuple(seen)


def parse_reviewer_seniority(raw: str) -> dict[str, str]:
    """Parse ``login:tier,login:tier`` into a ``{login: tier}`` map."""
    out: dict[str, str] = {}
    for token in raw.split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        login, _, tier = token.partition(":")
        login = login.strip()
        tier = tier.strip()
        if login and tier:
            out[login] = tier
    return out


def load_config() -> Config:
    return Config(
        holidays=parse_holidays(HOLIDAYS_YYYYMMDD),
        team_slugs=parse_team_slugs(TEAM_SLUGS),
        reviewer_seniority=parse_reviewer_seniority(REVIEWER_SENIORITY),
    )


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
    """% of merged PRs whose ``defect_flag`` is set.

    ``defect_flag`` is a real, GitHub-Revert-based signal (see
    ``defect_escape_shas`` below) — NOT a synthetic/hardcoded value. It was
    previously hardcoded to 0 at build time, which made this function always
    return 0.0% with no signal behind it. It is a deliberate LOWER BOUND: it
    only catches merges reverted via GitHub's own "Revert" PR flow (or a PR
    titled/bodied the same way) within ``defect_lookback_days`` of the
    original merge. A hotfix that doesn't reference the original commit, or a
    revert outside the lookback window, will not be counted. Documented in
    the README rather than presented as complete.
    """
    if not defect_flags:
        return 0.0
    return 100.0 * sum(1 for d in defect_flags if d) / len(defect_flags)


REVERT_SHA_RE = re.compile(r"This reverts commit ([0-9a-fA-F]{7,40})")


def extract_reverted_sha(body: str | None) -> str | None:
    """Parse the reverted commit SHA out of a GitHub-generated revert-PR body.

    GitHub's "Revert" button always produces a body containing
    ``This reverts commit <sha>.`` — a stable, first-party marker we can rely
    on without any heuristics. A manually authored revert PR that doesn't
    include that exact line is not detected (documented limitation).
    """
    if not body:
        return None
    m = REVERT_SHA_RE.search(body)
    return m.group(1)[:7] if m else None


def defect_escape_shas(revert_items: Iterable[dict]) -> set[str]:
    """Short (7-char) SHAs reverted by any PR in ``revert_items``.

    ``revert_items`` are GitHub search-API PR items (must include ``body``).
    Matching against ``PRRecord.merge_commit_sha[:7]`` is how ``defect_flag``
    gets set — see ``search_revert_prs`` / ``apply_defect_flags``.
    """
    shas: set[str] = set()
    for item in revert_items:
        sha = extract_reverted_sha(item.get("body"))
        if sha:
            shas.add(sha)
    return shas


def apply_defect_flags(records: Sequence[PRRecord], reverted_shas: set[str]) -> None:
    """Set ``defect_flag`` in place for any merged record whose
    ``merge_commit_sha`` (short form) was reverted. Pure w.r.t. its inputs
    other than mutating the records it's handed — mirrors how ``collect_prs``
    already builds records before storing them.
    """
    for record in records:
        if record.merge_commit_sha and record.merge_commit_sha[:7] in reverted_shas:
            record.defect_flag = 1


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
    team_slug: str = ""
    # Reviewer attribution (fixes the "who reviewed" gap): the GitHub login of
    # whoever gave the first response/approval. Needed for the per-reviewer
    # and per-seniority "are seniors leading" metrics — see ReviewEvent below
    # for the full per-review detail these are drawn from.
    first_responder_login: str = ""
    first_approver_login: str = ""
    # Merge commit SHA, used to match this PR against later "Revert" PRs for
    # the (honest, lower-bound) defect-escape signal. See defect_escape_shas().
    merge_commit_sha: str = ""

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
            self.team_slug,
            self.first_responder_login,
            self.first_approver_login,
            self.merge_commit_sha,
        )


@dataclass
class ReviewEvent:
    """One individual review, attributed to its reviewer. This is the record
    that fixes defect #1: the old code discarded ``review.user.login``
    entirely and only ever stored the PR *author*. Every review on a PR
    produces one of these, so per-reviewer and per-seniority metrics are
    computed from real per-reviewer data, not inferred from the PR record.
    """

    repo: str
    number: int
    reviewer: str
    state: str
    submitted_at: str | None
    response_bh: float | None  # business hours from the review-request anchor


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


def latest_team_review_request_multi(
    events: Iterable[dict], team_slugs: Iterable[str]
) -> tuple[datetime | None, str]:
    """Latest ``review_requested`` event across any of ``team_slugs``.

    Returns ``(when, team_slug)`` for whichever team's request is latest, or
    ``(None, "")`` if none of the configured teams were ever requested.
    Enables multi-team observation without changing the single-team
    ``latest_team_review_request`` primitive above (kept, and still tested,
    as the per-team building block).
    """
    events = list(events)
    best_when: datetime | None = None
    best_team = ""
    for team in team_slugs:
        when = latest_team_review_request(events, team)
        if when and (best_when is None or when > best_when):
            best_when = when
            best_team = team
    return best_when, best_team


def summarise_reviews(
    reviews: Iterable[dict],
) -> tuple[datetime | None, str, datetime | None, str, bool]:
    """Return (first_review_at, first_review_login, first_approval_at,
    first_approval_login, any_changes_requested).

    Captures ``review.user.login`` for both the first response and the first
    approval — this is the reviewer-attribution fix. Previously this function
    (and the record it fed) never read the login at all.
    """
    first_review: datetime | None = None
    first_review_login = ""
    first_approval: datetime | None = None
    first_approval_login = ""
    changes_requested = False
    for review in reviews:
        when = parse_iso8601(review.get("submitted_at"))
        if when is None:
            continue
        login = (review.get("user") or {}).get("login", "")
        if first_review is None or when < first_review:
            first_review = when
            first_review_login = login
        state = (review.get("state") or "").upper()
        if state == "APPROVED" and (first_approval is None or when < first_approval):
            first_approval = when
            first_approval_login = login
        if state == "CHANGES_REQUESTED":
            changes_requested = True
    return first_review, first_review_login, first_approval, first_approval_login, changes_requested


def build_review_events(
    repo: str,
    number: int,
    reviews: Iterable[dict],
    anchor: datetime | None,
    cfg: Config,
) -> list[ReviewEvent]:
    """One ``ReviewEvent`` per review, each attributed to its reviewer login.

    This is the full per-reviewer detail behind the per-seniority rollup
    (``reviewer_aggregate`` / ``seniority_rollup``) — every review counts,
    not just the first response/approval kept on ``PRRecord``.
    """
    out: list[ReviewEvent] = []
    for review in reviews:
        when = parse_iso8601(review.get("submitted_at"))
        login = (review.get("user") or {}).get("login", "")
        if not login:
            continue
        response_bh = None
        if anchor and when:
            response_bh = business_hours_between(
                anchor,
                when,
                cfg.tz,
                start_hour=cfg.business_start_hour,
                end_hour=cfg.business_end_hour,
                holidays=cfg.holidays,
            )
        out.append(
            ReviewEvent(
                repo=repo,
                number=number,
                reviewer=login,
                state=(review.get("state") or "").upper(),
                submitted_at=to_iso8601(when),
                response_bh=response_bh,
            )
        )
    return out


def earliest_comment(comments: Iterable[dict]) -> tuple[datetime | None, str]:
    earliest: datetime | None = None
    login = ""
    for comment in comments:
        when = parse_iso8601(comment.get("created_at"))
        if when and (earliest is None or when < earliest):
            earliest = when
            login = (comment.get("user") or {}).get("login", "")
    return earliest, login


def build_pr_record(
    item: dict,
    events: Iterable[dict],
    reviews: Iterable[dict],
    comments: Iterable[dict],
    cfg: Config,
    *,
    merge_commit_sha: str = "",
) -> PRRecord:
    """Combine raw GitHub payloads into a metric-bearing PRRecord. Pure."""
    repo = repo_full_name_from_url(item.get("repository_url", ""))
    review_requested_at, matched_team = latest_team_review_request_multi(events, cfg.teams)
    (
        first_review_at,
        first_review_login,
        first_approval_at,
        first_approval_login,
        changes_requested,
    ) = summarise_reviews(reviews)
    comment_at, comment_login = earliest_comment(comments)

    # First response = earliest of first review (any state) and first comment.
    first_response_at = first_review_at
    first_responder_login = first_review_login
    if comment_at and (first_response_at is None or comment_at < first_response_at):
        first_response_at = comment_at
        first_responder_login = comment_login

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
        defect_flag=0,  # set later, in a separate pass — see defect_escape_shas()
        ttfr_bh=ttfr,
        tta_bh=tta,
        sla_response_met=sla_response_met,
        sla_approval_met=sla_approval_met,
        updated_at=to_iso8601(parse_iso8601(item.get("updated_at"))),
        team_slug=matched_team,
        first_responder_login=first_responder_login,
        first_approver_login=first_approval_login,
        merge_commit_sha=merge_commit_sha,
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


@dataclass
class TeamAggregate:
    """One team's slice of the multi-team rollup. Director/TPM-level
    observation across teams — not a single-team performance verdict.
    """

    team_slug: str
    agg: Aggregate


def aggregate_by_team(records: Sequence[PRRecord], cfg: Config) -> list[TeamAggregate]:
    """Group records by ``team_slug`` and aggregate each group.

    Any record with no matched team (``team_slug == ""`` — e.g. anchored on
    PR creation because no configured team was ever requested) is grouped
    under ``"unassigned"`` rather than dropped, so nothing is silently
    excluded from the multi-team view. Sorted by team slug for a stable,
    non-editorialising order (not ranked "best"/"worst").
    """
    by_team: dict[str, list[PRRecord]] = {}
    for r in records:
        key = r.team_slug or "unassigned"
        by_team.setdefault(key, []).append(r)
    return [
        TeamAggregate(team_slug=team, agg=aggregate(recs, cfg))
        for team, recs in sorted(by_team.items())
    ]


@dataclass
class ReviewerStat:
    """Per-reviewer rollup — the data behind "are seniors leading on
    review". Built from ``ReviewEvent`` rows, i.e. real per-reviewer
    attribution, not the PR author (the bug this fixes).
    """

    reviewer: str
    seniority: str
    review_count: int
    approval_count: int
    median_response_bh: float


def reviewer_aggregate(events: Sequence[ReviewEvent], cfg: Config) -> list[ReviewerStat]:
    """Aggregate per-reviewer stats from a set of ``ReviewEvent`` rows."""
    by_reviewer: dict[str, list[ReviewEvent]] = {}
    for e in events:
        by_reviewer.setdefault(e.reviewer, []).append(e)
    out = []
    for reviewer, evs in sorted(by_reviewer.items()):
        response_times = [e.response_bh for e in evs if e.response_bh is not None]
        out.append(
            ReviewerStat(
                reviewer=reviewer,
                seniority=cfg.seniority_of(reviewer),
                review_count=len(evs),
                approval_count=sum(1 for e in evs if e.state == "APPROVED"),
                median_response_bh=median_or_zero(response_times),
            )
        )
    return out


@dataclass
class SeniorityStat:
    """One seniority tier's slice of review activity — the measurable
    version of "are seniors leading on review": what share of reviews and
    first approvals come from the senior tier, and how fast.
    """

    tier: str
    reviewer_count: int
    review_count: int
    median_response_bh: float
    share_of_reviews_pct: float
    share_of_first_approvals_pct: float


def seniority_rollup(
    events: Sequence[ReviewEvent], records: Sequence[PRRecord], cfg: Config
) -> list[SeniorityStat]:
    """Roll per-reviewer stats up to seniority tiers.

    ``share_of_first_approvals_pct`` is computed from ``PRRecord.
    first_approver_login`` (one first-approver per merged/approved PR);
    ``share_of_reviews_pct`` counts every individual review. Both are real
    numbers over real attribution — this is what makes the "are seniors
    leading" thesis measurable instead of asserted.
    """
    total_reviews = len(events)
    approvers = [r.first_approver_login for r in records if r.first_approver_login]
    total_approvals = len(approvers)

    by_tier: dict[str, list[ReviewEvent]] = {}
    reviewers_by_tier: dict[str, set[str]] = {}
    for e in events:
        tier = cfg.seniority_of(e.reviewer)
        by_tier.setdefault(tier, []).append(e)
        reviewers_by_tier.setdefault(tier, set()).add(e.reviewer)

    approvals_by_tier: dict[str, int] = {}
    for login in approvers:
        tier = cfg.seniority_of(login)
        approvals_by_tier[tier] = approvals_by_tier.get(tier, 0) + 1

    tiers = set(by_tier) | set(approvals_by_tier)
    out = []
    for tier in sorted(tiers):
        evs = by_tier.get(tier, [])
        response_times = [e.response_bh for e in evs if e.response_bh is not None]
        out.append(
            SeniorityStat(
                tier=tier,
                reviewer_count=len(reviewers_by_tier.get(tier, set())),
                review_count=len(evs),
                median_response_bh=median_or_zero(response_times),
                share_of_reviews_pct=(100.0 * len(evs) / total_reviews if total_reviews else 0.0),
                share_of_first_approvals_pct=(
                    100.0 * approvals_by_tier.get(tier, 0) / total_approvals
                    if total_approvals
                    else 0.0
                ),
            )
        )
    return out


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
    team_slug TEXT DEFAULT '',
    first_responder_login TEXT DEFAULT '',
    first_approver_login TEXT DEFAULT '',
    merge_commit_sha TEXT DEFAULT '',
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS pr_reviews (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    state TEXT,
    submitted_at TEXT,
    response_bh REAL,
    PRIMARY KEY (repo, number, reviewer, submitted_at)
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
                sla_response_met, sla_approval_met, updated_at,
                team_slug, first_responder_login, first_approver_login, merge_commit_sha
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                updated_at=excluded.updated_at,
                team_slug=excluded.team_slug,
                first_responder_login=excluded.first_responder_login,
                first_approver_login=excluded.first_approver_login,
                merge_commit_sha=excluded.merge_commit_sha
            """,
            record.as_row(),
        )
        self.conn.commit()

    def replace_reviews(self, repo: str, number: int, events: Sequence[ReviewEvent]) -> None:
        """Replace all stored ``ReviewEvent`` rows for one PR with ``events``.

        Delete+insert (rather than upsert) because re-collecting a PR can
        change which reviews exist (e.g. a dismissed review); the current
        set from GitHub is always the source of truth for the projection.
        """
        self.conn.execute("DELETE FROM pr_reviews WHERE repo = ? AND number = ?", (repo, number))
        self.conn.executemany(
            "INSERT INTO pr_reviews(repo, number, reviewer, state, submitted_at, response_bh) "
            "VALUES(?,?,?,?,?,?)",
            [
                (e.repo, e.number, e.reviewer, e.state, e.submitted_at, e.response_bh)
                for e in events
            ],
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
        keys = row.keys()
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
            team_slug=row["team_slug"] if "team_slug" in keys else "",
            first_responder_login=(
                row["first_responder_login"] if "first_responder_login" in keys else ""
            ),
            first_approver_login=(
                row["first_approver_login"] if "first_approver_login" in keys else ""
            ),
            merge_commit_sha=row["merge_commit_sha"] if "merge_commit_sha" in keys else "",
        )

    def _row_to_review_event(self, row: sqlite3.Row) -> ReviewEvent:
        return ReviewEvent(
            repo=row["repo"],
            number=row["number"],
            reviewer=row["reviewer"],
            state=row["state"],
            submitted_at=row["submitted_at"],
            response_bh=row["response_bh"],
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

    def fetch_reviews_for(self, records: Sequence[PRRecord]) -> list[ReviewEvent]:
        """All stored ``ReviewEvent`` rows for the given PR records."""
        out: list[ReviewEvent] = []
        for r in records:
            rows = self.conn.execute(
                "SELECT * FROM pr_reviews WHERE repo = ? AND number = ?", (r.repo, r.number)
            ).fetchall()
            out.extend(self._row_to_review_event(row) for row in rows)
        return out


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


def _search_issues(cfg: Config, query: str) -> list[dict]:
    """Paginate the GitHub Search (issues/PRs) API for one query."""
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


def search_review_requested(cfg: Config, since: datetime) -> list[dict]:
    """Search PRs where any configured team was requested to review, updated
    since ``since``. Multi-team: one search query per team slug (GitHub's
    ``team-review-requested`` filter only accepts one team), de-duplicated by
    PR URL so a PR requested from two configured teams isn't double-counted.
    """
    since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    seen: dict[str, dict] = {}
    for team in cfg.teams:
        query = f"is:pr team-review-requested:{cfg.gh_org}/{team} updated:>={since_str}"
        for item in _search_issues(cfg, query):
            seen.setdefault(item.get("html_url", item.get("url", "")), item)
    return list(seen.values())


def search_revert_prs(cfg: Config, since: datetime) -> list[dict]:
    """Search merged, revert-titled PRs org-wide since ``since``.

    Feeds the real (lower-bound) defect-escape signal — see
    ``defect_escape_shas`` / ``extract_reverted_sha``. One query for the
    whole org rather than per-PR, to keep this a single extra call per
    collection cycle instead of N+1.
    """
    since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    query = f'is:pr is:merged org:{cfg.gh_org} "Revert " in:title merged:>={since_str}'
    return _search_issues(cfg, query)


def fetch_pr_detail(cfg: Config, repo: str, number: int) -> tuple[list, list, list, dict]:
    """Return (events, reviews, comments, pr_detail) for one PR.

    ``pr_detail`` is the full ``/pulls/{number}`` object — fetched so
    ``merge_commit_sha`` is available for defect-escape matching (the search
    API's issue item does not include it).
    """
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
    pr_detail = http_json(
        f"{cfg.gh_api}/repos/{repo}/pulls/{number}",
        token=cfg.gh_token,
    )
    # List endpoints return JSON arrays; http_json returns {} only for empty
    # bodies. The single-PR detail endpoint returns an object.
    return (
        events if isinstance(events, list) else [],
        reviews if isinstance(reviews, list) else [],
        comments if isinstance(comments, list) else [],
        pr_detail if isinstance(pr_detail, dict) else {},
    )


@dataclass
class CollectResult:
    """What one collection cycle produced: the PR records (with defect flags
    already applied) and the full per-reviewer event log behind them.
    """

    records: list[PRRecord]
    review_events: list[ReviewEvent]


def collect_prs(cfg: Config, since: datetime) -> CollectResult:
    """Collect and compute PR records + per-reviewer events from GitHub
    since ``since``, across every configured team, with real defect-escape
    flags applied (see ``defect_escape_shas``).
    """
    records: list[PRRecord] = []
    review_events: list[ReviewEvent] = []
    for item in search_review_requested(cfg, since):
        repo = repo_full_name_from_url(item.get("repository_url", ""))
        number = int(item["number"])
        events, reviews, comments, pr_detail = fetch_pr_detail(cfg, repo, number)
        merge_commit_sha = pr_detail.get("merge_commit_sha") or ""
        record = build_pr_record(
            item, events, reviews, comments, cfg, merge_commit_sha=merge_commit_sha
        )
        records.append(record)
        anchor = parse_iso8601(record.review_requested_at) or parse_iso8601(record.created_at)
        review_events.extend(build_review_events(repo, number, reviews, anchor, cfg))

    reverted_shas = defect_escape_shas(search_revert_prs(cfg, since))
    apply_defect_flags(records, reverted_shas)

    return CollectResult(records=records, review_events=review_events)


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


def render_weekly_digest(
    agg: Aggregate,
    week_label: str,
    cfg: Config,
    team_aggs: Sequence[TeamAggregate] = (),
) -> str:
    """Slack-flavoured weekly digest text.

    Director/TPM-level awareness across teams, not a single-team verdict —
    ``team_aggs`` (from ``aggregate_by_team``) adds a neutral per-team
    breakdown, listed in a stable slug order rather than ranked "best" to
    "worst".
    """
    teams_note = f" — {len(team_aggs)} teams" if len(team_aggs) > 1 else ""
    lines = [
        f"*Weekly PR Review Metrics ({week_label}){teams_note}*",
        f"- Median Time to First Response: {fmt_hours(agg.median_ttfr)}",
        f"- Median Time to Approval: {fmt_hours(agg.median_tta)}",
        f"- SLA Compliance (<={cfg.sla_approval_hrs:g}h approval): {agg.sla_approval_pct:.0f}%",
        f"- P95 Latency: {fmt_hours(agg.p95_tta)}",
        f"- Total PRs: {agg.total}",
        f"- Latency Outliers (>{cfg.sla_approval_hrs:g}h approval): {len(agg.breaches)}",
    ]
    if len(team_aggs) > 1:
        lines.append("*By team (awareness, not a ranking):*")
        for ta in team_aggs:
            lines.append(
                f"- {ta.team_slug}: {ta.agg.total} PRs, "
                f"median TTA {fmt_hours(ta.agg.median_tta)}, SLA {ta.agg.sla_approval_pct:.0f}%"
            )
    if agg.breaches:
        lines.append("*Slowest to approve:*")
        for pr in agg.breaches[:10]:
            lines.append(
                f"- <{pr.html_url}|{pr.repo} #{pr.number}> "
                f"({fmt_hours(pr.tta_bh or 0.0)}) - {pr.title}"
            )
    return "\n".join(lines)


def render_monthly_summary(
    agg: Aggregate,
    month_label: str,
    cfg: Config,
    *,
    team_aggs: Sequence[TeamAggregate] = (),
    seniority_stats: Sequence[SeniorityStat] = (),
) -> str:
    """Leadership-facing monthly Markdown summary with optional baseline.

    Framed for someone overseeing multiple teams (director/TPM-level
    awareness), not a single-team performance callout — per Karl's direction:
    team-level managers get defensive about this data on their specific team;
    senior leadership found it valuable as a validation/awareness signal.
    """
    baseline_bits = []
    if BASELINE_TTA_HRS:
        baseline_bits.append(f"median TTA {BASELINE_TTA_HRS}h")
    if BASELINE_SLA_PCT:
        baseline_bits.append(f"SLA {BASELINE_SLA_PCT}%")
    if BASELINE_P95_HRS:
        baseline_bits.append(f"P95 {BASELINE_P95_HRS}h")
    baseline_text = f" (baseline: {', '.join(baseline_bits)})" if baseline_bits else ""

    lines = [
        f"# Monthly Review Metrics - {month_label}",
        "",
        "_Multi-team awareness signal for whoever is overseeing more than one "
        "team — not a single-team performance verdict._",
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
        f"- Defect Escape Rate (revert-based, lower bound): {agg.defect_pct:.1f}%",
        f"- Total PRs: {agg.total}",
        "",
    ]

    if len(team_aggs) > 1:
        lines.append("**By team**")
        for ta in team_aggs:
            lines.append(
                f"- {ta.team_slug}: {ta.agg.total} PRs, "
                f"median TTA {fmt_hours(ta.agg.median_tta)}, "
                f"SLA {ta.agg.sla_approval_pct:.0f}%"
            )
        lines.append("")

    if seniority_stats:
        lines.append("**Reviewer leadership by seniority**")
        lines.append(
            "_Measures whether senior reviewers are leading on review load and "
            "speed — set `REVIEWER_SENIORITY` to populate tiers._"
        )
        for s in seniority_stats:
            lines.append(
                f"- {s.tier}: {s.reviewer_count} reviewer(s), {s.review_count} reviews "
                f"({s.share_of_reviews_pct:.0f}% of all reviews), "
                f"{s.share_of_first_approvals_pct:.0f}% of first approvals, "
                f"median response {fmt_hours(s.median_response_bh)}"
            )
        lines.append("")

    lines += [
        "**What this means**",
        "An awareness signal across teams: where review latency and reviewer "
        "participation sit this month. It is not a verdict on any one team, "
        "and the defect-escape figure above is a lower bound (GitHub-revert "
        "based), not a complete quality measure — see the README.",
        "",
    ]
    return "\n".join(lines)


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
    collector: Callable[[Config, datetime], CollectResult] = collect_prs,
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

        result = collector(cfg, since)
        collected_at = to_iso8601(now) or ""
        for record in result.records:
            store.upsert_pr(record, collected_at)
        # Group review events per PR before replacing — replace_reviews is
        # per (repo, number) so a stale-review purge doesn't touch other PRs.
        by_pr: dict[tuple[str, int], list[ReviewEvent]] = {}
        for ev in result.review_events:
            by_pr.setdefault((ev.repo, ev.number), []).append(ev)
        for (repo, number), evs in by_pr.items():
            store.replace_reviews(repo, number, evs)
        store.set_cursor(to_iso8601(now) or "")
        actions["collected"] = len(result.records)

        # Weekly digest.
        week_label = iso_week_label(now.astimezone(cfg.tz).date())
        if weekly_digest_due(now, store.was_sent("weekly", week_label)):
            w_start, w_end = week_window(now, cfg.tz)
            week_records = store.fetch_between(w_start, w_end)
            agg = aggregate(week_records, cfg)
            team_aggs = aggregate_by_team(week_records, cfg)
            text = render_weekly_digest(agg, week_label, cfg, team_aggs=team_aggs)
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
            month_records = store.fetch_between(m_start, m_end)
            agg = aggregate(month_records, cfg)
            team_aggs = aggregate_by_team(month_records, cfg)
            review_events = store.fetch_reviews_for(month_records)
            seniority_stats = seniority_rollup(review_events, month_records, cfg)
            text = render_monthly_summary(
                agg, month_label, cfg, team_aggs=team_aggs, seniority_stats=seniority_stats
            )
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
