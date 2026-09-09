# Atlassian Forge app scaffold (`pr-latency-reporter`)

A Jira global-page dashboard (`jira:globalPage`) presenting the same
multi-team, director/TPM-level review-latency and reviewer-seniority data as
the Slack digest and monthly Markdown summary — inside Jira, for whoever's
overseeing more than one team. Built as far as possible without Karl's own
Atlassian developer account; everything past that is called out below.

## The one thing this can't get around: where the data lives

Forge resolvers run in Atlassian's cloud sandbox — they cannot read a local
SQLite file (`data/pr_latency.db`). So this scaffold includes
`marketplace/api_server.py` (stdlib `http.server`, at the repo root of
`marketplace/`): a small read-only JSON API over the same datastore, which
the Forge resolver calls over HTTPS with an allow-listed egress permission.
**This means the Forge surface only works once that API is hosted
somewhere reachable from Atlassian's cloud** — see manual steps below.

## What's built here

- **`manifest.yml`** — one `jira:globalPage` module, a resolver function,
  and an `external.fetch.backend` egress allow-list (placeholder host —
  fill in once `api_server.py` is hosted).
- **`src/index.js`** — the Forge resolver (`@forge/resolver`): three
  functions (`getSummary`, `getTeams`, `getSeniority`) that proxy to
  `api_server.py`'s equivalent endpoints, reading the base URL/token from
  Forge environment variables (never hardcoded).
- **`static/dashboard/`** — a small React (Custom UI) app: a by-team table
  and a reviewer-seniority-by-tier table, calling the resolver via
  `@forge/bridge`'s `invoke()`. Deliberately mirrors the same
  awareness-not-verdict framing as `render_monthly_summary()` in
  `pr_latency_reporter.py`, so the two surfaces never disagree on tone.
- **`package.json`** files (root + `static/dashboard/`) with the standard
  Forge Custom UI dependency set (`@forge/api`, `@forge/resolver`,
  `@forge/bridge`, React via `react-scripts`).

## What could NOT be built without a live setup

- **No `forge deploy`/`forge install` was run** — no Atlassian developer
  account is available in this environment, and this is exactly the kind of
  irreversible-ish external action (registers a real app ID, deploys to
  Atlassian's infra) this repo's rules require Karl's explicit go on.
- **No `npm install`** was run in either `package.json` — no network access
  for npm here. Versions pinned are current-as-of-writing; **check them
  against `https://developer.atlassian.com/platform/forge/` before
  installing**, since Forge's SDK versions move.
- **The API host in `manifest.yml`/environment variables is a placeholder**
  — nothing here can provision hosting for `api_server.py`.

## Manual steps Karl still needs to do

1. **Host `marketplace/api_server.py`** somewhere with a public HTTPS URL
   and set `API_AUTH_TOKEN` to a long random value there.
2. **Get a Forge/Atlassian developer account** if not already set up:
   `https://developer.atlassian.com/console/`, then install the Forge CLI
   (`npm install -g @forge/cli`) and `forge login`.
3. **Register the app**: `forge register` from `marketplace/forge-app/` —
   this assigns the real `app.id`; paste it into `manifest.yml` replacing
   the placeholder.
4. **Install dependencies and build the dashboard**:
   `npm install` (root), then
   `cd static/dashboard && npm install && npm run build`.
5. **Set the two Forge environment variables** (per environment —
   `development`/`staging`/`production` if used):
   `forge variables set --encrypt PR_LATENCY_API_TOKEN <value>` and
   `forge variables set PR_LATENCY_API_BASE_URL <value>`.
6. **Fill in the real API host** in `manifest.yml`'s
   `permissions.external.fetch.backend` (must match the host from step 1
   exactly — Forge egress is allow-listed, not wildcard).
7. **`forge deploy`** then **`forge install`** into the Jira site(s) that
   should see this.
8. **If publishing to the Atlassian Marketplace** (rather than a private
   install on Karl's own site): that's a separate submission with its own
   listing requirements (pricing, support, privacy policy, security
   self-assessment) — same open question as the GitHub App side: confirm
   with Karl whether the goal is an actual public Marketplace listing or
   just "runs inside my own Jira" before doing that additional work.
