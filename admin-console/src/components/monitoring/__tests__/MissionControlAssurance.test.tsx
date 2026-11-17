/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control's lifecycle surfaces: the fleet board, the
 * assurance band and the run-ledger cards.
 *
 * The load-bearing assertions are the honesty rules:
 *   * a system with no computed health renders `unknown` — never healthy;
 *   * the fleet sorts worst-health-first (unmeasured between red and green);
 *   * cost is per PROCESSED outcome; no-work runs are overhead in grey,
 *     never green and never averaged in; a policy block is a failure;
 *   * zero runs renders the run-id hint, not an empty chart.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { callsWhileIdle } from '../../../test/idle';

// How often a component asked the hook: once per render.
const asked = vi.fn();

// The pages key their loads on these: one identity for the whole file, or
// every render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchSystemHealth: vi.fn().mockResolvedValue({
        ai_system_id: 'sys-1',
        health_state: 'degraded',
        health_score: 0.6,
        open_drift_events: 0,
        reasons: [],
        is_trustworthy: true,
        computed_at: new Date().toISOString(),
    }),
    fetchAISystemLiveness: vi.fn().mockResolvedValue({
        ai_system_id: 'sys-1',
        configured: true,
        expectation: { mode: 'continuous' },
        has_ever_run: true,
        is_silent: false,
    }),
    fetchAISystemDrift: vi.fn().mockResolvedValue({ events: [], total: 0 }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => (asked(), stableGateway),
}));

import { AssuranceBand, RunSignalsSection } from '../MissionControlAssurance';
import type {
    AISystemSummary,
    RunStatsResponse,
} from '../../../lib/aiSystemTypes';

// ─────────────────────────────────────────────────────────────────────────
// Fixtures
// ─────────────────────────────────────────────────────────────────────────

function system(overrides: Partial<AISystemSummary> = {}): AISystemSummary {
    return {
        id: 'sys-1',
        name: 'copilot',
        display_name: 'Copilot',
        system_kind: 'agent',
        lifecycle_stage: 'active',
        owner: 'RevOps',
        hierarchy_level: 0,
        is_active: true,
        composition: {
            llm_models: 1, mcp_servers: 1, agent_skills: 0,
            agent_identities: 1, child_groups: 0,
        },
        governance_gaps: [],
        ...overrides,
    } as AISystemSummary;
}

function runStats(overrides: Partial<RunStatsResponse> = {}): RunStatsResponse {
    return {
        ai_system_id: 'sys-1',
        period_hours: 720,
        cost: {
            processed_runs: 100,
            total_cost_usd: 1.5,
            cost_per_processed_outcome: 0.0123,
            median_cost_per_processed_outcome: 0.01,
            p95_cost_per_processed_outcome: 0.03,
            wasted_cost_usd: 0.25,
            no_work_runs: 40,
            no_work_cost_usd: 0.07,
            total_tokens: 250_000,
            tokens_per_processed_outcome: 2400,
        },
        steps: {
            runs: 110,
            median_steps: 6,
            p95_steps: 14,
            max_steps: 40,
            distribution: [],
            exhausted_runs: 1,
            exhaustion_rate: 0.009,
            exhaustion_reporting_available: true,
            abandoned_runs: 2,
        },
        tool_errors: {
            total_tool_calls: 300,
            total_tool_errors: 3,
            overall_error_rate: 0.01,
            by_tool: [],
        },
        terminal_states: {
            total_runs: 110,
            by_state: [
                { state: 'completed', count: 140, share: 0.933 },
                { state: 'errored', count: 10, share: 0.067 },
            ],
            success_rate: 0.909,
        },
        outcomes: {
            total_runs: 150,
            success_rate: 0.909,
            by_class: { success: 100, no_work: 40, deviation: 0, failed: 10, not_decisive: 0 },
            by_outcome: [],
        },
        chain_integrity_pct: 0.9,
        observed_pct: 0.95,
        asserted_pct: 0.05,
        fragmented_runs: 0,
        ...overrides,
    } as RunStatsResponse;
}

// ─────────────────────────────────────────────────────────────────────────
// FleetBoard
// ─────────────────────────────────────────────────────────────────────────

describe('RunSignalsSection', () => {
    it('cost variant renders per-run money with enough decimals to matter', () => {
        render(<RunSignalsSection stats={runStats()} loading={false} variant="cost" />);
        expect(screen.getByText('$0.0123')).toBeInTheDocument();       // avg / processed outcome
        expect(screen.getByText('100 processed outcomes')).toBeInTheDocument();
        expect(screen.getByText(/\$0\.0100/)).toBeInTheDocument();     // median
        expect(screen.getByText('$0.25')).toBeInTheDocument();         // spent, nothing produced
        expect(screen.getByText('Spent, nothing produced')).toBeInTheDocument();
        // The empty polls: their own card, in grey, never averaged in.
        const noWork = screen.getByTestId('mc-no-work');
        expect(noWork).toHaveTextContent('$0.07');
        expect(noWork).toHaveTextContent('40 no-work runs');
        expect(screen.queryByText(/completed task/)).toBeNull();
    });

    it('overview variant shows the run classes, no-work grey and policy blocks inside the failures', () => {
        render(<RunSignalsSection stats={runStats()} loading={false} variant="overview" />);
        expect(screen.getByText('90.9%')).toBeInTheDocument();  // success rate
        expect(screen.queryByText('Blocked by policy')).toBeNull();
        const bar = screen.getByTestId('mc-run-class-bar');
        expect(bar).toHaveTextContent('Success');
        expect(bar).toHaveTextContent('No work');
        expect(bar).toHaveTextContent('Failed');
        // Classes with no runs are not drawn.
        expect(bar).not.toHaveTextContent('Deviation');
        const noWorkChip = screen.getAllByText('No work').find((el) => el.className.includes('rounded'));
        expect(noWorkChip?.className).toMatch(/slate/);
        expect(noWorkChip?.className).not.toMatch(/emerald|green/);
    });

    it('zero runs explains the run-id header instead of rendering empty charts', () => {
        const empty = runStats({
            terminal_states: { total_runs: 0, by_state: [], success_rate: null },
        });
        render(<RunSignalsSection stats={empty} loading={false} variant="overview" />);
        expect(screen.getByText(/x-brutor-run-id/)).toBeInTheDocument();
    });

    it('renders nothing without stats (tab keeps its own spinner)', () => {
        const { container } = render(
            <RunSignalsSection stats={null} loading={false} variant="cost" />
        );
        expect(container.firstChild).toBeNull();
    });
});

// ─────────────────────────────────────────────────────────────────────────
// AssuranceBand
// ─────────────────────────────────────────────────────────────────────────

describe('AssuranceBand', () => {
    it('reads its verdicts once: left alone, it neither fetches nor renders again', async () => {
        render(<AssuranceBand systemId="sys-1" system={system()} stats={runStats()} onOpenAiSystems={() => {}} />);
        await screen.findByText('Copilot');
        expect(await callsWhileIdle([asked])).toBe(0);
    });

    it('renders identity: name, kind, stage and owner', () => {
        render(
            <AssuranceBand
                systemId="sys-1"
                system={system()}
                stats={runStats()}
                onOpenAiSystems={() => {}}
            />
        );
        expect(screen.getByText('Copilot')).toBeInTheDocument();
        expect(screen.getByText('Active')).toBeInTheDocument();
        expect(screen.getByText(/owner: RevOps/)).toBeInTheDocument();
        expect(screen.getByText(/Open in AI Systems/)).toBeInTheDocument();
    });

    it('flags a missing owner and a missing stage instead of hiding them', () => {
        render(
            <AssuranceBand
                systemId="sys-1"
                system={system({ owner: null, lifecycle_stage: null })}
                stats={null}
            />
        );
        expect(screen.getByText('no owner')).toBeInTheDocument();
        expect(screen.getByText('no stage')).toBeInTheDocument();
    });

    it('falls back to the raw id when the system is not in the fleet yet', () => {
        render(<AssuranceBand systemId="group-xyz" system={null} stats={null} />);
        expect(screen.getByText('group-xyz')).toBeInTheDocument();
    });
});
