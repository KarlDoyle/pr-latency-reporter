# PR Latency Reporter

A small, self-hosted Python tool that collects pull request review activity from GitHub and creates local weekly and monthly reports. It is designed for personal or team use, with data kept in a local SQLite database.

**Lean by design:** one Python script, no runtime packages, no hosted service, local files by default. Slack is optional and only used with `--post-slack`.

## Quick start

Requirements: Python 3.11 or newer and a GitHub token with access to the repositories and organization teams you want to measure.

```bash
git clone https://github.com/KarlDoyle/pr-latency-reporter.git
cd pr-latency-reporter
```

You can also download and unzip the repository from GitHub. The tool has no
runtime packages; the development tools are only needed to run its checks.

Set the required GitHub configuration, then run the script:

```bash
export GH_TOKEN="your-token"
export GH_ORG="your-organization"
export TEAM_SLUG="your-team"
python3 pr_latency_reporter.py
```

The first run collects the previous 28 days. Later runs resume from a local cursor. Reports and the SQLite database are saved under `data/`. Use `--dry-run` to preview due reports without writing report files or posting to Slack; it still collects data and updates the local database.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GH_TOKEN` | Yes | — | GitHub token; use the narrowest access suitable for your repositories and teams. |
| `GH_ORG` | Yes | — | GitHub organization. |
| `TEAM_SLUG` or `TEAM_SLUGS` | Yes | — | One team slug, or a comma-separated list. |
| `DATA_DIR` | No | `data` | Local database and report directory. |
| `TZ_NAME` | No | `Europe/London` | Time zone used for business hours and report scheduling. |
| `SLACK_BOT_TOKEN` | Only with `--post-slack` | — | Slack bot token. |
| `SLACK_CHANNEL_ID` | Only with `--post-slack` | — | Destination channel ID. |

Additional settings for business hours, holidays, SLAs, reviewer tiers, and the revert-based defect signal are documented near the top of `pr_latency_reporter.py`.

## Reports and metrics

- Weekly Markdown report: `data/summaries/weekly-YYYY-Www.md` (written on Friday after 16:00 local time, or on weekend catch-up).
- Monthly Markdown summary: `data/summaries/YYYY-MM.md` (written after month end).
- Local SQLite store: `data/pr_latency.db`.
- `python3 pr_latency_reporter.py --post-slack` also sends the weekly report to Slack. Monthly summaries stay local.
- `python3 pr_latency_reporter.py --backfill YYYY-MM-DD` sets a one-time collection start date.

Latencies use configured business hours (weekdays, 09:00–17:00 by default). The revert-based defect metric only detects GitHub Revert PRs and is a lower-bound signal. Metrics describe observed review activity; they are not a complete measure of quality or individual performance. Treat reports as potentially sensitive because they contain repository and reviewer information.

## Development and checks

Runtime uses only Python's standard library. Development tools are `ruff` and `pytest`:

```bash
uv sync --locked --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pytest marketplace/github-app/tests/ marketplace/tests/ -q
```

CI runs these checks on Python 3.11 and 3.14. Tests use mocks and do not contact GitHub or Slack.

## Optional marketplace scaffolds

The `marketplace/` directory contains experimental GitHub App and Atlassian Forge scaffolds. They are not needed to use the local CLI and are not registered, hosted, or part of this tool's supported download workflow.

## License

MIT. See [LICENSE](LICENSE).
