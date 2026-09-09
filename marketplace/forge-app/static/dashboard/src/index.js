import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@forge/bridge";

/**
 * Multi-team PR review-latency dashboard.
 *
 * Director/TPM-level awareness view — a team breakdown listed in a stable
 * order (never ranked "best"/"worst") plus the reviewer-seniority section
 * that measures "are seniors leading on review", not asserts it. Mirrors
 * the framing in the monthly Markdown summary
 * (`render_monthly_summary` in pr_latency_reporter.py) so the Jira surface
 * and the Slack/Markdown outputs never disagree on tone.
 */
function App() {
  const [window_, setWindow] = useState("week");
  const [teams, setTeams] = useState(null);
  const [seniority, setSeniority] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    Promise.all([
      invoke("getTeams", { window: window_ }),
      invoke("getSeniority", { window: window_ }),
    ])
      .then(([teamsResult, seniorityResult]) => {
        if (cancelled) return;
        if (teamsResult.error) setError(teamsResult.error);
        else setTeams(teamsResult.teams || []);
        if (!seniorityResult.error) setSeniority(seniorityResult.seniority || []);
      })
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, [window_]);

  return (
    <div style={{ fontFamily: "sans-serif", padding: "1rem" }}>
      <h2>PR Review Latency — Multi-Team Awareness</h2>
      <p style={{ color: "#555" }}>
        Observation across teams for whoever is overseeing more than one —
        not a single-team performance verdict.
      </p>
      <label>
        Window:{" "}
        <select value={window_} onChange={(e) => setWindow(e.target.value)}>
          <option value="week">This week</option>
          <option value="month">This month</option>
        </select>
      </label>

      {error && <p style={{ color: "#b00020" }}>Error: {error}</p>}

      <h3>By team</h3>
      {!teams ? (
        <p>Loading…</p>
      ) : teams.length === 0 ? (
        <p>No data for this window yet.</p>
      ) : (
        <table cellPadding={6} style={{ borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th align="left">Team</th>
              <th align="right">PRs</th>
              <th align="right">Median TTA</th>
              <th align="right">SLA %</th>
              <th align="right">Defect % (lower bound)</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((t) => (
              <tr key={t.team_slug}>
                <td>{t.team_slug}</td>
                <td align="right">{t.total}</td>
                <td align="right">{t.median_tta_bh.toFixed(1)}h</td>
                <td align="right">{t.sla_approval_pct.toFixed(0)}%</td>
                <td align="right">{t.defect_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Reviewer leadership by seniority</h3>
      {!seniority ? (
        <p>Loading…</p>
      ) : seniority.length === 0 ? (
        <p>
          No seniority data — set <code>REVIEWER_SENIORITY</code> on the
          collector to populate tiers.
        </p>
      ) : (
        <table cellPadding={6} style={{ borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th align="left">Tier</th>
              <th align="right">Reviewers</th>
              <th align="right">Reviews</th>
              <th align="right">Share of reviews</th>
              <th align="right">Share of first approvals</th>
              <th align="right">Median response</th>
            </tr>
          </thead>
          <tbody>
            {seniority.map((s) => (
              <tr key={s.tier}>
                <td>{s.tier}</td>
                <td align="right">{s.reviewer_count}</td>
                <td align="right">{s.review_count}</td>
                <td align="right">{s.share_of_reviews_pct.toFixed(0)}%</td>
                <td align="right">{s.share_of_first_approvals_pct.toFixed(0)}%</td>
                <td align="right">{s.median_response_bh.toFixed(1)}h</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const root = createRoot(document.getElementById("root"));
root.render(<App />);
