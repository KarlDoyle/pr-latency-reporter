# Dual marketplace scaffolding

Same pattern as the sibling `pr-watch` tool, per Karl's direction: fix the
underlying defects first (see the root README's "Fixes" section — reviewer
attribution and the defect-escape metric), then wrap the now-honest data in
both a **GitHub App** and an **Atlassian Forge app**, reframed for
director/TPM-level, multi-team observation rather than a single-team
performance callout.

- **`github-app/`** — a GitHub App manifest + stdlib webhook handler,
  replacing (well: supplementing — see its README) the current polling
  loop with push-triggered collection.
- **`forge-app/`** — a Jira `jira:globalPage` dashboard surfacing the same
  multi-team + reviewer-seniority data inside Jira.
- **`api_server.py`** — the one piece both marketplace surfaces share: a
  small stdlib read-only JSON API over the existing SQLite datastore. The
  GitHub App doesn't strictly need it (it can keep using Slack/Markdown),
  but the Forge app has no other way to reach the data, since Forge
  resolvers run in Atlassian's sandbox with no filesystem access to
  Karl's machine.

## Status: code scaffolded, nothing registered or deployed

Nothing in this directory has been registered with GitHub, registered with
Atlassian, deployed, or hosted. Both READMEs below list the exact manual
steps — account setup, `forge register`/`forge deploy`, GitHub App
registration, and hosting `api_server.py` somewhere with a public HTTPS URL
— that only Karl can do, since they require his own developer accounts and
a decision about where to host the API. None of it was run in this session.

## Open question worth Karl's explicit call before going further

Both README files (`github-app/README.md` point 6, `forge-app/README.md`
point 8) flag the same fork: is the goal an actual **public Marketplace
listing** (GitHub Marketplace / Atlassian Marketplace — extra listing
requirements, billing webhooks, support commitments, and a public-facing
posture that sits oddly next to this repo's `licence: UNLICENSED —
personal tooling, not published for reuse` line in `AGENTS.md`), or
**"GitHub App / Forge app architecture for Karl's own use"** (private
install, no listing, no distribution commitments)? The manifest/resolver
code built here works identically either way — only the "submit for
Marketplace review" step differs, and that's a distribution decision, not
a coding one.

## Running the pieces that exist today

```bash
# The shared API (needed by the Forge dashboard; optional for the GitHub App)
API_AUTH_TOKEN=some-long-random-value uv run python marketplace/api_server.py

# The GitHub App webhook receiver (needs a registered App + secret; see its README)
GH_WEBHOOK_SECRET=... GH_TOKEN=... GH_ORG=... TEAM_SLUGS=... \
    uv run python marketplace/github-app/webhook_handler.py

# Tests for both (kept out of the main `uv run pytest` gate — see below)
uv run pytest marketplace/github-app/tests/ marketplace/tests/ -q
```

Kept out of the root `tests/` gate deliberately: these are optional
marketplace wrapper components, not the core tool's contract. The root
`AGENTS.md` gate (`ruff check . && ruff format --check . && pytest`) stays
the source of truth for "is the core tool correct"; run the commands above
separately when working on the marketplace layer.
