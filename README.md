# PR Latency Reporter (`pr-latency-reporter`)

Measure and report how fast pull requests get reviewed, across one or more
teams. The tool collects PR lifecycle events from the GitHub API, computes
**business-hours-aware** review metrics with real per-reviewer attribution,
stores them append-only in SQLite, and emits a **weekly Slack digest** and a
**monthly Markdown summary** — all from a single Python file with **zero
third-party dependencies**.

![tests](https://img.shields.io/badge/tests-66-slategray?style=flat-square)
![deps](https://img.shields.io/badge/runtime--deps-zero-slategray?style=flat-square)
![python](https://img.shields.io/badge/python-3.10%2B-slategray?style=flat-square)
![licence](https://img.shields.io/badge/licence-UNLICENSED-slategray?style=flat-square)

**Framing:** this is a director/TPM-level awareness signal across teams, not a
single-team performance verdict. Team-level managers tend to get defensive
about this kind of data being visible on their specific team; senior
leadership found it useful as validation/awareness across several. The
weekly digest and monthly summary are written for the latter.

The default run takes **no arguments**: it detects the datastore, resumes from
the last cursor, pulls only new/updated PRs across every configured team,
recomputes aggregates, and posts the Friday Slack digest / month-end summary
when due. It is idempotent and safe to run on a cron every 30–60 minutes.

## Fixes (August 2026 productization pass)

An earlier technical audit found two real defects in the original build.
Both are fixed, not papered over:

1. **Reviewer attribution was never captured.** `review.user.login` was
   available from the GitHub API but never read or stored — `PRRecord` only
   ever carried the PR *author*, so there was no way to measure "are seniors
   leading on review" at all. Fixed: every review is now captured as a
   `ReviewEvent` (repo, number, reviewer, state, response time), `PRRecord`
   gained `first_responder_login`/`first_approver_login`, and
   `reviewer_aggregate()`/`seniority_rollup()` turn a `REVIEWER_SENIORITY`
   login→tier map into real per-tier review-volume, speed, and
   first-approval-share numbers — the monthly summary's "Reviewer leadership
   by seniority" section.
2. **`defect_flag` was hardcoded to `0`.** The monthly summary rendered a
   real-looking "0.0%" defect-escape-rate with no signal behind it. Fixed,
   honestly: it now searches GitHub for PRs reverted via GitHub's own
   "Revert" flow (`This reverts commit <sha>.` in the body) referencing a
   collected PR's merge commit, within `DEFECT_LOOKBACK_DAYS`. This is a
   **real signal, explicitly a lower bound** — it only catches formal
   GitHub-Revert PRs, not a silent hotfix that doesn't reference the
   original commit. Labelled as such everywhere it's shown
   ("revert-based, lower bound"), never presented as a complete quality
   measure.

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
| `TEAM_SLUG` | yes² | – | Single team slug, e.g. `your-team` (legacy; kept for back-compat) |
| `TEAM_SLUGS` | yes² | – | Comma list for multi-team observation, e.g. `platform-team,checkout-team,mobile-team` — wins over `TEAM_SLUG` when set |
| `REVIEWER_SENIORITY` | no | – | `login:tier,login:tier` map (e.g. `alice:senior,bob:mid`), feeds the "are seniors leading" rollup. Unmapped reviewers show as `unspecified` |
| `DEFECT_LOOKBACK_DAYS` | no | `14` | Days after merge a GitHub "Revert" PR still counts as a defect escape |
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
² Either `TEAM_SLUG` or `TEAM_SLUGS` must be set — at least one team.

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
| **Defect escape** | % of merged PRs later reverted via GitHub's "Revert" flow within `DEFECT_LOOKBACK_DAYS` — a **real, revert-based signal, and an explicit lower bound**, not a complete quality measure. See "Fixes" above |
| **Reviewer leadership by seniority** | Per-tier (from `REVIEWER_SENIORITY`) share of review volume, share of first approvals, and median response time — the measurable version of "are seniors leading on review" |

## Multi-team observation

Set `TEAM_SLUGS` (comma list) instead of the single `TEAM_SLUG` to observe
several teams at once. Collection runs one GitHub Search query per team
(GitHub's `team-review-requested` filter only accepts one team), de-duplicates
PRs requested from more than one configured team, and tags each `PRRecord`
with which team's request it was anchored on (`team_slug`; PRs where no
configured team was ever requested fall into an `"unassigned"` bucket rather
than being dropped). Both the weekly digest and monthly summary add a
per-team breakdown once more than one team is configured — listed in a
stable slug order, never ranked "best" to "worst".

## Dual marketplace

Same pattern as the sibling `pr-watch` tool: `marketplace/github-app/` and
`marketplace/forge-app/` scaffold a GitHub App (webhook-triggered collection)
and an Atlassian Forge app (a Jira dashboard for the same multi-team data),
built as far as possible without a live registered App on Karl's own
accounts. See `marketplace/README.md` for status and the manual steps that
remain.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The test suite (66 tests) covers the pure logic with **no network and no
token**: business-hours maths (weekends/holidays/multi-day spans), each
metric, reviewer attribution and seniority rollups from a realistic mocked
GitHub payload, the revert-based defect-escape signal, multi-team
aggregation, the SQLite round-trip (including the `pr_reviews` table),
digest/summary rendering, scheduling, and a GitHub collect path driven by a
**mocked `urlopen`**. A secret-scan test guards against committed tokens.
`marketplace/` has its own, separate test suite
(`uv run pytest marketplace/github-app/tests/ marketplace/tests/ -q`) —
kept out of this gate since it covers optional wrapper components, not the
core tool's contract.

## Licence

Unlicensed (all rights reserved) — personal tooling, not published for reuse.
