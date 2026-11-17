/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Renders the monthly per-system statement (the CP's
 * `/ai-systems/{id}/statement` payload) as one self-contained HTML file —
 * inline CSS, no external assets, printable. The artifact a system owner
 * forwards to their budget review.
 *
 * Honesty rules carried through from the platform: health is stamped with
 * when it was computed (a statement generated mid-August carries today's
 * verdict, not a reconstruction); cost is per PROCESSED outcome (runs that
 * completed in a declared outcome counting toward the baseline) with the
 * spend that produced nothing and the no-work overhead beside it, never
 * averaged in; and a never-computed health renders "not computed", never a
 * cheerful default.
 */

import type { CostPerRunSignal, TerminalStateSignal } from './aiSystemTypes';

const esc = (v: unknown): string =>
    String(v ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');

const usd = (v: number | null | undefined, dp = 2): string =>
    v === null || v === undefined ? '—' : `$${v.toFixed(dp)}`;

const num = (v: number | null | undefined): string =>
    v === null || v === undefined ? '—' : v.toLocaleString();

const pct = (v: number | null | undefined): string =>
    v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;

export function statementHtml(st: any): string {
    const sys = st.system || {};
    const cost = st.cost || {};
    const totals = cost.totals || {};
    const prev = cost.previous?.totals;
    const savings = st.savings || {};
    const runs = st.runs || {};
    const runCost = (runs.cost || {}) as Partial<CostPerRunSignal>;
    const states = (runs.terminal_states || {}) as Partial<TerminalStateSignal>;
    const health = st.health;

    const delta = prev && prev.cost > 0
        ? ` (${totals.cost >= prev.cost ? '+' : ''}${(((totals.cost - prev.cost) / prev.cost) * 100).toFixed(1)}% vs previous month)`
        : '';

    const modelRows = (cost.by_model || []).slice(0, 10).map((m: any) =>
        `<tr><td>${esc(m.key)}</td><td class="r">${num(m.requests)}</td><td class="r">${num(m.tokens)}</td><td class="r">${usd(m.cost)}</td></tr>`
    ).join('');

    const userRows = (cost.by_user || []).slice(0, 10).map((u: any) =>
        `<tr><td>${esc(u.name)}</td><td class="r">${num(u.requests)}</td><td class="r">${usd(u.cost)}</td></tr>`
    ).join('');

    const stateRows = (states.by_state || []).map((s) =>
        `<tr><td>${esc(s.state)}</td><td class="r">${num(s.count)}</td><td class="r">${pct(s.share)}</td></tr>`
    ).join('');

    return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>${esc(sys.display_name || sys.name)} — ${esc(st.month)} statement</title>
<style>
  body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; color: #0f172a;
         max-width: 860px; margin: 2rem auto; padding: 0 1.5rem; }
  h1 { font-size: 1.5rem; margin-bottom: .25rem; }
  h2 { font-size: 1.05rem; margin-top: 1.75rem; border-bottom: 2px solid #e2e8f0;
       padding-bottom: .3rem; }
  .meta { color: #64748b; font-size: .85rem; }
  .cards { display: flex; gap: 1rem; flex-wrap: wrap; margin-top: 1rem; }
  .card { border: 1px solid #e2e8f0; border-radius: 8px; padding: .75rem 1rem; min-width: 160px; }
  .card .k { font-size: .72rem; color: #64748b; text-transform: uppercase; }
  .card .v { font-size: 1.35rem; font-weight: 700; }
  table { border-collapse: collapse; width: 100%; margin-top: .5rem; font-size: .88rem; }
  th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #eef2f7; }
  th { color: #64748b; font-weight: 600; font-size: .75rem; text-transform: uppercase; }
  td.r, th.r { text-align: right; }
  .foot { margin-top: 2rem; color: #94a3b8; font-size: .75rem; }
  .note { color: #64748b; font-size: .8rem; }
</style></head><body>
<h1>${esc(sys.display_name || sys.name)}</h1>
<p class="meta">AI System statement · ${esc(st.month)} · kind: ${esc(sys.system_kind || '—')} ·
owner: ${esc(sys.owner || 'unassigned')} · lifecycle: ${esc(sys.lifecycle_stage || 'not set')} ·
path: ${esc(sys.hierarchy_path || '')}</p>

<div class="cards">
  <div class="card"><div class="k">Spend</div><div class="v">${usd(totals.cost)}</div>
    <div class="note">${num(totals.requests)} requests${esc(delta)}</div></div>
  <div class="card"><div class="k">Cost / processed outcome</div>
    <div class="v">${usd(runCost.cost_per_processed_outcome, 4)}</div>
    <div class="note">${num(runCost.processed_runs)} processed outcomes</div></div>
  <div class="card"><div class="k">Cost avoided</div>
    <div class="v">${usd(savings.total_avoided_usd)}</div>
    <div class="note">cache (est.) + provider prompt cache (measured)</div></div>
  <div class="card"><div class="k">Health</div>
    <div class="v">${esc(health?.state ?? 'not computed')}</div>
    <div class="note">${health?.computed_at ? 'as of ' + esc(String(health.computed_at).slice(0, 10)) : 'no verdict on record'}</div></div>
</div>

<h2>Task outcomes</h2>
<p class="note">Success rate ${pct(states.success_rate)} (completed in a declared outcome that counts,
over successes, deviations and failures — policy blocks are failures) · spent, nothing produced
${usd(runCost.wasted_cost_usd)} · no-work overhead ${usd(runCost.no_work_cost_usd)}
(${num(runCost.no_work_runs)} runs) · run coverage ${pct(runs.observed_pct)}</p>
<table><thead><tr><th>Execution</th><th class="r">Runs</th><th class="r">Share</th></tr></thead>
<tbody>${stateRows || '<tr><td colspan="3">No runs recorded this month</td></tr>'}</tbody></table>

<h2>Cost by model</h2>
<table><thead><tr><th>Model</th><th class="r">Requests</th><th class="r">Tokens</th><th class="r">Cost</th></tr></thead>
<tbody>${modelRows || '<tr><td colspan="4">No model spend this month</td></tr>'}</tbody></table>

<h2>Cost by user</h2>
<table><thead><tr><th>User</th><th class="r">Requests</th><th class="r">Cost</th></tr></thead>
<tbody>${userRows || '<tr><td colspan="3">No user-attributed spend this month</td></tr>'}</tbody></table>

<p class="foot">Generated ${esc(String(st.generated_at || '').slice(0, 19).replace('T', ' '))} UTC by
Brutor Mission Control from the gateway's per-request ledger. Health is the current materialised
verdict, stamped with its computation time — not a reconstruction of the statement month.</p>
</body></html>`;
}
