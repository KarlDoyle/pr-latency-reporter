# PR Latency Reporter

Measure and report how fast pull requests get reviewed. The tool collects PR
lifecycle events from the GitHub API, computes **business-hours-aware** review
metrics, stores them append-only in SQLite, and emits a **weekly Slack digest**
and a **monthly Markdown summary** — all from a single Python file with **zero
third-party dependencies**.

The default run takes **no arguments**: it detects the datastore, resumes from
the last cursor, pulls only new/updated PRs, recomputes aggregates, and posts
the Friday Slack digest / month-end summary when due. It is idempotent and safe
to run on a cron every 30–60 minutes.

## Why standard library only

`urllib` for HTTP, `sqlite3` for storage, `zoneinfo` for timezones,
`datetime`/`statistics` for the maths. No `requests`/`pytz`/`dateutil`. This
keeps the metric logic pure and fully testable with **no network and no
tokens**, and matches the sibling tooling in this workspace.

## Requirements

- Python 3.10+
- No runtime dependencies. Dev tooling (`ruff`, `pytest`) via `uv` or `pip`.

## Install

```bash
# with uv (recommended)
uv sync --dev

# or with a plain venv
python3 -m venv .venv && source .venv/bin/activate
pip install ruff pytest
```

## Configuration

Set as environment variables, or edit the constants at the top of
`pr_latency_reporter.py`. Required secrets are checked at startup; the tool
exits with a clear list of what is missing and **never logs tokens**.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GH_TOKEN` | yes | – | GitHub PAT (`repo`, `read:org`) |
| `GH_ORG` | yes | – | GitHub org, e.g. `acme-corp` |
| `TEAM_SLUG` | yes | – | Team slug, e.g. `your-team` |
| `SLACK_BOT_TOKEN` | yes¹ | – | Slack bot token (`xoxb-…`) |
| `SLACK_CHANNEL_ID` | yes¹ | – | Slack channel **ID** (e.g. `C0123456789`), not `#name` |
| `TZ_NAME` | no | `Europe/London` | Timezone for business hours |
| `BUSINESS_START_HOUR` | no | `9` | Business-day start hour |
| `BUSINESS_END_HOUR` | no | `17` | Business-day end hour |
| `HOLIDAYS_YYYYMMDD` | no | – | Comma list, e.g. `20260101,20260317` |
| `SLA_RESPONSE_HRS` | no | `2.0` | First-response SLA target (business hours) |
| `SLA_APPROVAL_HRS` | no | `4.0` | Approval SLA target (business hours) |
| `DATA_DIR` | no | `data` | Datastore + summaries directory |
| `DEFAULT_BACKFILL_DAYS` | no | `28` | Backfill window on first run |
| `BASELINE_TTA_HRS` / `BASELINE_SLA_PCT` / `BASELINE_P95_HRS` | no | – | Optional baseline for the monthly before/after line |

¹ Slack secrets are not required when using `--dry-run`.

## Usage

```bash
# Day-to-day: no arguments. Resumes from the cursor, posts when due.
python3 pr_latency_reporter.py

# First-time backfill (run once), e.g. seed four weeks of history:
python3 pr_latency_reporter.py --backfill 2026-01-01

# Print the digest/summary instead of posting to Slack or writing files:
python3 pr_latency_reporter.py --dry-run
```

Example cron (hourly):

```cron
5 * * * * /path/to/.venv/bin/python /path/to/pr_latency_reporter.py
```

## Outputs

- **Weekly Slack digest** — posted Friday from 16:00 local (weekend catch-up if
  missed), once per ISO week. Median TTFR/TTA, SLA %, P95, and a breach list.
- **Monthly Markdown summary** — written to `data/summaries/YYYY-MM.md` at
  month end, with an optional baseline before/after line.
- **SQLite datastore** — `data/pr_latency.db`:
  - `pr_events` — append-only log of every observation.
  - `prs` — current-state projection (upserted by `repo`+`number`).
  - `meta` — the resume cursor.
  - `sent_log` — which digests/summaries were already emitted (idempotency).

## Metrics

All latencies are measured in **business hours** (09:00–17:00 Mon–Fri,
excluding weekends and configured holidays), per the project's Metrics
Reference.

| Metric | Definition |
|---|---|
| **TTFR** | Time to First Response: review-request → first comment/review |
| **TTA** | Time to Approval: review-request → first `APPROVED` review |
| **SLA %** | % of PRs within target (≤2h response / ≤4h approval, configurable) |
| **P95** | 95th-percentile TTA (the slowest 5%) |
| **Rework rate** | % of PRs with a `CHANGES_REQUESTED` review (optional) |
| **Defect escape** | % of merged PRs later linked to a defect (optional) |

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The test suite covers the pure logic with **no network and no token**:
business-hours maths (weekends/holidays/multi-day spans), each metric, the
SQLite round-trip, digest/summary rendering, scheduling, and a GitHub collect
path driven by a **mocked `urlopen`**. A secret-scan test guards against
committed tokens.

## License

Internal use only unless otherwise specified.
