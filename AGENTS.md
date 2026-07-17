# AGENTS.md — PR Latency Reporter

The canonical agent instructions for this repo (`CLAUDE.md` defers here).

## What this is

A single-file Python CLI that measures how fast pull requests get reviewed.
It pulls PR review-lifecycle events from the GitHub API, computes
**business-hours-aware** latency metrics (TTFR, TTA, SLA %, P95, rework rate,
defect-escape rate), stores them append-only in SQLite, and emits a **weekly
Slack digest** (Fridays) plus a **monthly Markdown summary**. It is not a
GitHub Action — there is no `action.yml`; it runs as a plain script on a cron
(hourly, `5 * * * *`) or invoked ad hoc.

## Stack

Python 3.10+ (CI runs 3.12) · `uv` for env/dependency management ·
**zero runtime dependencies** — `urllib` for HTTP, `sqlite3` for storage,
`zoneinfo` for timezones, `datetime`/`statistics` for the maths. Dev tooling
only: `ruff` (lint + format) and `pytest`. `licence: UNLICENSED` — personal
tooling, not published for reuse; do not add a licence.

## Build / test / lint

```bash
uv sync --dev                 # install (or: python3 -m venv .venv && pip install ruff pytest)
uv run ruff check .           # lint
uv run ruff format --check .  # format check (does not rewrite files)
uv run pytest                 # test — 42 tests, no network, no tokens
```

Run the tool itself:

```bash
python3 pr_latency_reporter.py                     # default: resume from cursor, post when due
python3 pr_latency_reporter.py --backfill 2026-01-01  # one-off backfill
python3 pr_latency_reporter.py --dry-run           # print instead of posting/writing
```

## Architecture

Everything lives in one file, `pr_latency_reporter.py`, organised into clearly
banner-commented sections (in order): Configuration → Config object →
datetime helpers → business-hours maths → metric primitives → `PRRecord` +
`build_pr_record` → `Aggregate`/`aggregate` → `Store` (SQLite) → GitHub/Slack
HTTP → rendering (digest/summary) → scheduling → `run()` orchestration → CLI
(`argparse`, `main`).

- **Latency computation** — `business_hours_between()` (line ~170) walks
  day-by-day between two aware datetimes, intersecting each day's
  09:00–17:00 (configurable) window in the configured timezone, skipping
  weekends and `HOLIDAYS_YYYYMMDD`. All metrics (`percentile`,
  `sla_compliance_pct`, etc.) are pure functions over lists of floats/bools —
  no I/O, fully unit-testable.
- **CLI entrypoint** — `main()` / `build_arg_parser()` at the bottom of the
  file: `--backfill YYYY-MM-DD` and `--dry-run` are the only flags; the
  default (no args) resumes from the SQLite cursor.
- **GitHub API usage** — `http_json()` wraps `urllib.request` (Bearer token
  auth, never logs the token; HTTP errors are re-raised with the token
  stripped from the message). `search_review_requested()` paginates the
  Search API for `team-review-requested:<org>/<team>`; `fetch_pr_detail()`
  pulls per-PR issue events, reviews, and comments; `collect_prs()` ties
  these together into `PRRecord`s via `build_pr_record()`.
- **Storage** — `Store` (SQLite): `pr_events` (append-only log),
  `prs` (current-state upsert by repo+number), `meta` (resume cursor),
  `sent_log` (digest/summary idempotency — never double-posts).
- **Output** — `render_weekly_digest()` / `render_monthly_summary()` build the
  Slack text / Markdown file; `post_slack()` posts via `chat.postMessage`.
  Weekly digest posts Fridays from 16:00 local (or on weekend catch-up);
  monthly summary writes to `data/summaries/YYYY-MM.md`.

## Conventions

- Python 3.10+, stdlib only at runtime — do not add a third-party runtime
  dependency without discussing it first; the zero-deps property is a stated
  design goal (see README "Why standard library only"), not an accident.
- Package/env manager: `uv` (`pyproject.toml` + `uv.lock`). Dev deps only:
  `ruff>=0.11`, `pytest>=7.4`.
- Formatting/lint: `ruff.toml` — line-length 100, double quotes, rule sets
  `E W F I UP B`. Run `uv run ruff format .` to auto-fix; CI only checks.
- Config is environment variables with top-of-file constant fallbacks (see
  `Config`/`load_config()`); secrets are never logged. Keep new config the
  same shape: env var, documented in the README table, checked in
  `Config.missing_required()` if mandatory.
- Tests mirror source structure 1:1 (`tests/test_*.py` per section); GitHub
  calls are tested via a mocked `urlopen`, never a real request.

## Gates (must stay green — CI mirrors this exactly, see `.github/workflows/ci.yml`)

```
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

- Lint → format-check → test, in that order (cheapest failure first).
- `pytest` includes `tests/test_secrets_scan.py`, which scans
  `pr_latency_reporter.py`, `README.md`, `pyproject.toml`, `ruff.toml`, and
  every file under `tests/` for committed secrets (Slack/GitHub token
  patterns, AWS keys, private-key headers) **and** for employer-specific
  placeholder strings that must never be reintroduced. A failing secret-scan
  test blocks the merge — do not weaken or delete it to make a change pass.
- No coverage gate is currently configured; keep the 42/42 pass rate and add
  a test for any new pure function or metric.

## Rules

- Never commit a real GitHub PAT, Slack bot token, or any other credential —
  use env vars locally (`GH_TOKEN`, `SLACK_BOT_TOKEN`, etc.); `data/` and
  `*.db` are gitignored and must stay that way.
- Never hardcode a real org name, team slug, or Slack channel ID in source,
  tests, or docs — use the neutral placeholders already in use
  (`acme-corp`, `your-team`) per `tests/test_secrets_scan.py`'s forbidden-
  placeholder check.
- Keep the file dependency-free at runtime; keep tests network-free and
  token-free (mock `urlopen`, never call the real GitHub/Slack API in a test).
- Keep all 42 tests green before proposing a change; add tests alongside any
  new metric or code path rather than after.
