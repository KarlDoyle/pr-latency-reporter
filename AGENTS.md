# Repository guidance

PR Latency Reporter is a small, local-first Python CLI for measuring GitHub
pull request review latency. Keep its first-release workflow simple: standard
library at runtime, local SQLite and Markdown output by default, and Slack only
when the user explicitly runs `--post-slack`.

## Development checks

```bash
uv sync --locked --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests must use mocks and must not contact GitHub or Slack. Keep the secret scan
covering repository text sources, configuration, documentation, and tests.

## Safety and scope

- Never commit credentials, local databases, or generated reports.
- Keep API tokens in environment variables and do not include them in logs or
  errors.
- Do not add runtime dependencies, hosted services, package publishing, or
  hosted integrations to the supported download workflow without discussing
  the scope first.
- Keep the README accurate and concise, including setup, local data handling,
  report behavior, and known metric limitations.
