/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for AssurancePanel.
 *
 * The properties worth defending are all about not flattering the system:
 *
 *   - only `healthy` renders green; `learning` and `unknown` do not;
 *   - a missing score shows as "—", never as zero or a full bar;
 *   - the score is always decomposable into its five components;
 *   - a component that was not measured says so rather than showing 0%;
 *   - `not_covered` is rendered, not tucked behind a link.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockHealth = vi.fn();
const mockReport = vi.fn();
const mockCapture = vi.fn();
const mockHistory = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchSystemHealth: mockHealth,
        fetchAssuranceReport: mockReport,
        captureAssuranceReport: mockCapture,
        fetchAssuranceHistory: mockHistory,
    }),
}));

import AssurancePanel from '../AssurancePanel';
import type { SystemHealth } from '../../../lib/aiSystemTypes';

function health(overrides: Partial<SystemHealth> = {}): SystemHealth {
    return {
        ai_system_id: 'sys-1',
        health_state: 'healthy',
        health_score: 1.0,
        liveness_score: 1.0,
        behaviour_score: 1.0,
        reliability_score: 1.0,
        cost_score: null,
        conformance_score: 1.0,
        chain_integrity_pct: 1.0,
        baseline_state: 'ready',
        runs_24h: 120,
        open_drift_events: 0,
        reasons: [],
        is_trustworthy: true,
        ...overrides,
    };
}

describe('AssurancePanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockHealth.mockResolvedValue(health());
        mockReport.mockResolvedValue({ not_covered: [] });
        mockCapture.mockResolvedValue({});
        mockHistory.mockResolvedValue({ reports: [], total: 0 });
    });

    // -- the score --------------------------------------------------------

    it('shows the worst-of score as a percentage', async () => {
        mockHealth.mockResolvedValue(health({ health_score: 0.42, health_state: 'degraded' }));
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-score')).toHaveTextContent('42%');
    });

    it('shows a dash rather than a zero when there is no score', async () => {
        // Zero would render as a fault; a full bar would render as a clean
        // bill. Both would be inventions.
        mockHealth.mockResolvedValue(
            health({ health_state: 'unknown', health_score: null })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-score')).toHaveTextContent('—');
    });

    it('always shows the five components', async () => {
        // A score nobody can decompose is a score nobody trusts.
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('health-components');
        for (const key of [
            'liveness_score',
            'behaviour_score',
            'reliability_score',
            'cost_score',
            'conformance_score',
            // The sixth signal must decompose alongside the rest.
            'oversight_score',
        ]) {
            expect(screen.getByTestId(`component-${key}`)).toBeInTheDocument();
        }
    });

    it('shows oversight as not measured when no approval gate was exercised', async () => {
        // The whole point of the null-not-zero rule: a system with no gates
        // must not read as badly overseen.
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('component-oversight_score')).toHaveTextContent(
            'not measured'
        );
        expect(screen.getByTestId('component-oversight_score')).not.toHaveTextContent('0%');
    });

    // -- kind-aware signal profiles ---------------------------------------

    const assistant = () =>
        health({
            system_kind: 'assistant',
            liveness_score: 0.0,
            behaviour_score: 0.4,
            signal_levels: {
                liveness: { score: null, level: 'not_applicable' },
                behaviour: { score: 0.4, level: 'advisory' },
                reliability: { score: 1.0, level: 'enforced' },
                cost: { score: null, level: 'enforced' },
                conformance: { score: 1.0, level: 'enforced' },
                oversight: { score: null, level: 'enforced' },
            },
            profile_relaxations: [
                {
                    signal: 'liveness',
                    level: 'not_applicable',
                    reason: 'Liveness is not scored for an assistant: usage is human and bursty.',
                },
                {
                    signal: 'behaviour',
                    level: 'advisory',
                    reason: 'Behavioural drift is advisory for an assistant.',
                },
            ],
        });

    it('renders a not-applicable signal as n/a, never as a score', async () => {
        // The failure this guards: a relaxed signal rendering as 0% reads as
        // a failing system, and rendering as 100% reads as a passing one.
        // Both are claims the platform did not make.
        mockHealth.mockResolvedValue(assistant());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const el = await screen.findByTestId('component-liveness_score');
        expect(el).toHaveTextContent('n/a');
        expect(el).not.toHaveTextContent('0%');
        expect(el).not.toHaveTextContent('100%');
    });

    it('badges an advisory signal while still showing its number', async () => {
        mockHealth.mockResolvedValue(assistant());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('component-behaviour_score-advisory')).toBeInTheDocument();
        expect(screen.getByTestId('component-behaviour_score')).toHaveTextContent('40%');
    });

    it('discloses which signals the kind relaxed', async () => {
        // Anti-laundering: if picking a kind could quietly drop a signal,
        // `system_kind` would be a dropdown for hiding a bad score.
        mockHealth.mockResolvedValue(assistant());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const box = await screen.findByTestId('profile-relaxations');
        expect(box).toHaveTextContent('relaxes 2 of 6 signals');
        expect(box).toHaveTextContent('human and bursty');
    });

    it('shows no relaxation notice for a fully enforced kind', async () => {
        // Default fixture has no profile_relaxations.
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('health-components');
        expect(screen.queryByTestId('profile-relaxations')).not.toBeInTheDocument();
    });

    it('does not badge enforced signals as advisory', async () => {
        mockHealth.mockResolvedValue(assistant());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('health-components');
        expect(
            screen.queryByTestId('component-conformance_score-advisory')
        ).not.toBeInTheDocument();
    });

    // -- the ladder as a workflow -----------------------------------------

    const withLadder = (over = {}) => ({
        not_covered: [],
        assurance_summary: {
            dimensions: {},
            level: {
            level: 1,
            scale: 'Evidence maturity',
            label: 'Baseline',
            means: 'Behaviour is judged against learned normal.',
            next: {
                level: 2,
                label: 'Contract',
                means: 'An approved envelope is in force.',
                requires: 'An approved operating envelope in force.',
                blocker: 'No approved operating envelope is in force.',
                status: 'missing',
                dimension: 'contract_assurance',
                action: 'Declare an operating envelope and have it signed off.',
                surface: 'contract',
            },
            rungs: [
                { level: 0, label: 'Observable', held: true, requires: 'Runs observed.', dimension: 'operational_evidence' },
                { level: 1, label: 'Baseline', held: true, requires: 'Learned baselines.', dimension: 'behavioural_stability' },
                { level: 2, label: 'Contract', held: false, requires: 'An approved envelope.', dimension: 'contract_assurance' },
                { level: 3, label: 'Execution', held: true, requires: 'Signed chains.', dimension: 'execution_evidence' },
                { level: 4, label: 'Independent', held: false, requires: 'Independent evidence.', dimension: 'independent_evidence' },
            ],
                ...over,
            },
        },
    });

    it('renders the whole ladder as a checklist, not just the level', async () => {
        mockReport.mockResolvedValue(withLadder());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const list = await screen.findByTestId('assurance-rungs');
        expect(list).toBeInTheDocument();
        for (const lvl of [0, 1, 2, 3, 4]) {
            expect(screen.getByTestId(`rung-${lvl}`)).toBeInTheDocument();
        }
    });

    it('shows a held rung above a broken one as still held', async () => {
        // Cumulative scoring means L3 does not raise the level while L2 is
        // unmet — but hiding the work makes operators redo it.
        mockReport.mockResolvedValue(withLadder());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('rung-3')).toHaveTextContent('✓');
        expect(screen.getByTestId('rung-2')).toHaveTextContent('○');
    });

    it('names what is blocking the next rung and what to do about it', async () => {
        // The whole point of the ladder UX: a score an operator cannot move
        // is a score they learn to ignore.
        mockReport.mockResolvedValue(withLadder());
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const step = await screen.findByTestId('assurance-next-step');
        expect(step).toHaveTextContent('To reach L2');
        expect(step).toHaveTextContent('No approved operating envelope is in force.');
        expect(step).toHaveTextContent('Declare an operating envelope');
    });

    it('hides the next-step box for a fully assured system', async () => {
        mockReport.mockResolvedValue(
            withLadder({ level: 4, label: 'Independent', next: null })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('maturity-detail');
        expect(screen.queryByTestId('assurance-next-step')).not.toBeInTheDocument();
    });

    it('still renders the ladder when the report carries no rung data', async () => {
        // The report schema does not require `rungs`.
        mockReport.mockResolvedValue({
            not_covered: [],
            assurance_summary: {
                dimensions: {},
                level: { level: 1, label: 'Baseline', means: 'x', next: null },
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        await screen.findByTestId('maturity-detail');
        expect(screen.queryByTestId('assurance-rungs')).not.toBeInTheDocument();
    });

    it('says a component was not measured rather than showing zero', async () => {
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('component-cost_score')).toHaveTextContent(
            'not measured'
        );
    });

    // -- states -----------------------------------------------------------

    it('renders healthy as green', async () => {
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-state')).toHaveClass('bg-green-100');
    });

    it('does not render still-learning as green', async () => {
        // The state an optimistic UI would round up.
        mockHealth.mockResolvedValue(
            health({ health_state: 'learning', reasons: ['Still learning.'] })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const badge = await screen.findByTestId('health-state');
        expect(badge).not.toHaveClass('bg-green-100');
        expect(badge).toHaveTextContent('Still learning');
    });

    it('does not render unknown as green', async () => {
        mockHealth.mockResolvedValue(
            health({ health_state: 'unknown', health_score: null })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-state')).not.toHaveClass(
            'bg-green-100'
        );
    });

    it('renders a suspended system in red', async () => {
        mockHealth.mockResolvedValue(health({ health_state: 'suspended' }));
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-state')).toHaveClass('bg-red-100');
    });

    it('shows why the state is what it is', async () => {
        mockHealth.mockResolvedValue(
            health({
                health_state: 'drifting',
                reasons: ['2 open behavioural findings.'],
            })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('health-reason')).toHaveTextContent(
            '2 open behavioural findings'
        );
    });

    // -- trust ------------------------------------------------------------

    it('shows chain integrity beside the score', async () => {
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('chain-integrity')).toHaveTextContent(
            '100% of runs observed intact'
        );
    });

    it('flags integrity that undermines the score', async () => {
        // If only 40% of runs were observed intact, the customer must know
        // before deciding what the number is worth.
        mockHealth.mockResolvedValue(
            health({ chain_integrity_pct: 0.4, is_trustworthy: false })
        );
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const el = await screen.findByTestId('chain-integrity');
        expect(el).toHaveTextContent('40%');
        expect(el).toHaveClass('text-amber-800');
    });

    // -- the report -------------------------------------------------------

    it('renders what the report does not cover', async () => {
        // The section that makes the rest credible — not hidden behind a
        // disclaimer link.
        mockReport.mockResolvedValue({
            not_covered: [
                'Behavioural change is detected; correctness is not evaluated.',
                'Covers traffic routed through the gateway.',
            ],
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const block = await screen.findByTestId('not-covered');
        expect(block).toHaveTextContent('correctness is not evaluated');
        expect(block).toHaveTextContent('through the gateway');
    });

    // -- the executive layer (report spec 1.2) ----------------------------

    it('leads with the assurance status and the three executive rows', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            assurance_summary: {
                conclusion:
                    'Claims Triage Agent operated within its observed historical behaviour during the reporting period, but formal assurance is incomplete. Remain under monitored operation until the outstanding assurance requirements are completed.',
                status: { code: 'conditionally_assured', label: 'Conditionally assured' },
                what_we_know:
                    'Claims Triage Agent operated within its observed historical behaviour during the reporting period, but formal assurance is incomplete.',
                what_prevents_full_assurance: [
                    'the operating contract is not approved',
                    '49% of runs lack cryptographically signed execution chains',
                ],
                recommended_action:
                    'Remain under monitored operation until the outstanding assurance requirements are completed.',
                why_it_matters:
                    'No evidence of abnormal behaviour was detected during the reporting period. However, it cannot yet be established that the system remains within an approved operating boundary.',
                level: {
                    level: 1,
                    label: 'Baseline established',
                    means: 'The system’s normal operating behaviour has been learned.',
                    next: { level: 2, label: 'Controlled', means: 'An approved contract binds the system; enforcement is active.' },
                },
                dimensions: {
                    operational_evidence: { status: 'ok', detail: '340 run(s) fully observed in the period.' },
                    behavioural_stability: { status: 'partial', detail: '3 open drift finding(s) have no attributed cause.' },
                    contract_assurance: { status: 'missing', detail: 'No approved operating envelope is in force.' },
                    execution_evidence: { status: 'partial', detail: 'Only 51% of runs carry gateway-signed chains.' },
                    independent_evidence: { status: 'missing', detail: 'No external evidence is attached.' },
                },
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        const block = await screen.findByTestId('assurance-conclusion');
        // The status word first — uppercase, unmistakable.
        expect(screen.getByTestId('assurance-status')).toHaveTextContent('Conditionally assured');
        expect(block).toHaveTextContent('What we know:');
        expect(screen.getByTestId('assurance-blockers')).toHaveTextContent('operating contract is not approved');
        expect(block).toHaveTextContent('Recommended action:');
        // The final translation into risk language sits with the rows.
        expect(screen.getByTestId('assurance-why')).toHaveTextContent(
            'cannot yet be established that the system remains within an approved operating boundary'
        );
        // WORD ONLY in the executive area: "CONDITIONALLY ASSURED" beside a
        // numeral 0 reads as "so it isn't really assured". The numeral
        // lives in the detail block below.
        const chip = screen.getByTestId('assurance-level');
        expect(chip).toHaveTextContent('Evidence maturity: Baseline established');
        expect(chip).not.toHaveTextContent('1');
        expect(screen.getByTestId('dimension-contract_assurance')).toHaveTextContent('Contract assurance');
        expect(screen.getByTestId('dimension-contract_assurance')).toHaveTextContent('✕');
        expect(screen.getByTestId('dimension-operational_evidence')).toHaveTextContent('✓');
        // The ladder lives on the Evidence sub-tab.
        fireEvent.click(screen.getByTestId('assurance-subtab-evidence'));
        expect(await screen.findByTestId('maturity-detail')).toHaveTextContent('1 — Baseline established');
        expect(screen.getByTestId('assurance-next-level')).toHaveTextContent('Level 2 — Controlled');
    });

    it('a 1.2 snapshot without the structured rows falls back to the paragraph', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            assurance_summary: {
                conclusion: 'Older snapshot conclusion paragraph.',
                level: { level: 0, label: 'Observable', means: 'Traffic visible.' },
                dimensions: {},
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        const block = await screen.findByTestId('assurance-conclusion');
        expect(block).toHaveTextContent('Older snapshot conclusion paragraph.');
        expect(screen.queryByTestId('assurance-exec-rows')).not.toBeInTheDocument();
    });

    it('marks a verdict measured only against observed history as conditional', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            verdict: {
                failing_dependencies: [],
                status: 'assured_with_exceptions',
                conclusion:
                    'The system remained within its observed historical behaviour during the reporting period, with exceptions on record.',
                basis: 'observed_history',
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('verdict-conditional')).toHaveTextContent(
            /conditional/i,
        );
    });

    it('marks a verdict resting only on ingested telemetry as conditional', async () => {
        // Even under a signed envelope: the gateway witnessed none of the runs,
        // so the badge cannot borrow the signature's authority.
        mockReport.mockResolvedValue({
            not_covered: [],
            verdict: {
                failing_dependencies: [],
                status: 'assured',
                conclusion:
                    'The system remained within its approved operating envelope during the reporting period. Every run in the period was ingested from external telemetry.',
                basis: 'reported_telemetry',
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('verdict-conditional')).toHaveTextContent(
            /conditional/i,
        );
    });

    it('a signed-envelope verdict carries no conditional marker', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            verdict: {
                failing_dependencies: [],
                status: 'assured',
                conclusion: 'The system remained within its approved operating envelope.',
                basis: 'signed_envelope',
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        await screen.findByTestId('verdict-banner');
        expect(screen.queryByTestId('verdict-conditional')).not.toBeInTheDocument();
    });

    // -- learned baselines (report spec 1.1) ------------------------------

    it('renders learned baselines with their values and states', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            baselines: {
                ready: 1,
                learning: 1,
                metrics: [
                    {
                        metric_key: 'cost_per_run',
                        state: 'ready',
                        sample_count: 240,
                        median: 0.04,
                        mad: 0.01,
                        p90: 0.09,
                        distribution: null,
                    },
                    {
                        metric_key: 'tool_mix',
                        state: 'learning',
                        sample_count: 12,
                        median: null,
                        mad: null,
                        p90: null,
                        distribution: { search: 0.62, fetch: 0.31 },
                    },
                ],
                sub_baselines: { seasonal: 3, per_actor: 2 },
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        const block = await screen.findByTestId('baselines-block');
        expect(screen.getByTestId('baselines-counts')).toHaveTextContent('1 ready · 1 learning');
        // Numeric baselines print median ± MAD in operator language.
        expect(screen.getByTestId('baseline-cost_per_run')).toHaveTextContent('Cost per run — $0.0400 ± $0.0100');
        // Categorical baselines print the dominant entries.
        expect(screen.getByTestId('baseline-tool_mix')).toHaveTextContent('search 62% · fetch 31%');
        expect(screen.getByTestId('baseline-tool_mix')).toHaveTextContent('learning');
        expect(screen.getByTestId('sub-baselines')).toHaveTextContent('3 seasonal and 2 per-actor');
        expect(block).toBeInTheDocument();
    });

    it('says baselines are still building rather than rendering an empty box', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            baselines: { ready: 0, learning: 0, metrics: [] },
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="evidence" />);
        expect(await screen.findByTestId('baselines-empty')).toHaveTextContent('No baselines yet');
    });

    // -- recommended controls (report spec 1.1) ---------------------------

    it('renders recommended controls with severity and reason', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            recommended_controls: [
                {
                    control: 'declare_or_block_client',
                    severity: 'high',
                    reason: 'This client called the system but is not declared.',
                    subject: 'curl-dev',
                },
                {
                    control: 'bind_replay_suite',
                    severity: 'medium',
                    reason: 'No replay suite is bound.',
                },
            ],
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="findings" />);
        const block = await screen.findByTestId('recommended-controls');
        expect(block).toBeInTheDocument();
        expect(screen.getByTestId('rec-declare_or_block_client')).toHaveTextContent('high');
        expect(screen.getByTestId('rec-declare_or_block_client')).toHaveTextContent('Declare or block client curl-dev');
        expect(screen.getByTestId('rec-bind_replay_suite')).toHaveTextContent('Bind a replay suite');
    });

    it('a fully governed system shows no recommendations block at all', async () => {
        // A section that always recommends something teaches operators to
        // ignore it — quiet must be possible.
        mockReport.mockResolvedValue({ not_covered: [], recommended_controls: [] });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="findings" />);
        await screen.findByTestId('assurance-panel');
        expect(screen.queryByTestId('recommended-controls')).not.toBeInTheDocument();
    });

    it('captures a snapshot', async () => {
        render(<AssurancePanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('capture-button'));
        await waitFor(() => expect(mockCapture).toHaveBeenCalledWith('sys-1'));
    });

    it('reloads after a capture so the history reflects it', async () => {
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="snapshots" />);
        await waitFor(() => expect(mockHistory).toHaveBeenCalledTimes(1));
        fireEvent.click(screen.getByTestId('capture-button'));
        await waitFor(() => expect(mockHistory).toHaveBeenCalledTimes(2));
    });

    it('lists captured snapshots with their contract version', async () => {
        mockHistory.mockResolvedValue({
            total: 1,
            reports: [
                {
                    id: 'asr-1',
                    ai_system_id: 'sys-1',
                    period_hours: 720,
                    trigger: 'scheduled',
                    contract_version: 4,
                    created_at: '2026-07-29T09:00:00Z',
                },
            ],
        });
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="snapshots" />);
        const row = await screen.findByTestId('snapshot-asr-1');
        expect(row).toHaveTextContent('scheduled');
        expect(row).toHaveTextContent('contract v4');
    });

    it('cannot export before the report has loaded', async () => {
        let resolveReport: (v: Record<string, unknown>) => void = () => {};
        mockReport.mockImplementation(
            () => new Promise<Record<string, unknown>>((r) => (resolveReport = r))
        );
        render(<AssurancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('export-button')).toBeDisabled();
        resolveReport({ not_covered: [] });
        await waitFor(() =>
            expect(screen.getByTestId('export-button')).not.toBeDisabled()
        );
    });

    it('surfaces an API failure instead of an empty panel', async () => {
        mockHealth.mockRejectedValue(new Error('control plane unreachable'));
        mockReport.mockRejectedValue(new Error('control plane unreachable'));
        mockHistory.mockRejectedValue(new Error('control plane unreachable'));
        render(<AssurancePanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('health-block')).not.toBeInTheDocument();
    });
});

describe('AssurancePanel — the badge carries its basis', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockHealth.mockResolvedValue(health());
        mockHistory.mockResolvedValue({ reports: [], total: 0 });
    });

    it('renders the basis note under the status badge', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            assurance_summary: {
                conclusion: 'Claims Triage Agent remained within its approved operating envelope during the reporting period.',
                status: {
                    code: 'assured',
                    label: 'Assured · operating envelope',
                    basis_note: 'Envelope conformance over observed runs. Correctness is not evaluated.',
                },
                what_we_know: 'Within envelope.',
                what_prevents_full_assurance: [],
                recommended_action: 'Continue.',
                why_it_matters: 'The approved boundary held.',
                level: {
                    level: 2,
                    label: 'Controlled',
                    means: 'An approved contract binds the system; enforcement is active.',
                    next: { level: 3, label: 'Evidence-backed', means: 'Runs carry signed chains and a replay suite passed.' },
                },
                dimensions: {
                    operational_evidence: { status: 'ok', detail: '120 run(s) fully observed.' },
                    behavioural_stability: { status: 'ok', detail: 'No open drift.' },
                    contract_assurance: { status: 'ok', detail: 'Approved envelope in force.' },
                    execution_evidence: { status: 'partial', detail: '80% signed chains.' },
                    independent_evidence: { status: 'missing', detail: 'No external evidence is attached.' },
                },
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('assurance-status')).toHaveTextContent('operating envelope');
        expect(screen.getByTestId('assurance-status-basis')).toHaveTextContent('Correctness is not evaluated');
    });
});

describe('AssurancePanel — sub-tabs and the assurance loop', () => {
    it('opens on Conclusion with the loop diagram, and hides the evidence blocks until asked', async () => {
        render(<AssurancePanel aiSystemId="sys-1" />);
        await screen.findByTestId('assurance-conclusion');
        expect(screen.getByTestId('assurance-subtab-conclusion')).toHaveAttribute('aria-selected', 'true');
        expect(screen.getByTestId('assurance-lifecycle-diagram')).toBeInTheDocument();
        expect(screen.queryByTestId('health-block')).not.toBeInTheDocument();
        expect(screen.queryByTestId('baselines-block')).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId('assurance-subtab-evidence'));
        expect(await screen.findByTestId('health-block')).toBeInTheDocument();
        expect(screen.queryByTestId('assurance-conclusion')).not.toBeInTheDocument();
    });

    it('says where the system is in the loop, from the report facts', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            doing_its_job: { runs: { total_runs: 12 } },
            baselines: { ready: 3, learning: 1, metrics: [] },
            verdict: { status: 'assured', basis: 'signed_envelope', conclusion: 'ok', failing_dependencies: [] },
            human_review: { available: true, open: 0, raised: 2 },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        const diagram = await screen.findByTestId('assurance-lifecycle-diagram');
        // Nothing has been captured yet, so the report is the first
        // unsatisfied node — every earlier node reads done, with its number.
        expect(diagram).toHaveAttribute('data-here', 'report');
        expect(screen.getByTestId('loop-evidence')).toHaveAttribute('data-state', 'done');
        expect(screen.getByTestId('loop-evidence')).toHaveTextContent('12 runs in the ledger');
        expect(screen.getByTestId('loop-normal')).toHaveTextContent('3 baselines ready, 1 learning');
        expect(screen.getByTestId('loop-review')).toHaveTextContent('2 raised, all decided');
        expect(screen.getByTestId('loop-report')).toHaveAttribute('data-state', 'here');
    });

    it('"N findings open" is a button that takes the person to the Inbox', async () => {
        const setActiveTab = vi.fn();
        mockReport.mockResolvedValue({
            not_covered: [],
            doing_its_job: { runs: { total_runs: 12 } },
            baselines: { ready: 3, learning: 0, metrics: [] },
            verdict: { status: 'assured_with_exceptions', basis: 'signed_envelope', conclusion: 'ok', failing_dependencies: [] },
            human_review: { available: true, open: 2, raised: 5 },
        });
        render(<AssurancePanel aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        const btn = await screen.findByTestId('loop-review-open');
        expect(btn).toHaveTextContent('2 findings open');
        fireEvent.click(btn);
        expect(setActiveTab).toHaveBeenCalledWith('inbox');
    });

    it('stops at learned normal while the baselines are still learning', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            doing_its_job: { runs: { total_runs: 5 } },
            baselines: { ready: 0, learning: 4, metrics: [] },
            verdict: { status: 'insufficient_evidence', basis: 'observed_history', conclusion: 'thin', failing_dependencies: [] },
            human_review: { available: true, open: 1, raised: 1 },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        const diagram = await screen.findByTestId('assurance-lifecycle-diagram');
        expect(diagram).toHaveAttribute('data-here', 'normal');
        expect(screen.getByTestId('loop-normal')).toHaveTextContent('4 baselines still learning');
        expect(screen.getByTestId('loop-verdict')).toHaveAttribute('data-state', 'ahead');
    });

    it('the Snapshots sub-tab explains an empty history instead of showing nothing', async () => {
        render(<AssurancePanel aiSystemId="sys-1" initialSubTab="snapshots" />);
        expect(await screen.findByTestId('assurance-history-empty')).toHaveTextContent(/capture one/i);
    });
});

describe('AssurancePanel — envelope terms that are enforced and not graded', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockHealth.mockResolvedValue(health());
        mockHistory.mockResolvedValue({ reports: [], total: 0 });
    });

    it('shows a not_graded term as enforced, not graded, apart from met and breached', async () => {
        mockReport.mockResolvedValue({
            not_covered: [],
            contract_compliance: {
                under_signature: true,
                envelope: {
                    terms: [
                        { term: 'max_cost_per_run_usd', label: 'Cost ceiling for one run', status: 'met' },
                        {
                            term: 'max_new_runs_per_window',
                            label: 'New runs per credential in a window',
                            status: 'not_graded',
                            detail: 'enforced at the gateway on every call; no worst case is recorded to grade it against',
                        },
                        { term: 'min_completion_rate', label: 'Tasks that complete', status: 'insufficient_evidence' },
                    ],
                },
            },
        });
        render(<AssurancePanel aiSystemId="sys-1" />);
        const notGraded = await screen.findByTestId('term-max_new_runs_per_window');
        expect(notGraded).toHaveTextContent('enforced, not graded');
        expect(notGraded).toHaveTextContent('no worst case is recorded to grade it against');
        expect(notGraded).not.toHaveTextContent('not_graded');
        expect(screen.getByTestId('term-max_cost_per_run_usd')).toHaveTextContent('met');
        expect(screen.getByTestId('term-min_completion_rate')).toHaveTextContent('insufficient evidence');
    });
});
