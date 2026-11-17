/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Overview: readiness per system for the selected framework (profile
 * declared / sealed, obligations in force and upcoming, statements by
 * basis — never summed), the dated timeline, undetermined declarations as
 * a to-do list, witness health, record lag and capture gaps. Counts, never
 * verdicts.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const getEvidenceSummary = vi.fn();
const getComplianceOverview = vi.fn();

const gateway = { gatewayClient: { getEvidenceSummary, getComplianceOverview } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import OverviewPage from '../pages/OverviewPage';
import type { ComplianceFramework, ComplianceOverviewResponse, EvidenceSummary } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [
    { id: 'eu-ai-act', title: 'EU AI Act', enabled: true, obligation_count: 2 },
    { id: 'iso-42001', title: 'ISO/IEC 42001', enabled: true, obligation_count: 0 },
    { id: 'soc2', title: 'SOC 2', enabled: false, obligation_count: 0 },
] as unknown as ComplianceFramework[];

const SUMMARY: EvidenceSummary = {
    records_total: 1234,
    by_verdict: { executed: 1000, blocked: 200, awaiting_human: 34 },
    by_kind: { decide: 1200, fyi: 34 },
    by_surface: { llm: 1234 },
    human_disposed_count: 34,
    systems_covered: 3,
    log: {
        log_id: 'log-1',
        tree_size: 1234,
        root: 'abcdef0123456789abcdef',
        last_tree_head: { id: 'h1', tree_size: 1234, issued_at: '2026-09-19T10:00:00Z', grade: 'self_witnessed' },
        unwitnessed_heads: 2,
    },
};

const OVERVIEW = {
    framework: { id: 'eu-ai-act', title: 'EU AI Act', kind: 'regulation', jurisdiction: 'EU', reference: 'Reg 2024/1689', catalog_version: '2026-09', verified_against_instrument: false, offered: false, unavailable_reason: 'Not checked against its published text yet; not available until it has.', obligation_count: 2, not_legal_advice: 'n' },
    window: { from: '2026-08-23T00:00:00Z', to: '2026-09-22T00:00:00Z' },
    evidence: { grade: 'self_witnessed', record_lag: { p50_seconds: 1.5, max_seconds: 9.2, records: 1234 }, open_capture_gap_items: 1 },
    timeline: [
        { key: 'now', title: 'Current duty', applies_from: '2026-08-02', in_force: true },
        { key: 'later', title: 'Later duty', applies_from: '2027-12-02', in_force: false },
    ],
    systems: [
        {
            ai_system_id: 'sys-1', name: 'Copilot', risk_tier: 'high', role: 'deployer', profile_declared: true, profile_sealed: true,
            obligations_in_force: [{ key: 'now', title: 'Current duty', applies_from: '2026-08-02' }],
            obligations_upcoming: [{ key: 'later', title: 'Later duty', applies_from: '2027-12-02' }],
            undetermined: [],
            statement_counts: { worst_status: 'not_met', by_basis: { recomputed: { met: 3, measured: 4 }, judged: { met: 1, measured: 1 } } },
            capture: { unsealed_governed_actions: 0, open_capture_gap_items: 0 },
            latest_report: { cadence: 'monthly', period_start: '2026-08-01', status: 'ready' },
        },
        {
            ai_system_id: 'sys-2', name: 'Triage', risk_tier: null, role: null, profile_declared: true, profile_sealed: false,
            obligations_in_force: [], obligations_upcoming: [],
            undetermined: [{ key: 'now', title: 'Current duty', applies_from: '2026-08-02', reason: 'regulated role not declared' }],
            statement_counts: { worst_status: null, by_basis: {} },
            capture: { unsealed_governed_actions: null, open_capture_gap_items: 2 },
            latest_report: null,
        },
    ],
} as unknown as ComplianceOverviewResponse;

const props = (framework = 'eu-ai-act') => ({
    framework,
    frameworks: FRAMEWORKS,
    registry: FRAMEWORKS,
    refreshFrameworks: vi.fn(),
    onTabChange: vi.fn(),
    setActiveTab: vi.fn(),
});

beforeEach(() => {
    vi.clearAllMocks();
    getEvidenceSummary.mockResolvedValue(SUMMARY);
    getComplianceOverview.mockResolvedValue(OVERVIEW);
});

describe('OverviewPage', () => {
    it('shows the loading state first', () => {
        getEvidenceSummary.mockReturnValue(new Promise(() => undefined));
        getComplianceOverview.mockReturnValue(new Promise(() => undefined));
        render(<OverviewPage {...props()} />);
        expect(screen.getByTestId('overview-loading')).toBeInTheDocument();
    });

    it('renders the evidence cards with the head grade vocabulary, lag and capture gaps', async () => {
        render(<OverviewPage {...props()} />);
        await screen.findByTestId('compliance-overview');
        expect(screen.getByTestId('overview-grade')).toHaveTextContent('latest head self-witnessed');
        expect(screen.getByTestId('overview-unwitnessed')).toHaveTextContent('2 unwitnessed heads');
        expect(screen.getByTestId('overview-lag')).toHaveTextContent('1.5 s');
        expect(screen.getByTestId('card-lag')).toHaveTextContent('max 9.2 s · 1,234 records');
        expect(screen.getByTestId('overview-capture-gaps')).toHaveTextContent('1');
        expect(screen.getByTestId('card-frameworks')).toHaveTextContent('2 of 3 enabled');
        const [fw, window] = getComplianceOverview.mock.calls[0] as [string, { from: string; to: string }];
        expect(fw).toBe('eu-ai-act');
        expect(typeof window.from).toBe('string');
        expect(typeof window.to).toBe('string');
    });

    it('links to the registry and the inbox', async () => {
        const p = props();
        render(<OverviewPage {...p} />);
        await screen.findByTestId('compliance-overview');
        fireEvent.click(screen.getByTestId('overview-frameworks-link'));
        expect(p.onTabChange).toHaveBeenCalledWith('compliance-frameworks');
        fireEvent.click(screen.getByText('Open the inbox →'));
        expect(p.setActiveTab).toHaveBeenCalledWith('inbox');
    });

    it('shows readiness per system with bases side by side, never summed, and never a verdict', async () => {
        render(<OverviewPage {...props()} />);
        await screen.findByTestId('overview-readiness');
        const copilot = screen.getByTestId('readiness-sys-1');
        expect(screen.getByTestId('readiness-profile-sys-1')).toHaveTextContent('declared · sealed');
        expect(screen.getByTestId('readiness-statements-sys-1')).toHaveTextContent('not met');
        expect(screen.getByTestId('readiness-statements-sys-1')).toHaveTextContent('recomputed: 3 of 4 met · judged: 1 of 1 met');
        expect(copilot).toHaveTextContent('none unsealed');
        expect(copilot).toHaveTextContent('monthly 2026-08-01 · ready');
        expect(copilot.textContent).not.toMatch(/%/);
        const triage = screen.getByTestId('readiness-sys-2');
        expect(screen.getByTestId('readiness-profile-sys-2')).toHaveTextContent('declared · not sealed');
        expect(triage).toHaveTextContent('classification not declared · role not declared');
        expect(triage).toHaveTextContent('not evaluable');
        expect(triage).toHaveTextContent('2 gap item(s)');
        expect(triage).toHaveTextContent('none yet');
        // Only offered frameworks reach the Overview, so there is no unverified banner.
        expect(screen.queryByTestId('overview-unverified-banner')).toBeNull();
    });

    it('lists undetermined declarations as a to-do and the dated timeline', async () => {
        const p = props();
        render(<OverviewPage {...p} />);
        await screen.findByTestId('overview-timeline');
        expect(screen.getByTestId('undetermined-sys-2-now')).toHaveTextContent('regulated role not declared');
        fireEvent.click(within(screen.getByTestId('undetermined-sys-2-now')).getByText('Declare →'));
        expect(p.onTabChange).toHaveBeenCalledWith('compliance-profiles');
        const rows = within(screen.getByTestId('overview-timeline')).getAllByRole('listitem');
        expect(rows[0]).toHaveTextContent('2026-08-02');
        expect(rows[0]).toHaveTextContent('in force');
        expect(rows[1]).toHaveTextContent('2027-12-02');
        expect(rows[1]).toHaveTextContent('upcoming');
    });

    it('does not compute an overview for a framework without a catalog', async () => {
        render(<OverviewPage {...props('iso-42001')} />);
        await screen.findByTestId('compliance-overview');
        expect(getComplianceOverview).not.toHaveBeenCalled();
        expect(screen.getByTestId('overview-no-catalog')).toHaveTextContent('has no obligation catalog');
    });

    it('renders the empty log and the two failures independently', async () => {
        getEvidenceSummary.mockResolvedValueOnce({ ...SUMMARY, log: null });
        getComplianceOverview.mockResolvedValueOnce({ ...OVERVIEW, evidence: { ...OVERVIEW.evidence, grade: null } });
        const { unmount } = render(<OverviewPage {...props()} />);
        await screen.findByTestId('compliance-overview');
        expect(screen.getByTestId('overview-unwitnessed')).toHaveTextContent('no tenant log yet');
        unmount();

        getEvidenceSummary.mockRejectedValueOnce(new Error('summary down'));
        getComplianceOverview.mockRejectedValueOnce(new Error('overview down'));
        render(<OverviewPage {...props()} />);
        expect(await screen.findByTestId('overview-summary-error')).toHaveTextContent('summary down');
        expect(screen.getByTestId('overview-error')).toHaveTextContent('overview down');
    });
});

// Under a composite-classification framework a system is
// evaluated at its EFFECTIVE tier; readiness shows it with the declared tier
// and the systems that lifted it, and the framework says so.
describe('OverviewPage — composite classification', () => {
    const lifted = {
        ...OVERVIEW,
        framework: { ...OVERVIEW.framework, composite_classification: true },
        systems: [
            { ...OVERVIEW.systems[0], risk_tier: 'high', risk_tier_declared: 'high', effective_tier_sources: [] },
            {
                ...OVERVIEW.systems[1],
                risk_tier: 'high',
                risk_tier_declared: 'minimal',
                effective_tier_sources: [{ system_id: 'sys-1', system_name: 'Copilot', tier: 'high' }],
            },
        ],
    } as unknown as ComplianceOverviewResponse;

    it('shows the evaluated tier against the declared one, with its source', async () => {
        getComplianceOverview.mockResolvedValue(lifted);
        render(<OverviewPage {...props()} />);
        const tier = await screen.findByTestId('readiness-tier-sys-2');
        expect(tier).toHaveAttribute('data-lifted', 'true');
        expect(tier).toHaveTextContent('evaluated high (declared minimal, via Copilot)');
        expect(screen.getByTestId('readiness-tier-sys-1')).toHaveAttribute('data-lifted', 'false');
        expect(screen.getByTestId('readiness-tier-sys-1')).toHaveTextContent('high');
    });

    it('notes composite classification only for a framework that uses it', async () => {
        getComplianceOverview.mockResolvedValue(lifted);
        render(<OverviewPage {...props()} />);
        expect(await screen.findByTestId('overview-composite-classification')).toHaveTextContent('effective');
    });

    it('a framework without it shows no note', async () => {
        render(<OverviewPage {...props()} />);
        await screen.findByTestId('readiness-tier-sys-1');
        expect(screen.queryByTestId('overview-composite-classification')).toBeNull();
    });
});
