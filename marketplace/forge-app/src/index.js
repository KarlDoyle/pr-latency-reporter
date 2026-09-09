/**
 * Forge resolver for the pr-latency-reporter dashboard (jira:globalPage).
 *
 * Runs in Atlassian's cloud sandbox, which cannot read the tool's local
 * SQLite datastore — it proxies to marketplace/api_server.py (a small
 * stdlib HTTP JSON API Karl hosts himself) over the egress backend declared
 * in manifest.yml's `permissions.external.fetch.backend`.
 *
 * PR_LATENCY_API_BASE_URL / PR_LATENCY_API_TOKEN come from Forge
 * environment variables (see manifest.yml + README.md) — never hardcoded,
 * never committed.
 */

import Resolver from "@forge/resolver";

const resolver = new Resolver();

async function callApi(path, params) {
  const baseUrl = process.env.PR_LATENCY_API_BASE_URL;
  const token = process.env.PR_LATENCY_API_TOKEN;
  if (!baseUrl || !token) {
    return {
      error:
        "PR_LATENCY_API_BASE_URL / PR_LATENCY_API_TOKEN not configured — " +
        "see marketplace/forge-app/README.md.",
    };
  }
  const qs = new URLSearchParams(params).toString();
  const response = await fetch(`${baseUrl}${path}?${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    return { error: `API returned HTTP ${response.status}` };
  }
  return response.json();
}

resolver.define("getSummary", async (req) => {
  const { window = "week", team } = req.payload || {};
  return callApi("/api/v1/summary", team ? { window, team } : { window });
});

resolver.define("getTeams", async (req) => {
  const { window = "week" } = req.payload || {};
  return callApi("/api/v1/teams", { window });
});

resolver.define("getSeniority", async (req) => {
  const { window = "week" } = req.payload || {};
  return callApi("/api/v1/seniority", { window });
});

export const handler = resolver.getDefinitions();
