# GitHub App scaffold (`pr-latency-reporter`)

Wraps the existing polling collector (`search_review_requested` on a cron)
in a **GitHub App + webhook** so collection is push-triggered instead of
polled. Built as far as possible without a live, registered App — everything
below that needs Karl's own GitHub account/org is called out explicitly.

## What's built here

- **`app.manifest.json`** — a [GitHub App Manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
  requesting the minimum scopes the tool already uses read-only
  (`pull_requests`, `issues`, `contents`, `members`, `metadata`), subscribed
  to `pull_request`, `pull_request_review`, `issue_comment`.
- **`webhook_handler.py`** — stdlib-only (`http.server`, `hmac`) receiver.
  Verifies `X-Hub-Signature-256`, then re-collects and stores the one PR an
  event refers to via the **same** `build_pr_record` / `build_review_events`
  path the poller uses (`pr_latency_reporter.fetch_pr_detail` +
  `build_pr_record` + `build_review_events`), so both collection paths
  produce identical `PRRecord`/`ReviewEvent` shapes. This is what fixes
  reviewer attribution flows through here too — nothing App-specific
  duplicates that logic.
- **`tests/test_webhook_handler.py`** — signature verification, event
  parsing, and the store round-trip, run with a mocked `fetch_pr_detail`
  (no network, no live App). Run with:
  `uv run pytest marketplace/github-app/tests/ -q` (kept out of the main
  `tests/` gate deliberately — this is optional wrapper code, not the core
  tool's contract).

## Does polling still make sense?

Partially replaced, not fully retired. The webhook path only fires for
_activity_ (a review submitted, a comment posted) — it won't notice a PR
that's been silently waiting with **no** activity, which is exactly the
"nobody's looked at this in three days" case the SLA-breach metric exists to
catch. Recommendation: keep the existing cron poll (e.g. hourly rather than
the current 5-minute-friendly cadence, since the webhook now handles the
fast path) as a safety net for PRs with no webhook-triggering activity, and
let the webhook handler keep same-day PRs fresh. Both write through the same
`Store`, so this is safe to run side by side.

## Manual steps Karl still needs to do (cannot be done from this repo)

1. **Host `webhook_handler.py` somewhere with a public HTTPS URL.** GitHub
   needs to reach it. A small VM, a container behind a reverse proxy, or a
   tunnel (e.g. Cloudflare Tunnel) all work — nothing here provisions that.
2. **Fill in the three placeholder URLs in `app.manifest.json`** (`url`,
   `hook_attributes.url`, `redirect_url`/`callback_urls`) with that hosted
   address.
3. **Register the App from the manifest**: go to
   `https://github.com/settings/apps/new?state=<random>` (or your org's
   equivalent `https://github.com/organizations/<org>/settings/apps/new`),
   POST the manifest per GitHub's manifest flow, or paste it into the app
   creation UI. GitHub will hand back the App ID, a generated webhook
   secret, and a private key — capture all three.
4. **Set `GH_WEBHOOK_SECRET`** (from step 3) as an env var wherever
   `webhook_handler.py` runs.
5. **Install the App** on the org/repos you want observed
   (`https://github.com/settings/apps/pr-latency-reporter/installations`).
6. **If publishing to GitHub Marketplace** (rather than a private/internal
   App): GitHub Marketplace listing requires the App to support the
   Marketplace billing webhook (`marketplace_purchase` events) even for a
   free listing, plus a listing description, screenshots, and support
   contact — none of that is built here, since it's a business/legal step,
   not a code one. Flag to Karl before doing that work: is Marketplace
   *listing* actually the goal, or just "GitHub App architecture" for
   Karl's own org? The manifest/webhook code above works either way; only
   the public listing needs the extra Marketplace-specific webhook + forms.
