/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** CSV writer quoting + the statement HTML's honesty rules. */

import { describe, it, expect } from 'vitest';
import { toCsv } from '../csvExport';
import { statementHtml } from '../statementHtml';

describe('toCsv', () => {
    it('quotes commas, quotes and newlines per RFC 4180', () => {
        const csv = toCsv(['a', 'b'], [['x,y', 'he said "hi"'], ['line\nbreak', 7]]);
        expect(csv).toContain('"x,y"');
        expect(csv).toContain('"he said ""hi"""');
        expect(csv).toContain('"line\nbreak"');
        expect(csv.endsWith('\r\n')).toBe(true);
    });

    it('renders null/undefined as empty cells', () => {
        expect(toCsv(['a', 'b'], [[null, undefined]])).toContain('\r\n,\r\n');
    });
});

const statement = (over: Record<string, unknown> = {}) => ({
    month: '2026-07',
    generated_at: '2026-08-04T10:00:00Z',
    system: {
        name: 'copilot', display_name: 'Copilot', system_kind: 'agent',
        owner: 'RevOps', lifecycle_stage: 'active', hierarchy_path: 'acme/copilot',
    },
    cost: {
        totals: { cost: 245.57, requests: 78964, tokens: 1 },
        previous: { totals: { cost: 193.75 } },
        by_model: [{ key: 'gpt-5.2', cost: 87.25, tokens: 5, requests: 3 }],
        by_user: [{ name: 'priya', cost: 17.42, requests: 4 }],
    },
    savings: { total_avoided_usd: 83.59 },
    runs: {
        observed_pct: 0.95,
        cost: {
            cost_per_processed_outcome: 0.0065, processed_runs: 10819, wasted_cost_usd: 4.27,
            no_work_runs: 412, no_work_cost_usd: 0.12,
        },
        terminal_states: {
            success_rate: 0.93,
            by_state: [{ state: 'completed', count: 10819, share: 0.93 }],
        },
    },
    health: { state: 'degraded', score: 0.6, computed_at: '2026-08-04T09:00:00Z' },
    ...over,
});

describe('statementHtml', () => {
    it('carries the headline numbers and identity', () => {
        const html = statementHtml(statement());
        expect(html).toContain('Copilot');
        expect(html).toContain('$245.57');
        expect(html).toContain('+26.7% vs previous month');
        expect(html).toContain('$0.0065');
        expect(html).toContain('$83.59');
        expect(html).toContain('degraded');
        const asOf = '2026-08-04';
        expect(html).toContain(`as of ${asOf}`);
    });

    it('reports cost per processed outcome, the spend that produced nothing and the no-work overhead apart', () => {
        const html = statementHtml(statement());
        expect(html).toContain('Cost / processed outcome');
        expect(html).toContain('10,819 processed outcomes');
        expect(html).toContain('spent, nothing produced\n$4.27');
        expect(html).toContain('no-work overhead $0.12');
        expect(html).toContain('(412 runs)');
        // Policy blocks are failures — no separate "blocked" line.
        expect(html).not.toMatch(/blocked by policy/i);
        expect(html).not.toMatch(/completed task/);
    });

    it('renders never-computed health honestly', () => {
        const html = statementHtml(statement({ health: null }));
        expect(html).toContain('not computed');
        expect(html).toContain('no verdict on record');
    });

    it('escapes HTML in names — a system name cannot inject markup', () => {
        const html = statementHtml(statement({
            system: { name: 'x', display_name: '<script>alert(1)</script>', hierarchy_path: '' },
        }));
        expect(html).not.toContain('<script>alert');
        expect(html).toContain('&lt;script&gt;');
    });

    it('is self-contained — no external asset references', () => {
        const html = statementHtml(statement());
        expect(html).not.toMatch(/src=["']http/);
        expect(html).not.toMatch(/href=["']http/);
    });
});
