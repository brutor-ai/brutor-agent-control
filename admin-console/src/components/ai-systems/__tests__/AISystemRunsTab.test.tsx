/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for AISystemRunsTab.
 *
 * The behaviours worth pinning are the honesty ones. A dashboard that
 * quietly reports "0% exhausted" when nothing is reporting, or paints a
 * guardrail block red like an outage, teaches operators the wrong thing
 * far more effectively than any doc corrects it.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetchRuns = vi.fn();
const mockFetchStats = vi.fn();
// The tab embeds LivenessCard, which reaches for these. Mocking
// them keeps the tab rendering what a user actually sees rather than a
// card stuck in its error state.
const mockFetchLiveness = vi.fn();
const mockFetchTimeseries = vi.fn();
const mockFetchRun = vi.fn();
const mockFetchDrift = vi.fn();
const mockFetchBaselines = vi.fn();
// Incident windows: listed on Signals + Lifecycle, shaded on charts.
const mockIncidentWindows = vi.fn();
const mockFetchOutcomes = vi.fn();
// A shared service's served calls.
const mockFetchServed = vi.fn();

// The pages key their loads on these: one identity for the whole file, or
// every render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemRuns: mockFetchRuns,
    fetchAISystemServedCalls: mockFetchServed,
    fetchAISystemRunStats: mockFetchStats,
    fetchAISystemRunTimeseries: mockFetchTimeseries,
    fetchRun: mockFetchRun,
    fetchAISystemLiveness: mockFetchLiveness,
    updateAISystemLiveness: vi.fn(),
    learnAISystemLiveness: vi.fn(),
    acceptAISystemLiveness: vi.fn(),
    // The tab also embeds DriftPanel.
    fetchAISystemDrift: mockFetchDrift,
    fetchAISystemBaselines: mockFetchBaselines,
    fetchAISystemBaselineEpochs: vi.fn().mockResolvedValue({ epochs: [], total: 0 }),
    // The tab shades incident windows on its charts.
    fetchAISystemIncidentWindows: mockIncidentWindows,
    fetchIncidentWindows: vi.fn(),
    createIncidentWindow: vi.fn(),
    updateIncidentWindow: vi.fn(),
    deleteIncidentWindow: vi.fn(),
    acknowledgeDriftEvent: vi.fn(),
    closeDriftEvent: vi.fn(),
    // ...and ContractsPanel.
    fetchAISystemContracts: vi.fn().mockResolvedValue({
        contracts: [], total: 0, active_version: null, config_drifted: false,
    }),
    fetchAISystemLifecycle: vi.fn().mockResolvedValue({
        ai_system_id: 'sys-1', current_stage: 'proposed', transitions: [],
    }),
    mintAISystemContract: vi.fn(),
    approveContract: vi.fn(),
    rejectContract: vi.fn(),
    promoteContract: vi.fn(),
    transitionAISystemLifecycle: vi.fn(),
    // ...and ResponsePolicyPanel.
    fetchResponsePolicies: vi.fn().mockResolvedValue({
        policies: [], total: 0, inherited_count: 0,
    }),
    fetchResponseHistory: vi.fn().mockResolvedValue({
        responses: [], total: 0, acted_count: 0, pending_count: 0,
    }),
    createResponsePolicy: vi.fn(),
    previewResponsePolicy: vi.fn(),
    updateResponsePolicy: vi.fn(),
    deleteResponsePolicy: vi.fn(),
    restoreAutonomy: vi.fn(),
    fetchReplayStatus: vi.fn(),
    // ...and AssurancePanel.
    fetchSystemHealth: vi.fn().mockResolvedValue({
        ai_system_id: 'sys-1', health_state: 'unknown', runs_24h: 0,
        open_drift_events: 0, reasons: [], is_trustworthy: false,
    }),
    fetchAssuranceReport: vi.fn().mockResolvedValue({ not_covered: [] }),
    captureAssuranceReport: vi.fn(),
    fetchAssuranceHistory: vi.fn().mockResolvedValue({ reports: [], total: 0 }),
    // ...and the Composition tab.
    fetchAISystemComposition: vi.fn().mockResolvedValue(COMPOSITION_PAYLOAD),
    // The catalogue labels the outcome tables.
    fetchAISystemOutcomes: mockFetchOutcomes,
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => stableGateway,
}));

import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { OVERLAY_RUN } from './fixtures/runOverlay';

import AISystemRunsTab from '../AISystemRunsTab';
import { callsWhileIdle } from '../../../test/idle';
import type { RunStatsResponse, RunSummary } from '../../../lib/aiSystemTypes';

function baseStats(overrides: Partial<RunStatsResponse> = {}): RunStatsResponse {
    return {
        ai_system_id: 'sys-1',
        period_hours: 24,
        window_start: null,
        window_end: null,
        cost: {
            processed_runs: 4,
            total_cost_usd: 2.0,
            cost_per_processed_outcome: 0.25,
            median_cost_per_processed_outcome: 0.2,
            p95_cost_per_processed_outcome: 0.9,
            by_outcome: [
                { outcome_id: 'application_processed.loan_approved', runs: 3, cost_usd: 0.9, cost_per_run: 0.3, median_cost_usd: 0.29, p95_cost_usd: 0.35 },
                { outcome_id: 'application_processed.underwriter_decision_applied', runs: 1, cost_usd: 0, cost_per_run: 0, median_cost_usd: 0, p95_cost_usd: 0 },
            ],
            wasted_cost_usd: 1.0,
            no_work_runs: 7,
            no_work_cost_usd: 0.0123,
            total_tokens: 1000,
            tokens_per_processed_outcome: 250,
        },
        actions: {
            runs: 4,
            finished_runs: 8,
            by_outcome: [
                { outcome_id: 'application_processed.loan_approved', runs: 3, median_actions: 10, p95_actions: 12, max_actions: 12 },
                { outcome_id: 'application_processed.underwriter_decision_applied', runs: 1, median_actions: 1, p95_actions: 1, max_actions: 1 },
            ],
            median_actions: 5,
            p95_actions: 20,
            max_actions: 42,
            distribution: [
                { lower: 1, upper: 2, count: 1 },
                { lower: 3, upper: 5, count: 3 },
                { lower: 101, upper: null, count: 0 },
            ],
            exhausted_runs: 0,
            exhaustion_rate: 0,
            exhaustion_reporting_available: false,
            abandoned_runs: 2,
        },
        tool_errors: {
            total_tool_calls: 20,
            total_tool_errors: 3,
            overall_error_rate: 0.15,
            by_tool: [
                { tool: 'timeout', calls: 20, errors: 2, error_rate: 0.1, error_types: {} },
                { tool: 'upstream_500', calls: 20, errors: 1, error_rate: 0.05, error_types: {} },
            ],
        },
        terminal_states: {
            total_runs: 14,
            by_state: [
                { state: 'completed', count: 12, share: 0.857 },
                { state: 'errored', count: 2, share: 0.143 },
            ],
            success_rate: 0.5,
        },
        outcomes: {
            total_runs: 14,
            success_rate: 0.5,
            by_class: { success: 4, no_work: 7, deviation: 1, failed: 2, not_decisive: 0 },
            by_outcome: [
                { outcome_id: 'application_processed.loan_approved', conformance: 'declared', counts_toward_baseline: true, run_class: 'success', runs: 3, share: 3 / 14, cost_usd: 0.9 },
                { outcome_id: 'application_processed.underwriter_decision_applied', conformance: 'declared', counts_toward_baseline: true, run_class: 'success', runs: 1, share: 1 / 14, cost_usd: 0 },
                { outcome_id: 'no_applications_available', conformance: 'declared', counts_toward_baseline: false, run_class: 'no_work', runs: 7, share: 0.5, cost_usd: 0.0123 },
                { outcome_id: 'rogue_ending', conformance: 'undeclared', counts_toward_baseline: null, run_class: 'deviation', runs: 1, share: 1 / 14, cost_usd: 0.1 },
                { outcome_id: null, conformance: 'unreported', counts_toward_baseline: null, run_class: 'failed', runs: 2, share: 2 / 14, cost_usd: 0.9 },
            ],
        },
        error_causes: {
            failed_runs: 2,
            by_cause: [
                { cause: 'blocked_by_policy', runs: 1, share: 0.5 },
                { cause: 'model_unavailable', runs: 1, share: 0.5 },
            ],
        },
        chain_integrity_pct: 1,
        observed_pct: 1,
        asserted_pct: 0,
        fragmented_runs: 0,
        ...overrides,
    };
}

const run: RunSummary = {
    id: 'run-1',
    root_task_id: 'root-1',
    ai_system_id: 'sys-1',
    via_system_ids: [],
    terminal_state: 'completed',
    finalized_by: 'explicit',
    outcome_id: 'application_processed.loan_approved',
    outcome_conformance: 'declared',
    outcome_counts_toward_baseline: true,
    outcome_contract_version: 3,
    run_class: 'success',
    error_cause: null,
    action_count: 5,
    step_count: 0,
    llm_call_count: 3,
    tool_call_count: 2,
    max_delegation_depth: 0,
    distinct_tools: 2,
    total_tokens: 300,
    total_cost_usd: 0.25,
    error_count: 0,
    tool_error_count: 0,
    guardrail_block_count: 0,
    policy_denial_count: 0,
    chain_integrity: 'intact',
    reopened_count: 0,
    is_replay: false,
    started_at: '2026-07-27T10:00:00Z',
};

function timeseries(points?: unknown[]) {
    return {
        ai_system_id: 'sys-1',
        period_hours: 24,
        bucket: 'hour' as const,
        window_start: '2026-07-27T00:00:00+00:00',
        window_end: '2026-07-27T12:00:00+00:00',
        points: points ?? [
            {
                bucket_start: '2026-07-27T10:00:00Z',
                runs: 4, success: 3, no_work: 0, deviation: 0, failed: 1, not_decisive: 0,
                cost_usd: 0.9, processed_cost_usd: 0.75,
                cost_per_processed_outcome: 0.25,
                tool_calls: 10, tool_errors: 1, tool_error_rate: 0.1,
                avg_actions: 5,
            },
        ],
    };
}

/** Open the Runs tab. The run list is tabbed; liveness, drift and the
 *  chain-integrity disclosure are not — see the component docstring. */
/** The Signals tab opens on Liveness; each signal family sits on its own
 *  sub-tab. Open one and return its section. */
async function openSignal(sub: 'liveness' | 'cost' | 'trajectory' | 'errors' | 'outcomes' | 'incidents') {
    // The page opens on Lifecycle; Signals is one click away.
    fireEvent.click(await screen.findByTestId('runs-tab-signals'));
    await screen.findByTestId('signals-subtabs');
    fireEvent.click(screen.getByTestId(`signals-subtab-${sub}`));
    const section = {
        liveness: 'liveness-card',
        cost: 'signal-cost',
        trajectory: 'signal-actions',
        errors: 'signal-errors',
        outcomes: 'signal-outcomes',
        incidents: 'incident-windows-panel',
    }[sub];
    return screen.findByTestId(section);
}

function openRuns() {
    fireEvent.click(screen.getByTestId('runs-tab-runs'));
}

describe('AISystemRunsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockIncidentWindows.mockResolvedValue({ windows: [], total: 0 });
        mockFetchStats.mockResolvedValue(baseStats());
        mockFetchRuns.mockResolvedValue({ runs: [run], total: 1, limit: 50, offset: 0 });
        mockFetchLiveness.mockResolvedValue({
            ai_system_id: 'sys-1',
            configured: true,
            expectation: {
                id: 'live-1',
                ai_system_id: 'sys-1',
                mode: 'continuous',
                expected_min_runs: 1,
                window_seconds: 3600,
                grace_seconds: 300,
                enabled: true,
                accepted_from_learned: false,
                alerts_on_silence: true,
                deadline_seconds: 3900,
                has_learned_cadence: false,
                last_checked_at: '2026-07-27T12:00:00Z',
            },
            state: 'alive',
            reason: '1 run(s) in the window.',
            window_hours: 720,
            inbound_calls: 0,
            last_activity_at: '2026-07-27T11:59:00Z',
            last_run_at: '2026-07-27T11:59:00Z',
            quiet_for_seconds: 60,
            last_processed_run_at: '2026-07-27T11:00:00Z',
            processed_quiet_for_seconds: 3600,
            work_quiet: false,
            has_ever_run: true,
            is_silent: false,
        });
        mockFetchDrift.mockResolvedValue({
            events: [], total: 0, open_count: 0, unexplained_count: 0,
        });
        mockFetchBaselines.mockResolvedValue({ baselines: [], total: 0, learning: 0 });
        mockFetchTimeseries.mockResolvedValue(timeseries());
        mockFetchOutcomes.mockResolvedValue({
            ai_system_id: 'sys-1',
            outcomes: null,
            active_outcomes: [
                {
                    id: 'application_processed', label: 'Application processed', counts_toward_baseline: true,
                    variants: [
                        { id: 'loan_approved', label: 'Recommended: approve' },
                        { id: 'underwriter_decision_applied', label: 'Underwriter-approved decision applied' },
                    ],
                },
                { id: 'no_applications_available', label: 'Nothing to screen', counts_toward_baseline: false },
            ],
            active_contract_version: 3,
            reportable_ids: [],
            config_drifted: false,
        });
    });

    it('loads once: left alone, the tab fetches nothing more', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await screen.findByTestId('runs-tab-signals');
        expect(await callsWhileIdle([mockFetchLiveness, mockFetchDrift, mockFetchStats])).toBe(0);
    });

    it('renders liveness above the traffic-derived signals', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('runs-tab-signals'));
        // Overview is the landing sub-tab: the worst-of verdict first.
        expect(await screen.findByTestId('signals-overview')).toBeInTheDocument();
        expect(screen.getByTestId('signals-subtab-overview')).toHaveAttribute('aria-selected', 'true');
        fireEvent.click(screen.getByTestId('signals-subtab-liveness'));
        // A system that stopped calling looks healthy on every signal
        // below, so the liveness card has to be present and load cleanly.
        expect(await screen.findByTestId('liveness-card')).toBeInTheDocument();
        expect(screen.getByTestId('pill-alive')).toBeInTheDocument();
    });

    it('counts model errors on the Errors tab, not only tool errors', async () => {
        mockFetchStats.mockResolvedValue({
            ...baseStats(),
            tool_errors: { total_tool_calls: 529, total_tool_errors: 0, overall_error_rate: 0, by_tool: [] },
            step_errors: {
                total_calls: 700,
                total_errors: 8,
                overall_error_rate: 8 / 700,
                by_kind: [
                    { kind: 'model', calls: 171, errors: 8, error_rate: 8 / 171, status_codes: { '502': 8 } },
                    { kind: 'tool', calls: 529, errors: 0, error_rate: 0, status_codes: {} },
                ],
            },
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        const section = await openSignal('errors');
        expect(section).toHaveTextContent('Errors by call type');
        // Beside the calls: the failed runs, by error cause, in words.
        const causes = screen.getByTestId('errors-by-cause');
        expect(causes).toHaveTextContent('2 failed runs');
        expect(screen.getByTestId('errors-cause-model_unavailable')).toHaveTextContent('Model unavailable');
        expect(screen.getByTestId('errors-cause-blocked_by_policy')).toHaveTextContent('Blocked by policy');
        expect(screen.getByTestId('errors-cause-blocked_by_policy')).toHaveTextContent('50.0%');
        expect(screen.getByTestId('errors-total-errors')).toHaveTextContent('8');
        const model = screen.getByTestId('errors-kind-model');
        expect(model).toHaveTextContent('Model calls');
        expect(model).toHaveTextContent('502 × 8');
        expect(screen.getByTestId('errors-kind-tool')).toHaveTextContent('Tool calls');
        expect(screen.getByTestId('signals-window-note')).toHaveTextContent(/Range: .*range buttons above/);
    });

    it('renders a trend chart under each signal section', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        expect(await screen.findByTestId('trend-cost')).toBeInTheDocument();
        await openSignal('trajectory');
        expect(await screen.findByTestId('trend-actions')).toBeInTheDocument();
        await openSignal('errors');
        expect(await screen.findByTestId('trend-errors')).toBeInTheDocument();
        await openSignal('outcomes');
        expect(await screen.findByTestId('trend-outcomes')).toBeInTheDocument();
    });

    it('explains the blank stretches of a trend chart instead of leaving a bare gap', async () => {
        // The default series has one busy hour (10:00) in a 12-hour window.
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        const legend = await screen.findByTestId('trend-cost-gap-legend');
        expect(legend).toHaveTextContent('No runs');
        // The one busy hour's processed outcomes cost something, so no hour is "unpaid".
        expect(legend).not.toHaveTextContent('No paid outcomes');
        expect(legend).not.toHaveTextContent('No processed outcomes');
        await openSignal('trajectory');
        expect(await screen.findByTestId('trend-actions-gap-legend')).toHaveTextContent('No runs');
        // Only the reasons drawn are explained: no hour here has runs without calls.
        await openSignal('errors');
        expect(await screen.findByTestId('trend-errors-gap-legend')).not.toHaveTextContent('No calls');
    });

    // A declared window is shaded on every trend chart, with a
    // legend entry that says what the shading means.
    it('shades a declared incident window on each trend chart, with a legend', async () => {
        mockIncidentWindows.mockResolvedValue({
            windows: [{
                id: 'iw-1', ai_system_id: 'sys-1', tenant_wide: false,
                starts_at: '2026-07-27T09:30:00Z', ends_at: '2026-07-27T11:00:00Z',
                cause_category: 'provider_outage', reason: 'credits ran out', runs_excluded: 4,
            }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        const legend = await screen.findByTestId('trend-cost-incident-legend');
        expect(legend).toHaveTextContent('declared incident window');
        expect(within(legend).getByTestId('incident-legend-item')).toHaveTextContent('Provider outage');
        await openSignal('outcomes');
        expect(await screen.findByTestId('trend-outcomes-incident-legend')).toBeInTheDocument();
        expect(mockIncidentWindows).toHaveBeenCalledWith('sys-1');
    });

    it('no legend when no window overlaps the chart', async () => {
        mockIncidentWindows.mockResolvedValue({
            windows: [{
                id: 'iw-old', ai_system_id: 'sys-1', tenant_wide: false,
                starts_at: '2026-06-01T00:00:00Z', ends_at: '2026-06-02T00:00:00Z',
                cause_category: 'maintenance', reason: 'old', runs_excluded: 0,
            }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        expect(await screen.findByTestId('trend-cost')).toBeInTheDocument();
        expect(screen.queryByTestId('trend-cost-incident-legend')).not.toBeInTheDocument();
    });

    it('lists incident windows on the Signals tab and the Lifecycle tab', async () => {
        const { unmount } = render(<AISystemRunsTab aiSystemId="sys-1" />);
        expect(await openSignal('incidents')).toBeInTheDocument();
        unmount();
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="lifecycle" />);
        const lifecycle = await screen.findByTestId('lifecycle-panel');
        const panel = await screen.findByTestId('incident-windows-panel');
        expect(panel.closest('.lg\\:col-span-3')).toBe(lifecycle.closest('.lg\\:col-span-3'));
    });

    it('a window with no runs shows no charts — the tiles already say quiet', async () => {
        mockFetchTimeseries.mockResolvedValue(timeseries([]));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        expect(screen.queryByTestId('trend-cost')).not.toBeInTheDocument();
    });

    it('a failed timeseries fetch never takes down the tiles', async () => {
        mockFetchTimeseries.mockRejectedValue(new Error('boom'));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        expect(await openSignal('cost')).toBeInTheDocument();
        expect(screen.queryByTestId('trend-cost')).not.toBeInTheDocument();
    });

    it('signal sections carry the observed chip; liveness carries editable', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('runs-tab-signals'));
        fireEvent.click(await screen.findByTestId('signals-subtab-liveness'));
        // The one configurable card on the page says so explicitly.
        expect(await screen.findByTestId('liveness-editable-chip')).toHaveTextContent(/editable/i);
        const cost = await openSignal('cost');
        expect(cost).toHaveTextContent(/observed/i);
    });

    it('keeps the header to one row so the tabs stay above the fold', async () => {
        // Identity, verdicts, period and actions all share a single line. The
        // guarantee is the row COUNT — the page's prose explanation lives in a
        // help tip precisely so the tab strip clears a 1080p fold. How the row
        // is composed may change; that it stays at one is the thing worth
        // pinning.
        render(
            <AISystemRunsTab
                aiSystemId="sys-1"
                identity={<span data-testid="test-identity">Internal Docs RAG</span>}
            />,
        );
        await screen.findByTestId('status-strip');

        // One row holds all four: name, verdicts, period, actions.
        const headerRow = screen.getByTestId('test-identity').parentElement!;
        const pills = screen.getByTestId('period-pills');
        expect(headerRow).toContainElement(screen.getByTestId('status-strip'));
        expect(headerRow).toContainElement(pills);
        expect(headerRow).toContainElement(
            screen.getByTestId('lifecycle-guide-button'),
        );

        // The period pills work from the header row.
        fireEvent.click(within(pills).getByText('7d'));
        await waitFor(() => expect(mockFetchStats).toHaveBeenCalledWith('sys-1', 168));
    });

    // -- panel placement: each control sits beside what it acts on --------

    it('Composition is the first tab and the default; Lifecycle is second', async () => {
        // A system opened from the list lands on what it is made of and what
        // it depends on, without a click.
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        expect(await screen.findByTestId('composition-tab')).toBeInTheDocument();
        expect(screen.queryByTestId('lifecycle-panel')).not.toBeInTheDocument();

        const tabs = within(screen.getByTestId('runs-tabstrip')).getAllByRole('tab');
        expect(tabs[0]).toHaveTextContent('Composition');
        expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
        expect(tabs[1]).toHaveTextContent('Lifecycle');
        expect(tabs[2]).toHaveTextContent('Signals');
    });

    it('replay lives beside the lifecycle gate it feeds', async () => {
        // A passing replay is what approved → active consumes, so it sits
        // next to the stage rail rather than under the contract list.
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="lifecycle" />);
        const lifecycle = await screen.findByTestId('lifecycle-panel');
        const replay = screen.getByTestId('replay-panel');
        expect(replay).toBeInTheDocument();
        expect(lifecycle.closest('.lg\\:col-span-3')).not.toBeNull();
        expect(replay.closest('.lg\\:col-span-2')).not.toBeNull();
    });

    it('the Contract tab holds the contract and its sub-tabs, not the gate', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="contract" />);
        await screen.findByTestId('contracts-panel');
        expect(screen.getByTestId('contract-subtabs')).toBeInTheDocument();
        expect(screen.queryByTestId('replay-panel')).not.toBeInTheDocument();
        expect(screen.queryByTestId('lifecycle-gate')).not.toBeInTheDocument();
    });

    it('the automatic response lives on the Drift tab, beside the findings it fires on', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="drift" />);
        const drift = await screen.findByTestId('drift-panel');
        const response = screen.getByTestId('response-policy-panel');
        expect(response).toBeInTheDocument();

        expect(drift.closest('.lg\\:col-span-3')).not.toBeNull();
        expect(response.closest('.lg\\:col-span-2')).not.toBeNull();
    });

    it('Assurance keeps only the report now that both controls have moved', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="assurance" />);
        await screen.findByTestId('assurance-panel');
        expect(screen.queryByTestId('replay-panel')).not.toBeInTheDocument();
        expect(screen.queryByTestId('response-policy-panel')).not.toBeInTheDocument();
    });

    it('initialSection opens the tab directly on that section (findings deep-link)', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="drift" />);
        // The Mission Control findings badge lands here — the Drift panel
        // must be the first thing on screen, not a tab away.
        expect(await screen.findByTestId('drift-panel')).toBeInTheDocument();
        expect(screen.queryByTestId('signal-cost')).not.toBeInTheDocument();
    });

    it('loads stats and runs on mount', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        expect(mockFetchStats).toHaveBeenCalledWith('sys-1', 24);
        // Both fetches fire on mount regardless of which tab is showing —
        // switching tabs must not cost a round trip.
        expect(mockFetchRuns).toHaveBeenCalled();
        openRuns();
        expect(screen.getByTestId('run-row-run-1')).toBeInTheDocument();
    });

    it('explains each cost tile behind its own help button', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        for (const label of ['Per processed outcome', 'Median', 'p95', 'Spent, nothing produced', 'No-work overhead']) {
            expect(screen.getByRole('button', { name: `What ${label} means` })).toBeInTheDocument();
        }
        fireEvent.click(screen.getByRole('button', { name: 'What Spent, nothing produced means' }));
        const pop = await screen.findByTestId('help-tip-popover');
        // Policy blocks are failures: they are in the waste, no-work is not.
        expect(pop).toHaveTextContent('blocked by policy included');
        expect(pop).toHaveTextContent('deviating run');
        expect(pop).toHaveTextContent('No-work runs are not counted here');
        expect(within(screen.getByTestId('signal-cost')).getByText('Failed and deviating runs')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'What Per processed outcome means' }));
        await waitFor(() => expect(screen.getAllByTestId('help-tip-popover').some((p) => /empty poll/.test(p.textContent ?? ''))).toBe(true));
    });

    it('splits cost per processed outcome by outcome and variant, never one blended average', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        const table = await screen.findByTestId('cost-by-outcome');
        const approved = within(table).getByTestId('cost-outcome-application_processed.loan_approved');
        expect(approved).toHaveTextContent('loan_approved');
        expect(approved).toHaveTextContent('3');
        expect(approved).toHaveTextContent('$0.3000');
        expect(approved).toHaveTextContent('$0.2900');
        expect(approved).toHaveTextContent('$0.3500');
        // Labelled from the active contract's catalogue.
        expect(await within(approved).findByText(/Recommended: approve/)).toBeInTheDocument();
        expect(within(table).getByTestId('cost-outcome-application_processed.underwriter_decision_applied')).toHaveTextContent('$0.0000');
        // The empty polls are not a row of the work.
        expect(within(table).queryByTestId('cost-outcome-no_applications_available')).toBeNull();
    });

    it('reports the no-work overhead as its own card', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        const card = await screen.findByTestId('cost-no-work');
        expect(card).toHaveTextContent('No-work overhead');
        expect(card).toHaveTextContent('$0.0123');
        expect(card).toHaveTextContent('7 no-work runs');
        // Neutral grey, not good news and not a warning.
        expect(card.innerHTML).toMatch(/text-slate-600/);
        expect(card.innerHTML).not.toMatch(/emerald|green|amber/);
    });

    it('explains each trajectory tile behind its own help button', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('trajectory');
        for (const label of ['Median actions', 'p95 actions', 'Max actions', 'Exhausted']) {
            expect(screen.getByRole('button', { name: `What ${label} means` })).toBeInTheDocument();
        }
        fireEvent.click(screen.getByRole('button', { name: 'What Median actions means' }));
        const pop = await screen.findByTestId('help-tip-popover');
        expect(pop).toHaveTextContent('one governed call through the gateway');
        // Over the work only: a one-action poll is not a short task.
        expect(pop).toHaveTextContent('Empty polls and failed runs are left out');
    });

    it('splits run length by outcome and variant, over processed outcomes only', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        const section = await openSignal('trajectory');
        expect(screen.getByTestId('actions-median')).toHaveTextContent('4 processed');
        const table = await within(section).findByTestId('actions-by-outcome');
        expect(within(table).getByTestId('actions-outcome-application_processed.loan_approved')).toHaveTextContent('10');
        expect(within(table).getByTestId('actions-outcome-application_processed.underwriter_decision_applied')).toHaveTextContent('1');
        expect(within(table).queryByTestId('actions-outcome-no_applications_available')).toBeNull();
    });

    it('pages through every run, not just the first 50', async () => {
        // The trial showed "Showing 50 of 431 runs" with no way to the other 381.
        const page = (offset: number) => ({
            runs: [{ ...run, id: `run-${offset}` }], total: 120, limit: 50, offset,
        });
        mockFetchRuns.mockImplementation((_id: string, f: { offset: number }) => Promise.resolve(page(f.offset)));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        expect(await screen.findByTestId('runs-page-range')).toHaveTextContent('Showing 1–50 of 120 runs.');
        expect(screen.getByTestId('runs-page-page')).toHaveTextContent('Page 1 of 3');
        expect(screen.getByTestId('runs-page-prev')).toBeDisabled();
        const statsCalls = mockFetchStats.mock.calls.length;

        fireEvent.click(screen.getByTestId('runs-page-next'));
        expect(await screen.findByTestId('run-row-run-50')).toBeInTheDocument();
        expect(mockFetchRuns).toHaveBeenLastCalledWith('sys-1', expect.objectContaining({ limit: 50, offset: 50 }));
        expect(screen.getByTestId('runs-page-range')).toHaveTextContent('Showing 51–100 of 120 runs.');
        // Turning a page fetches the list only, not the tiles and charts.
        expect(mockFetchStats.mock.calls.length).toBe(statsCalls);

        fireEvent.click(screen.getByTestId('runs-page-next'));
        expect(await screen.findByTestId('run-row-run-100')).toBeInTheDocument();
        expect(screen.getByTestId('runs-page-range')).toHaveTextContent('Showing 101–120 of 120 runs.');
        expect(screen.getByTestId('runs-page-next')).toBeDisabled();
    });

    it('starts again at the first page when the outcome filter changes', async () => {
        mockFetchRuns.mockImplementation((_id: string, f: { offset: number }) =>
            Promise.resolve({ runs: [{ ...run, id: `run-${f.offset}` }], total: 120, limit: 50, offset: f.offset }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        fireEvent.click(await screen.findByTestId('runs-page-next'));
        await screen.findByTestId('run-row-run-50');
        fireEvent.change(screen.getByLabelText('Filter by execution'), { target: { value: 'errored' } });
        await waitFor(() => expect(mockFetchRuns).toHaveBeenLastCalledWith(
            'sys-1', expect.objectContaining({ terminal_state: 'errored', offset: 0 })));
    });

    it('starts again at the first page when the run class filter changes', async () => {
        mockFetchRuns.mockImplementation((_id: string, f: { offset: number }) =>
            Promise.resolve({ runs: [{ ...run, id: `run-${f.offset}` }], total: 120, limit: 50, offset: f.offset }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        fireEvent.click(await screen.findByTestId('runs-page-next'));
        await screen.findByTestId('run-row-run-50');
        fireEvent.change(screen.getByLabelText('Filter by run class'), { target: { value: 'no_work' } });
        await waitFor(() => expect(mockFetchRuns).toHaveBeenLastCalledWith(
            'sys-1', expect.objectContaining({ run_class: 'no_work', offset: 0 })));
    });

    it('shows no pager when every run fits on one page', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        await screen.findByTestId('run-row-run-1');
        expect(screen.queryByTestId('runs-page-next')).not.toBeInTheDocument();
    });

    it('keeps the status strip and coverage disclosure on every tab', async () => {
        // The qualifiers must never be tabbable away — but a qualifier is a
        // verdict, not a form. The strip carries the verdicts (health with
        // its age, liveness, drift count, coverage) on every tab; the full
        // cards live one click behind their chips.
        mockFetchStats.mockResolvedValue(baseStats({ observed_pct: 0.6, asserted_pct: 0.2 }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        expect(await screen.findByTestId('status-strip')).toBeInTheDocument();
        await waitFor(() =>
            expect(screen.getByTestId('chain-integrity-warning')).toBeInTheDocument()
        );

        for (const tab of ['runs', 'drift', 'contract', 'assurance', 'signals'] as const) {
            fireEvent.click(screen.getByTestId(`runs-tab-${tab}`));
            expect(screen.getByTestId('status-strip')).toBeInTheDocument();
            expect(screen.getByTestId('chain-integrity-warning')).toBeInTheDocument();
        }
    });

    it('does not warn about coverage when asserted runs made it complete', async () => {
        // A client-asserted run is a fully observed run. A banner keyed on the
        // signed-only share would tell operators a third of their traffic is
        // missing when every step of it is on the ledger.
        mockFetchStats.mockResolvedValue(
            baseStats({ chain_integrity_pct: 0.6, asserted_pct: 0.4, observed_pct: 1 })
        );
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchStats).toHaveBeenCalled());
        expect(screen.queryByTestId('chain-integrity-warning')).not.toBeInTheDocument();
    });

    // The release per run: name@version and a trust marker.
    it('shows each run’s release with its trust, and "—" when it declared none', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [
                { ...run, agent_impl_name: 'screening-agent', agent_impl_version: '1.5.0', agent_impl_build: 'abc123', agent_impl_trust: 'verified' },
                { ...run, id: 'run-2', root_task_id: 'root-2' },
            ],
            total: 2, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        const cell = screen.getByTestId('release-run-1');
        expect(cell).toHaveTextContent('screening-agent@1.5.0');
        expect(within(cell).getByTestId('trust-marker')).toHaveAttribute('data-trust', 'verified');
        expect(within(cell).getByTitle('screening-agent@1.5.0 (abc123)')).toBeInTheDocument();
        expect(screen.getByTestId('release-run-2')).toHaveTextContent('—');
    });

    // The normalised client label, under the release.
    it('shows the run’s client channel under its release, and nothing when there is none', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [
                { ...run, client_label: 'python-httpx' },
                { ...run, id: 'run-2', root_task_id: 'root-2', client_label: null },
            ],
            total: 2, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        expect(screen.getByTestId('client-run-1')).toHaveTextContent('via python-httpx');
        expect(screen.queryByTestId('client-run-2')).toBeNull();
    });

    it('the header and every row carry the same number of columns (the timeline row spans them all)', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        const headerCells = within(screen.getByTestId('runs-thead')).getAllByRole('columnheader').length;
        const rowCells = screen.getByTestId('run-row-run-1').querySelectorAll(':scope > td').length;
        expect(rowCells).toBe(headerCells);
        mockFetchRun.mockResolvedValue({ ...run, actions: [] });
        fireEvent.click(screen.getByTestId('run-row-run-1'));
        const timeline = await screen.findByTestId('run-actions-run-1');
        expect(timeline.querySelector('td')?.getAttribute('colspan')).toBe(String(headerCells));
    });

    it('names each run by its id, in full and without expanding the row', async () => {
        // Every other surface addresses a run by id — the Actions lookup
        // takes one as input — so the list it came from has to show it, and
        // reaching for it must not be read as "show me the timeline".
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.spyOn(navigator, 'clipboard', 'get').mockReturnValue({
            writeText,
        } as unknown as Clipboard);

        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        const idButton = screen.getByTestId('run-id-run-1');
        // Truncated in the column, full id available to copy and on hover.
        expect(idButton).toHaveAttribute('title', expect.stringContaining('run-1'));

        fireEvent.click(idButton);
        await waitFor(() => expect(writeText).toHaveBeenCalledWith('run-1'));
        expect(screen.queryByTestId('run-actions-run-1')).not.toBeInTheDocument();
    });

    it('shows the Turns cell as unknown when the list endpoint omits it', async () => {
        // The Control Plane's RunSummary declares neither `turn_count` nor
        // `turn_attribution`, so Pydantic strips them from the runs list even
        // though the columns exist on `ai_system_runs`. Rendering the missing
        // value would print a blank cell, and `turn_attribution !== 'client_asserted'`
        // is true for `undefined`, which would stamp every row with the "inferred"
        // marker — a provenance claim made from a field we never received.
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        // Other cells legitimately render an em-dash, so identify this one by
        // the title that explains why it is empty.
        const row = screen.getByTestId('run-row-run-1');
        expect(
            within(row).getByTitle(/Not returned by the runs list/i),
        ).toHaveTextContent('—');
        expect(within(row).queryByText('~')).not.toBeInTheDocument();
    });

    it('marks an inferred turn count only when the server actually says so', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, turn_count: 4, turn_attribution: 'inferred' }],
            total: 1, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        const row = screen.getByTestId('run-row-run-1');
        expect(within(row).getByText('4')).toBeInTheDocument();
        expect(within(row).getByText('~')).toBeInTheDocument();
    });

    it('leaves a client-asserted turn count unmarked', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, turn_count: 4, turn_attribution: 'client_asserted' }],
            total: 1, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        const row = screen.getByTestId('run-row-run-1');
        expect(within(row).getByText('4')).toBeInTheDocument();
        expect(within(row).queryByText('~')).not.toBeInTheDocument();
    });

    it('explains the columns an operator misreads, behind an (i)', async () => {
        // These seven are the ones that get read wrong: abandoned as a failure,
        // blocked-by-policy as an outage, an inferred turn count as a reported
        // one, a non-intact chain as cosmetic.
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        const header = (name: string) =>
            screen.getByRole('columnheader', { name: new RegExp(name, 'i') });
        for (const col of [
            'Actor', 'Execution', 'Run class', 'Outcome', 'Error cause', 'Turns', 'Actions', 'Tools', 'Cost', 'Closed by', 'Chain',
        ]) {
            expect(within(header(col)).getByTestId('help-tip')).toBeInTheDocument();
        }

        fireEvent.click(within(header('Chain')).getByTestId('help-tip'));
        expect(screen.getByTestId('help-tip-popover')).toHaveTextContent(
            /qualifies every other figure on this row/i,
        );
        // Inside the table the last column's panel would open past the right
        // edge of an overflow-x-auto wrapper, which clips it rather than merely
        // pushing it off-screen. It renders outside the table.
        expect(screen.getByTestId('help-tip-popover').closest('table')).toBeNull();
    });

    it('remembers column widths across mounts, and can reset them', async () => {
        localStorage.removeItem('brutor.aiSystem.runs.columnWidths');
        const { unmount } = render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();

        // No reset control until there is something to reset.
        expect(screen.queryByTestId('reset-column-widths')).not.toBeInTheDocument();

        const handle = screen.getByTestId('col-resize-state');
        const th = handle.closest('th') as HTMLTableCellElement;
        vi.spyOn(th, 'getBoundingClientRect').mockReturnValue({ left: 100 } as DOMRect);

        fireEvent.mouseDown(handle);
        fireEvent.mouseMove(document, { clientX: 420 });
        fireEvent.mouseUp(document);
        expect(th).toHaveStyle({ width: '320px' });

        unmount();
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        expect(screen.getByTestId('col-resize-state').closest('th')).toHaveStyle({
            width: '320px',
        });

        fireEvent.click(screen.getByTestId('reset-column-widths'));
        expect(screen.getByTestId('col-resize-state').closest('th')).toHaveStyle({
            width: '150px',
        });
    });

    it('does not refetch when only the tab changes', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchStats).toHaveBeenCalledTimes(1));
        openRuns();
        fireEvent.click(screen.getByTestId('runs-tab-assurance'));
        fireEvent.click(screen.getByTestId('runs-tab-signals'));
        expect(mockFetchStats).toHaveBeenCalledTimes(1);
        expect(mockFetchRuns).toHaveBeenCalledTimes(1);
    });

    it('renders cost per processed outcome, not cost per run', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        // Scoped to the cost signal: the same figure also appears on the
        // run row below, so an unscoped query is ambiguous by construction.
        const section = await openSignal('cost');
        expect(section).toHaveTextContent('Cost per processed outcome');
        expect(screen.getByTestId('cost-per-processed')).toHaveTextContent('$0.2500');
        expect(screen.getByTestId('cost-per-processed')).toHaveTextContent('4 processed');
        expect(section).not.toHaveTextContent(/completed run/i);
    });

    it('reports spend that produced nothing separately', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        await waitFor(() => {
            expect(screen.getByText('Spent, nothing produced')).toBeInTheDocument();
        });
        expect(screen.getByText('$1.0000')).toBeInTheDocument();
    });

    it('says exhaustion is not reported rather than showing a healthy 0%', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('trajectory');
        const tile = await screen.findByTestId('exhaustion-unknown');
        expect(tile).toHaveTextContent('Not reported');
        expect(tile).toHaveTextContent('Needs client run-end reporting');
        // The specific failure this guards: rendering 0% would read as
        // "no runs are looping", which we cannot know.
        expect(tile).not.toHaveTextContent('0.0%');
    });

    it('shows a real exhaustion rate once clients report run ends', async () => {
        mockFetchStats.mockResolvedValue(
            baseStats({
                actions: {
                    ...baseStats().actions,
                    exhausted_runs: 2,
                    exhaustion_rate: 0.25,
                    exhaustion_reporting_available: true,
                },
            })
        );
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('trajectory');
        await waitFor(() => {
            expect(screen.getByText('25.0%')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('exhaustion-unknown')).not.toBeInTheDocument();
    });

    it('shows the run classes, with no-work in a neutral grey and never green', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('outcomes');
        expect(screen.getByTestId('outcomes-success-rate')).toHaveTextContent('50.0%');
        expect(screen.getByTestId('outcomes-processed')).toHaveTextContent('4');
        expect(screen.getByTestId('outcomes-no-work')).toHaveTextContent('7');
        expect(screen.getByTestId('outcomes-deviations')).toHaveTextContent('1');
        expect(screen.getByTestId('outcomes-failed')).toHaveTextContent('2');
        const noWork = screen.getByTestId('outcomes-class-no_work');
        expect(noWork).toHaveTextContent('No work');
        expect(noWork).toHaveTextContent('7 (50.0%)');
        expect(noWork.className).toMatch(/slate/);
        expect(noWork.className).not.toMatch(/emerald|green|lime/);
        expect(screen.getByTestId('outcomes-class-success').className).toMatch(/emerald/);
        // There is no separate "blocked" outcome: a block is a failure with a cause.
        expect(screen.getByTestId('signal-outcomes')).not.toHaveTextContent('Blocked by policy');
    });

    it('shows the mix by outcome and variant, undeclared and unreported runs visible', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('outcomes');
        const mix = await screen.findByTestId('outcomes-mix');
        // Variants grouped under their outcome.
        const group = within(mix).getByTestId('outcomes-group-application_processed');
        expect(group).toHaveTextContent('application_processed');
        expect(group).toHaveTextContent('4');
        const approved = within(mix).getByTestId('outcomes-row-application_processed.loan_approved-success');
        expect(approved).toHaveTextContent('loan_approved');
        expect(approved).toHaveTextContent('Declared');
        expect(approved).toHaveTextContent('Success');
        expect(await within(approved).findByText(/Recommended: approve/)).toBeInTheDocument();
        const poll = within(mix).getByTestId('outcomes-row-no_applications_available-no_work');
        expect(poll).toHaveTextContent('no work');
        expect(poll).toHaveTextContent('No work');
        const rogue = within(mix).getByTestId('outcomes-row-rogue_ending-deviation');
        expect(rogue).toHaveTextContent('Undeclared');
        expect(rogue).toHaveTextContent('Deviation');
        const none = within(mix).getByTestId('outcomes-row-none-failed');
        expect(none).toHaveTextContent('No outcome reported');
        expect(none).toHaveTextContent('Not reported');
        expect(none).toHaveTextContent('Failed');
    });

    it('keeps the execution mix beside the outcomes, without a blocked state', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('outcomes');
        const exec = screen.getByTestId('signal-terminal-states');
        expect(exec).toHaveTextContent('Completed');
        expect(exec).toHaveTextContent('Errored');
        expect(exec).not.toHaveTextContent(/blocked/i);
    });

    it('discloses partial coverage above the numbers it qualifies', async () => {
        mockFetchStats.mockResolvedValue(
            baseStats({ observed_pct: 0.6, chain_integrity_pct: 0.5, asserted_pct: 0.1 })
        );
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        const warning = await screen.findByTestId('chain-integrity-warning');
        expect(warning).toHaveTextContent('60.0%');
    });

    it('does not nag about chain integrity when everything is intact', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('cost');
        expect(screen.queryByTestId('chain-integrity-warning')).not.toBeInTheDocument();
    });

    it('renders the action histogram including the open-ended top bucket', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('trajectory');
        const hist = await screen.findByTestId('action-histogram');
        expect(hist).toHaveTextContent('1–2');
        expect(hist).toHaveTextContent('101+');
    });

    it('changes the period and refetches', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchStats).toHaveBeenCalledTimes(1));

        fireEvent.click(screen.getByText('7d'));
        await waitFor(() => {
            expect(mockFetchStats).toHaveBeenLastCalledWith('sys-1', 168);
        });
    });

    it('passes the execution filter through to the run list, without a blocked state', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalledTimes(1));
        openRuns();

        const select = screen.getByLabelText('Filter by execution');
        expect(within(select).queryByText('Blocked by policy')).toBeNull();
        fireEvent.change(select, {
            target: { value: 'errored' },
        });
        await waitFor(() => {
            expect(mockFetchRuns).toHaveBeenLastCalledWith(
                'sys-1',
                expect.objectContaining({ terminal_state: 'errored' })
            );
        });
    });

    it('passes the run class filter through to the run list', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalledTimes(1));
        openRuns();
        const select = screen.getByLabelText('Filter by run class');
        expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
            'All run classes', 'Success', 'No work', 'Deviation', 'Failed', 'Not decisive',
        ]);
        fireEvent.change(select, { target: { value: 'deviation' } });
        await waitFor(() => {
            expect(mockFetchRuns).toHaveBeenLastCalledWith('sys-1', expect.objectContaining({ run_class: 'deviation' }));
        });
    });

    it('shows each run’s outcome, conformance, class and error cause', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [
                run,
                {
                    ...run, id: 'run-poll', outcome_id: 'no_applications_available', outcome_conformance: 'declared',
                    outcome_counts_toward_baseline: false, run_class: 'no_work',
                },
                {
                    ...run, id: 'run-err', terminal_state: 'errored', outcome_id: null, outcome_conformance: 'unreported',
                    outcome_counts_toward_baseline: null, run_class: 'failed', error_cause: 'model_unavailable',
                },
                {
                    ...run, id: 'run-dev', outcome_id: 'made_up', outcome_conformance: 'undeclared',
                    outcome_counts_toward_baseline: null, run_class: 'deviation',
                },
            ],
            total: 4, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        expect(await screen.findByTestId('run-class-run-1')).toHaveTextContent('Success');
        expect(screen.getByTestId('outcome-run-1')).toHaveTextContent('application_processed.loan_approved');
        expect(screen.getByTestId('outcome-conformance-run-1')).toHaveTextContent('Declared');
        expect(screen.getByTestId('outcome-baseline-run-1')).toHaveTextContent('counts');
        expect(screen.getByTestId('error-cause-run-1')).toHaveTextContent('—');

        const pollClass = screen.getByTestId('run-class-run-poll');
        expect(pollClass).toHaveTextContent('No work');
        expect(pollClass.className).not.toMatch(/emerald|green/);
        expect(screen.getByTestId('outcome-baseline-run-poll')).toHaveTextContent('no work');

        expect(screen.getByTestId('run-class-run-err')).toHaveTextContent('Failed');
        expect(screen.getByTestId('outcome-conformance-run-err')).toHaveTextContent('Not reported');
        expect(screen.getByTestId('error-cause-run-err')).toHaveTextContent('Model unavailable');

        expect(screen.getByTestId('run-class-run-dev')).toHaveTextContent('Deviation');
        expect(screen.getByTestId('outcome-conformance-run-dev')).toHaveTextContent('Undeclared');
        expect(screen.queryByTestId('outcome-baseline-run-dev')).toBeNull();
    });

    it('flags a run whose chain was not intact', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, id: 'run-2', chain_integrity: 'partial' }],
            total: 1,
            limit: 50,
            offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        const flag = await screen.findByTestId('chain-run-2');
        expect(flag).toHaveTextContent('partial');
    });

    it('renders an ingested run\'s chain as "external" in its own tone, not as a defect', async () => {
        // Reported evidence is a different kind of evidence, not a broken
        // chain: it gets its own colour and its own words, and it does not
        // trip the coverage warning because the run IS complete as reported.
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, id: 'run-ext', chain_integrity: 'observed_external', source: 'otel' }],
            total: 1,
            limit: 50,
            offset: 0,
        });
        mockFetchStats.mockResolvedValue(
            baseStats({ observed_pct: 1, asserted_pct: 0, external_pct: 1, chain_integrity_pct: 0 }),
        );
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        const cell = await screen.findByTestId('chain-run-ext');
        expect(cell).toHaveTextContent('external');
        expect(cell.className).toContain('text-sky-700');
        expect(cell.getAttribute('title')).toMatch(/reported by the exporter/i);
        expect(screen.queryByTestId('chain-integrity-warning')).not.toBeInTheDocument();
    });

    it('surfaces an API failure instead of an empty run table', async () => {
        mockFetchStats.mockRejectedValue(new Error('control plane unreachable'));
        mockFetchRuns.mockRejectedValue(new Error('control plane unreachable'));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByText('No runs recorded yet')).not.toBeInTheDocument();
    });

    it('explains the empty state rather than showing a bare table', async () => {
        mockFetchRuns.mockResolvedValue({ runs: [], total: 0, limit: 50, offset: 0 });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        await waitFor(() => {
            expect(screen.getByText('No runs recorded yet')).toBeInTheDocument();
        });
    });

    it('shows which principal made the run', async () => {
        // "Which agent did this" is the first question asked of an autonomous
        // action, so the answer in the payload must be on screen. A run
        // reading `end_user` on an agentic system usually means the agent
        // authenticated with a key that was never bound to its identity.
        mockFetchRuns.mockResolvedValue({
            runs: [
                { ...run, id: 'r-agent', actor_type: 'agent_identity', actor_id: 'agent-1' },
                { ...run, id: 'r-user', actor_type: 'end_user', actor_id: 'group-1' },
            ],
            total: 2, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        expect(await screen.findByTestId('actor-r-agent')).toHaveTextContent('agent');
        expect(screen.getByTestId('actor-r-user')).toHaveTextContent('end_user');
    });
    // -- signals sub-tabs, runs scroll, run details -------------------------

    it('names the tabs for what they show: Actions, not a second "Trajectory"', async () => {
        // "Trajectory" names only the per-run path inside Actions, never both
        // the action finder and the run-length statistics.
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        const tabs = within(await screen.findByTestId('runs-tabstrip')).getAllByRole('tab');
        expect(tabs.map((t) => t.textContent)).toEqual([
            'Composition', 'Lifecycle', 'Signals', 'Runs', 'Actions', 'Drift', 'Contract', 'Compliance', 'Assurance',
        ]);
    });

    it('Signals opens on Overview and shows one signal family per sub-tab', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('runs-tab-signals'));
        const tabs = within(await screen.findByTestId('signals-subtabs')).getAllByRole('tab');
        expect(tabs.map((t) => t.textContent)).toEqual(['Overview', 'Liveness', 'Cost', 'Run length', 'Errors', 'Outcomes', 'Incident windows']);
        // Overview lands first: the worst-of verdict, no per-family section.
        expect(await screen.findByTestId('signals-overview')).toBeInTheDocument();
        expect(screen.queryByTestId('signal-cost')).not.toBeInTheDocument();
        await openSignal('liveness');
        expect(screen.getByTestId('learn-cadence')).toBeInTheDocument();
        expect(screen.queryByTestId('signal-cost')).not.toBeInTheDocument();
        await openSignal('trajectory');
        expect(screen.queryByTestId('signal-cost')).not.toBeInTheDocument();
        expect(screen.queryByTestId('signal-errors')).not.toBeInTheDocument();
        await openSignal('errors');
        expect(screen.queryByTestId('signal-actions')).not.toBeInTheDocument();
    });

    it('the runs table scrolls inside a bounded box and shows the whole run id', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        const box = await screen.findByTestId('runs-scroll');
        expect(box.className).toContain('max-h-[70vh]');
        expect(box.className).toContain('overflow-y-auto');
        // Sliced to eight characters before, which hid everything past "nordica-".
        expect(screen.getByTestId(`run-id-${run.id}`)).toHaveTextContent(run.id);
    });

    it('an expanded run lists each model with its calls, errors, blocks, tokens, cost and median latency', async () => {
        mockFetchRun.mockResolvedValue({
            ...run,
            models_used: [
                { model: 'gpt-5.2', calls: 3, errors: 1, blocked: 0, tokens: 1200, cost: 0.5, p50_ms: 812.4 },
                { model: 'claude-fable-5', calls: 1, errors: 0, blocked: 1, tokens: 0, cost: 0, p50_ms: null },
            ],
            actions: [],
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        fireEvent.click(await screen.findByTestId(`run-row-${run.id}`));

        const table = within(await screen.findByTestId('model-attribution'));
        expect(table.getAllByRole('columnheader').map((th) => th.textContent)).toEqual([
            'Model', 'Calls', 'Errors', 'Blocked', 'Tokens', 'Cost', 'Median',
        ]);
        const rows = table.getAllByRole('row').slice(1).map((tr) => within(tr).getAllByRole('cell').map((td) => td.textContent));
        expect(rows[0].slice(0, 5)).toEqual(['gpt-5.2', '3', '1', '0', (1200).toLocaleString()]);
        expect(rows[0][6]).toBe('812ms');
        // A zero is a recorded zero; a model with no timed call has no median.
        expect(rows[1].slice(0, 5)).toEqual(['claude-fable-5', '1', '0', '1', '0']);
        expect(rows[1][6]).toBe('—');
        // The table says nothing about what was or was not recorded.
        expect(screen.getByTestId('model-attribution')).not.toHaveTextContent(/not recorded|before per-model/i);
    });

    it('opens a details window with every field the ledger holds, contract included', async () => {
        mockFetchRun.mockResolvedValue({
            ...run,
            contract_id: 'ctr-7',
            contract_hash: 'sha256:abc',
            models_used: [{ model: 'gpt-5.2', calls: 3, errors: 0, blocked: 0, tokens: 900, cost: 0.2, p50_ms: 640 }],
            actions: [],
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        fireEvent.click(await screen.findByTestId(`run-details-${run.id}`));
        const dialog = await screen.findByTestId('run-details-dialog');
        expect(dialog).toBeInTheDocument();
        await waitFor(() => expect(mockFetchRun).toHaveBeenCalledWith(run.id));
        expect(await screen.findByTestId('run-field-contract_id')).toHaveTextContent('ctr-7');
        expect(screen.getByTestId('run-field-contract_hash')).toHaveTextContent('sha256:abc');
        expect(screen.getByTestId('run-field-chain_integrity')).toHaveTextContent('intact');
        // Raw JSON is one click away for anything the grouping did not name.
        fireEvent.click(screen.getByTestId('run-details-raw'));
        expect(screen.getByTestId('run-details-json')).toHaveTextContent('"contract_id": "ctr-7"');
        fireEvent.click(screen.getByTestId('run-details-close'));
        expect(screen.queryByTestId('run-details-dialog')).not.toBeInTheDocument();
    });
});

describe('lifecycle guide', () => {
    it('opens the guide from the button and closes on Escape', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await screen.findByTestId('ai-system-runs');
        fireEvent.click(screen.getByTestId('lifecycle-guide-button'));
        const modal = await screen.findByTestId('lifecycle-guide-modal');
        expect(modal).toHaveTextContent('AI System Lifecycle Guide');
        // The honesty rules travel with the guide.
        expect(modal).toHaveTextContent(/absence of evidence is never green/i);
        expect(modal).toHaveTextContent(/human approval on every/i);
        fireEvent.keyDown(document, { key: 'Escape' });
        await waitFor(() => {
            expect(screen.queryByTestId('lifecycle-guide-modal')).toBeNull();
        });
    });

    it('section help markers open a popover on click, not hover', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="lifecycle" />);
        await screen.findByTestId('ai-system-runs');
        const tips = screen.getAllByTestId('help-tip');
        expect(tips.length).toBeGreaterThan(0);
        fireEvent.click(tips[0]);
        expect(await screen.findByTestId('help-tip-popover')).toBeInTheDocument();
    });
});

describe('AISystemRunsTab — served calls (shared service)', () => {
    const call = {
        segment_id: 'seg-1', run_id: 'run-01M61N3SERVED', root_task_id: 'task-1',
        root_system_id: 'sys-root', root_system_name: 'Brutor Demo System',
        caller_system_id: 'sys-root', caller_system_name: 'Brutor Demo System',
        first_action_at: '2026-10-28T16:22:36Z', last_action_at: '2026-10-28T16:22:36Z',
        duration_ms: 1900, action_count: 1, llm_call_count: 1, tool_call_count: 0,
        total_tokens: 282, input_tokens: 210, output_tokens: 72, cost_usd: 0.00082,
        models: ['gpt-5.2'], outcome: 'answered', outcome_id: 'screening_completed.clear',
        outcome_conformance: 'declared', error_cause: null, a2a_task_state: 'completed',
        agent_impl_name: 'fraud-screener', agent_impl_version: '0.2.0',
        guardrail_block_count: 0, policy_denial_count: 0,
    };

    beforeEach(() => {
        vi.clearAllMocks();
        mockIncidentWindows.mockResolvedValue({ windows: [], total: 0 });
        mockFetchStats.mockResolvedValue(baseStats());
        mockFetchRuns.mockResolvedValue({ runs: [], total: 0, limit: 50, offset: 0 });
        mockFetchServed.mockResolvedValue({ calls: [call], total: 1, limit: 50, offset: 0 });
    });

    it('lists the calls it served instead of claiming no runs were recorded', async () => {
        // An A2A service roots no runs; its Runs tab must not read "No runs
        // recorded yet" while it answers calls every day.
        render(<AISystemRunsTab aiSystemId="sys-svc" initialSection="runs" />);
        const row = await screen.findByTestId('served-call-seg-1');
        expect(mockFetchServed).toHaveBeenCalledWith('sys-svc', expect.objectContaining({ offset: 0 }));
        expect(row).toHaveTextContent('Brutor Demo System');
        expect(row).toHaveTextContent('Answered');
        expect(row).toHaveTextContent('screening_completed.clear');
        expect(row).toHaveTextContent('gpt-5.2');
        expect(row).toHaveTextContent('in 210 · out 72');
        expect(screen.queryByText('No runs recorded yet')).not.toBeInTheDocument();
        expect(screen.getByTestId('runs-empty')).toHaveTextContent('served 1 call inside other systems’ runs');
    });

    it('opens the whole run it served', async () => {
        mockFetchRun.mockResolvedValue({ ...run, id: 'run-01M61N3SERVED' });
        render(<AISystemRunsTab aiSystemId="sys-svc" initialSection="runs" />);
        fireEvent.click(await screen.findByTestId('served-call-seg-1'));
        await waitFor(() => expect(mockFetchRun).toHaveBeenCalledWith('run-01M61N3SERVED'));
    });

    it('shows no served-calls section for a system that served none', async () => {
        mockFetchServed.mockResolvedValue({ calls: [], total: 0, limit: 50, offset: 0 });
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="runs" />);
        expect(await screen.findByText('No runs recorded yet')).toBeInTheDocument();
        expect(screen.queryByTestId('served-calls')).not.toBeInTheDocument();
    });
});

describe('AISystemRunsTab — Finished column', () => {
    it('renders the end time for a closed run', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, started_at: '2026-08-28T13:51:48Z', ended_at: '2026-08-28T13:52:10Z' }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="runs" />);
        const cell = await screen.findByTestId('run-finished');
        // Rendered in the viewer's locale, so assert on the date parts.
        expect(cell.textContent).toMatch(/2026/);
        expect(cell.textContent).not.toMatch(/still open/i);
    });

    it('says a run is still open rather than showing a dash', async () => {
        // "—" reads as missing data. An unfinished run is not missing data.
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, started_at: '2026-08-28T13:51:48Z', ended_at: null }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="runs" />);
        const cell = await screen.findByTestId('run-finished');
        expect(cell.textContent).toMatch(/still open/i);
    });

    it('splits time awaiting a person from the run\'s active time', async () => {
        // A held call approved 11 h later: the elapsed figure alone reads as
        // an 11-hour run, which the duration ceiling does not measure.
        mockFetchRuns.mockResolvedValue({
            runs: [{
                ...run,
                started_at: '2026-08-28T01:00:00Z',
                ended_at: '2026-08-28T12:33:00Z',
                duration_ms: 41_000 + 41_520_000,
                active_duration_ms: 41_000,
                awaiting_human_ms: 41_520_000,
            }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="runs" />);
        const timing = await screen.findByTestId('run-timing');
        expect(timing.textContent).toBe('active 41 s · awaited a person 11 h 32 m');
    });

    it('keeps Finished immediately after Started', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [{ ...run, started_at: '2026-08-28T13:51:48Z', ended_at: '2026-08-28T13:52:10Z' }],
            total: 1,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" initialSection="runs" />);
        await screen.findByTestId('run-finished');
        const headers = Array.from(document.querySelectorAll('thead th')).map(
            (th) => th.textContent?.trim(),
        );
        const started = headers.indexOf('Started');
        expect(started).toBeGreaterThanOrEqual(0);
        expect(headers[started + 1]).toBe('Finished');
    });
});


describe('AISystemRunsTab — delegation subject', () => {
    it('shows who an agent acted for, next to the agent chip', async () => {
        mockFetchRuns.mockResolvedValue({
            runs: [
                { ...run, id: 'r-for', actor_type: 'agent_identity', actor_id: 'agent-1',
                  subject_id: '42', subject_kind: 'user' },
                { ...run, id: 'r-none', actor_type: 'agent_identity', actor_id: 'agent-1' },
            ],
            total: 2, limit: 50, offset: 0,
        });
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await waitFor(() => expect(mockFetchRuns).toHaveBeenCalled());
        openRuns();
        expect(screen.getByTestId('subject-r-for')).toHaveTextContent('for user:42');
        // An agent with no subject is still an agent — no invented "for".
        expect(screen.queryByTestId('subject-r-none')).toBeNull();
        expect(screen.getByTestId('actor-r-none')).toHaveTextContent('agent');
    });
});

describe('AISystemRunsTab — error causes', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockIncidentWindows.mockResolvedValue({ windows: [], total: 0 });
        mockFetchTimeseries.mockResolvedValue(timeseries());
        mockFetchRuns.mockResolvedValue({ runs: [run], total: 1, limit: 50, offset: 0 });
        mockFetchOutcomes.mockRejectedValue(new Error('not found'));
    });

    it('says plainly when no run failed', async () => {
        mockFetchStats.mockResolvedValue(baseStats({ error_causes: { failed_runs: 0, by_cause: [] } }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('errors');
        expect(screen.getByTestId('errors-by-cause-empty')).toHaveTextContent('No failed runs in this range.');
    });

    it('labels every cause in the vocabulary, most frequent first, and an unknown one in words', async () => {
        const causes = [
            'blocked_by_policy', 'rate_limited', 'model_unavailable', 'model_rejected', 'tool_failed',
            'skill_failed', 'agent_unreachable', 'delegate_deviated', 'iteration_limit', 'client_reported', 'unknown',
        ];
        mockFetchStats.mockResolvedValue(baseStats({
            error_causes: {
                failed_runs: 66,
                by_cause: causes.map((cause, i) => ({ cause, runs: i + 1, share: (i + 1) / 66 })),
            },
        }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('errors');
        const rows = within(screen.getByTestId('errors-by-cause')).getAllByRole('row').slice(1);
        expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual(
            [...causes].reverse().map((c) => `errors-cause-${c}`),
        );
        expect(screen.getByTestId('errors-cause-agent_unreachable')).toHaveTextContent('Delegate agent unreachable');
        expect(screen.getByTestId('errors-cause-iteration_limit')).toHaveTextContent('Iteration limit reached');
        expect(screen.getByTestId('errors-cause-unknown')).toHaveTextContent('Cause not recorded');
        // The table still renders without a catalogue (labels are the ids).
    });

    it('omits the cause table when the server sends no cause signal', async () => {
        mockFetchStats.mockResolvedValue(baseStats({ error_causes: undefined }));
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        await openSignal('errors');
        expect(screen.queryByTestId('errors-by-cause')).toBeNull();
    });
});

/**
 * The run overlay's way in from the Runs tab: "Show on graph" on a run row
 * and in Run details opens Composition → Simple graph with that run applied.
 * Routed (the app passes setActiveTab) it goes through the route
 * `ai-systems:{id}:composition:{runId}`, so the view is deep-linkable.
 */
describe('AISystemRunsTab — Show on graph (the run overlay)', () => {
    const detail = { ...OVERLAY_RUN, id: 'run-1', ai_system_id: 'sys-1' };

    beforeEach(() => {
        vi.clearAllMocks();
        mockIncidentWindows.mockResolvedValue({ windows: [], total: 0 });
        mockFetchStats.mockResolvedValue(baseStats());
        mockFetchTimeseries.mockResolvedValue(timeseries());
        mockFetchRuns.mockResolvedValue({ runs: [run], total: 1, limit: 50, offset: 0 });
        mockFetchRun.mockResolvedValue(detail);
    });

    it('a run row opens the Composition graph with that run applied', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        openRuns();
        fireEvent.click(await screen.findByTestId('run-show-on-graph-run-1'));
        await screen.findByTestId('run-sequence');
        expect(screen.getByTestId('composition-simple-graph')).toHaveAttribute('data-run', 'run-1');
        expect(mockFetchRun).toHaveBeenCalledWith('run-1');
    });

    it('Run details opens it too', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        openRuns();
        fireEvent.click(await screen.findByTestId('run-details-run-1'));
        fireEvent.click(await screen.findByTestId('run-details-show-on-graph'));
        await screen.findByTestId('run-sequence');
        expect(screen.getByTestId('composition-simple-graph')).toHaveAttribute('data-run', 'run-1');
        expect(screen.queryByTestId('run-details-dialog')).toBeNull();
    });

    it('routed: the click goes through the deep link, and the route applies the run', async () => {
        const setActiveTab = vi.fn();
        const { unmount } = render(<AISystemRunsTab aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        openRuns();
        fireEvent.click(await screen.findByTestId('run-show-on-graph-run-1'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-1:composition:run-1');
        unmount();

        render(<AISystemRunsTab aiSystemId="sys-1" setActiveTab={setActiveTab} initialSection="composition" routeRunId="run-1" />);
        await screen.findByTestId('run-sequence');
        expect(screen.getByTestId('composition-simple-graph')).toHaveAttribute('data-run', 'run-1');
        fireEvent.click(screen.getByTestId('run-picker-clear'));
        expect(setActiveTab).toHaveBeenLastCalledWith('ai-systems:sys-1:composition');
    });

    it('the Runs timeline numbers actions exactly as the overlay does (5a under 5)', async () => {
        render(<AISystemRunsTab aiSystemId="sys-1" />);
        openRuns();
        fireEvent.click(await screen.findByTestId('run-row-run-1'));
        const timeline = await screen.findByTestId('action-timeline');
        const numbers = within(timeline)
            .getAllByTestId(/^action-timeline-/)
            .map((li) => li.getAttribute('data-testid')?.replace('action-timeline-', ''));
        expect(numbers).toEqual(['1', '2', '3', '4', '5', '5a', '5b', '6', '7', '8', '9']);
        expect(screen.getByTestId('action-timeline-5a')).toHaveStyle({ paddingLeft: '16px' });
    });
});
