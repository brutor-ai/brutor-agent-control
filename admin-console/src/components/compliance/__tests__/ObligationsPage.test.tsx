/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Obligations: the AI System selector (one system's
 * obligations grouped in force / upcoming / not applicable, evidence opening
 * under the row), the all-systems board (the default) and the system ×
 * obligation matrix with the worst status and `n of m` per cell, the
 * drill-in (basis chip, status, n of m, limit, catalog version, citations →
 * record dialog, the judged-row sentence), "show this evidence under other
 * frameworks", the window picker, and the honest empty state for a
 * framework without a catalog.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const getComplianceObligations = vi.fn();
const getComplianceObligationDetail = vi.fn();

const gateway = { gatewayClient: { getComplianceObligations, getComplianceObligationDetail } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string; onClose: () => void }) => (
        <div data-testid="record-dialog-stub">
            {p.recordId}
            <button onClick={p.onClose}>close</button>
        </div>
    ),
}));

import ObligationsPage from '../pages/ObligationsPage';
import type { ComplianceFramework, ComplianceObligationsResponse, ObligationDetailResponse } from '../../../lib/complianceEvidenceTypes';

const FRAMEWORKS = [
    { id: 'eu-ai-act', title: 'EU AI Act', reference: 'Regulation (EU) 2024/1689', catalog_version: '2026.09', obligation_count: 2 },
    { id: 'iso-42001', title: 'ISO/IEC 42001', reference: 'ISO/IEC 42001:2023', catalog_version: '2023', obligation_count: 5 },
    { id: 'soc2', title: 'SOC 2', reference: 'AICPA TSC 2017', catalog_version: 'none', obligation_count: 0 },
] as unknown as ComplianceFramework[];

const BOARD = {
    framework: { id: 'eu-ai-act', title: 'EU AI Act', jurisdiction: 'EU', reference: 'Regulation (EU) 2024/1689', obligation_count: 2 },
    as_of: '2026-09-19',
    system_count: 2,
    obligations: [
        {
            key: 'art50-transparency', title: 'Transparency obligations', reference: 'Art 50', summary: 'Users must be informed…',
            applies_from: '2026-08-02', in_force: true, status: 'applies', reason: '2 system(s) in scope', needs_review: false, undetermined_count: 0,
            evidence: [{ key: 'notice-shown', statement: 'AI-interaction notice shown in n of m conversations', basis: 'recomputed', resolver: 'notice.shown_ratio', limit: 'Counts notices the gateway rendered.' }],
            systems: [{ id: 'sys-1', name: 'Copilot', eu_ai_act_risk_tier: 'high', eu_ai_act_role: 'provider_and_deployer', status: 'applies', reason: 'risk tier is high' }],
        },
        {
            key: 'annex3-provider-duties', title: 'High-risk provider duties', reference: 'Art 9–17', summary: 'Risk management…',
            applies_from: '2027-12-02', in_force: false, status: 'undetermined', reason: '1 system(s) in scope', needs_review: true, review_note: 'Omnibus 2026/1744 moved this date; re-check.', undetermined_count: 1,
            evidence: [], systems: [{ id: 'sys-2', name: 'Undeclared', eu_ai_act_risk_tier: 'high', eu_ai_act_role: null, status: 'undetermined', reason: 'regulated role not declared — declare it' }],
        },
    ],
    matrix: {
        window: { from: '2026-08-23T00:00:00Z', to: '2026-09-22T00:00:00Z' },
        catalog_versions: ['2026-09'],
        systems: [
            {
                ai_system_id: 'sys-1', name: 'Copilot', profile_declared: true, risk_tier: 'high', role: 'deployer',
                cells: [
                    { obligation_key: 'art50-transparency', applicability: 'applies', in_force: true, worst_status: 'not_met', status_counts: { met: 1, not_met: 1 }, by_basis: { recomputed: { met: 1, measured: 2 } }, summary: '1 of 2 recomputed met' },
                    { obligation_key: 'annex3-provider-duties', applicability: 'applies', in_force: false, worst_status: 'declared', status_counts: {}, by_basis: {}, summary: '' },
                ],
            },
            {
                ai_system_id: 'sys-2', name: 'Undeclared', profile_declared: false,
                cells: [
                    { obligation_key: 'art50-transparency', applicability: 'not_applicable', in_force: true, worst_status: null, summary: '' },
                    { obligation_key: 'annex3-provider-duties', applicability: 'undetermined', in_force: false, worst_status: null, reason: 'regulated role not declared', summary: '' },
                ],
            },
        ],
    },
} as unknown as ComplianceObligationsResponse;

const DETAIL = {
    framework: { id: 'eu-ai-act', title: 'EU AI Act', catalog_version: '2026-09' },
    key: 'art50-transparency', resolved_keys: ['art50-transparency'], title: 'Transparency obligations', reference: 'Art 50',
    summary: 'Users must be informed…', limit: 'Obligation-level limit.', not_legal_advice: 'n',
    window: { from: '2026-08-23T00:00:00Z', to: '2026-09-22T00:00:00Z' },
    systems: [
        {
            ai_system_id: 'sys-1', name: 'Copilot',
            obligations: [
                {
                    key: 'art50-transparency', title: 'Transparency obligations', reference: 'Art 50', applies_from: '2026-08-02', in_force: true, applicability: 'applies', reason: 'risk tier is high',
                    worst_status: 'not_met', by_basis: { recomputed: { met: 1, measured: 2 }, judged: { met: 1, measured: 1 } },
                    statements: [
                        { key: 'notice-shown', statement: 'Notice shown in n of m conversations', basis: 'recomputed', resolver: 'notice.shown_ratio', status: 'not_met', n: 40, m: 60, detail: '', limit: 'Counts notices the gateway rendered.', citations: ['rec-a', 'rec-b'], catalog_version: '2026-09', evaluated_at: '2026-09-22T00:00:00Z', facts: {}, refs: [], other_frameworks: [{ framework_id: 'iso-42001', framework_title: 'ISO/IEC 42001', obligation_key: 'a-8-2', title: 'Transparency to users', reference: 'A.8.2' }] },
                        { key: 'notice-clear', statement: 'Notice text is clear', basis: 'judged', resolver: 'judge.notice_clear', status: 'met', n: 57, m: 60, detail: '', limit: null, citations: [], catalog_version: '2026-09', evaluated_at: '2026-09-22T00:00:00Z', facts: { agreement: { 'chk-1': { n: 34, observed_agreement: 0.92, ci95: [0.83, 0.97], blind: true } } }, refs: [], other_frameworks: [] },
                    ],
                },
            ],
        },
    ],
} as unknown as ObligationDetailResponse;

/** Render and switch to the all-systems matrix (the board is the default). */
async function renderMatrix(p: React.ComponentProps<typeof ObligationsPage>) {
    render(<ObligationsPage {...p} />);
    fireEvent.click(await screen.findByTestId('obligations-view-matrix'));
    await screen.findByTestId('obligation-matrix');
}

const props = (framework = 'eu-ai-act', extra: Partial<React.ComponentProps<typeof ObligationsPage>> = {}) => ({
    framework,
    frameworks: FRAMEWORKS,
    registry: FRAMEWORKS,
    refreshFrameworks: vi.fn(),
    onTabChange: vi.fn(),
    setActiveTab: vi.fn(),
    openObligation: vi.fn(),
    ...extra,
});

beforeEach(() => {
    vi.clearAllMocks();
    getComplianceObligations.mockResolvedValue(BOARD);
    getComplianceObligationDetail.mockResolvedValue(DETAIL);
});

describe('ObligationsPage', () => {
    it('loads the board with the matrix for the window and renders system × obligation cells', async () => {
        await renderMatrix(props());
        expect(getComplianceObligations).toHaveBeenCalledWith(expect.objectContaining({ framework: 'eu-ai-act', include_matrix: true }));
        const [call] = getComplianceObligations.mock.calls[0] as [{ from: string; to: string }];
        expect(typeof call.from).toBe('string');
        expect(typeof call.to).toBe('string');
        // In force first, then upcoming.
        const headers = screen.getAllByRole('columnheader').slice(1).map((h) => h.textContent);
        expect(headers[0]).toContain('Transparency obligations');
        expect(headers[1]).toContain('High-risk provider duties');
        const cell = screen.getByTestId('matrix-cell-sys-1-art50-transparency');
        expect(cell).toHaveTextContent('not met');
        expect(screen.getByTestId('matrix-cell-summary-sys-1-art50-transparency')).toHaveTextContent('1 of 2 recomputed met');
        expect(screen.getByTestId('matrix-cell-summary-sys-2-art50-transparency')).toHaveTextContent('not applicable');
        expect(screen.getByTestId('matrix-cell-summary-sys-2-annex3-provider-duties')).toHaveTextContent('undetermined — regulated role not declared');
        expect(screen.getByTestId('matrix-cell-sys-2-annex3-provider-duties').className).not.toContain('green');
        expect(screen.getByTestId('matrix-row-sys-2')).toHaveTextContent('profile not declared');
        expect(screen.getByTestId('obligation-matrix').textContent).not.toMatch(/%/);
        expect(screen.getByTestId('basis-legend')).toBeInTheDocument();
    });

    it('a matrix cell opens the drill-in for that system: basis chips, n of m, limit, catalog, citations, judged sentence', async () => {
        await renderMatrix(props());
        fireEvent.click(screen.getByTestId('matrix-cell-sys-1-art50-transparency'));
        await screen.findByTestId('drill-in-system-sys-1');
        expect(getComplianceObligationDetail).toHaveBeenCalledWith('art50-transparency', expect.objectContaining({ framework: 'eu-ai-act', ai_system_id: 'sys-1' }));
        expect(screen.getByTestId('drill-in-limit')).toHaveTextContent('Obligation-level limit.');
        const row = screen.getByTestId('statements-sys-1-notice-shown');
        expect(within(row).getByTestId('statements-sys-1-notice-shown-basis')).toHaveTextContent('RECOMPUTED');
        expect(within(row).getByTestId('statements-sys-1-notice-shown-status')).toHaveTextContent('not met');
        expect(within(row).getByTestId('statements-sys-1-notice-shown-nofm')).toHaveTextContent('40 of 60');
        expect(within(row).getByTestId('statements-sys-1-notice-shown-limit')).toHaveTextContent('Limit: Counts notices the gateway rendered.');
        expect(row).toHaveTextContent('catalog 2026-09');
        expect(screen.getByTestId('statements-sys-1-notice-clear-judged')).toHaveTextContent(
            'JUDGED · met 57 of 60 · human agreement 0.92 [0.83–0.97], n = 34, blind',
        );
        // A citation opens the shared record dialog.
        fireEvent.click(within(row).getByTestId('record-link-rec-a'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-a');
    });

    it('a column header opens the drill-in for every system, and Close removes it', async () => {
        await renderMatrix(props());
        fireEvent.click(screen.getByTestId('matrix-col-art50-transparency'));
        await screen.findByTestId('obligation-drill-in');
        expect(getComplianceObligationDetail).toHaveBeenCalledWith('art50-transparency', expect.objectContaining({ ai_system_id: undefined }));
        fireEvent.click(screen.getByTestId('drill-in-close'));
        expect(screen.queryByTestId('obligation-drill-in')).toBeNull();
    });

    it('"show this evidence under other frameworks" switches framework and opens that obligation', async () => {
        const p = props();
        await renderMatrix(p);
        fireEvent.click(screen.getByTestId('matrix-cell-sys-1-art50-transparency'));
        await screen.findByTestId('statements-sys-1-notice-shown-other-frameworks');
        fireEvent.click(screen.getByTestId('other-framework-iso-42001-a-8-2'));
        expect(p.openObligation).toHaveBeenCalledWith('iso-42001', 'a-8-2');
    });

    it('arrives with a focused obligation open (the cross-framework landing)', async () => {
        render(<ObligationsPage {...props('eu-ai-act', { focusObligation: 'art50-transparency' })} />);
        await screen.findByTestId('obligation-drill-in');
        expect(getComplianceObligationDetail).toHaveBeenCalledWith('art50-transparency', expect.objectContaining({ framework: 'eu-ai-act' }));
    });

    it('changing the window recomputes the board', async () => {
        await renderMatrix(props());
        fireEvent.change(screen.getByTestId('obligations-window'), { target: { value: '7d' } });
        await waitFor(() => expect(getComplianceObligations).toHaveBeenCalledTimes(2));
        const [first, second] = getComplianceObligations.mock.calls.map((c) => (c as [{ from: string }])[0].from);
        expect(new Date(second).getTime()).toBeGreaterThan(new Date(first).getTime());
    });

    it('opens on the all-systems board: dated rows, undetermined to-dos and the evidence opening under its row', async () => {
        const p = props();
        render(<ObligationsPage {...p} />);
        await screen.findByTestId('obligation-board');
        expect(within(screen.getByTestId('obligations-in-force')).getByTestId('obligation-art50-transparency')).toBeInTheDocument();
        expect(within(screen.getByTestId('obligations-upcoming')).getByTestId('obligation-annex3-provider-duties')).toBeInTheDocument();
        expect(screen.getByTestId('obligation-scope-annex3-provider-duties')).toHaveTextContent('1 system · 1 undetermined');
        fireEvent.click(screen.getByText('High-risk provider duties'));
        expect(screen.getByText('role not declared', { exact: false })).toBeInTheDocument();
        expect(screen.getByText('Omnibus 2026/1744 moved this date; re-check.')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Transparency obligations'));
        expect(screen.getByTestId('obligation-evidence-art50-transparency-notice-shown-limit')).toHaveTextContent('Limit:');
        fireEvent.click(screen.getByRole('button', { name: 'Copilot' }));
        expect(p.setActiveTab).toHaveBeenCalledWith('ai-systems:sys-1:compliance');
        fireEvent.click(screen.getByTestId('obligation-open-evidence-art50-transparency'));
        await screen.findByTestId('obligation-drill-in');
        expect(within(screen.getByTestId('obligation-detail-art50-transparency')).getByTestId('obligation-drill-in')).toBeInTheDocument();
        expect(getComplianceObligationDetail).toHaveBeenCalledWith('art50-transparency', expect.objectContaining({ ai_system_id: undefined }));
    });

    it('renders an honest empty state for a framework without a catalog', () => {
        const p = props('soc2');
        render(<ObligationsPage {...p} />);
        expect(getComplianceObligations).not.toHaveBeenCalled();
        const empty = screen.getByTestId('obligations-no-catalog');
        expect(empty).toHaveTextContent('SOC 2 has no obligation catalog yet');
        fireEvent.click(screen.getByText('Open the registry →'));
        expect(p.onTabChange).toHaveBeenCalledWith('compliance-frameworks');
    });

    it('shows the error when the board cannot be computed, and the drill-in error on its own', async () => {
        getComplianceObligations.mockRejectedValueOnce(new Error('board failed'));
        const { unmount } = render(<ObligationsPage {...props()} />);
        expect(await screen.findByTestId('obligations-error')).toHaveTextContent('board failed');
        unmount();

        getComplianceObligationDetail.mockRejectedValueOnce(new Error('detail failed'));
        await renderMatrix(props());
        fireEvent.click(screen.getByTestId('matrix-cell-sys-1-art50-transparency'));
        expect(await screen.findByTestId('drill-in-error')).toHaveTextContent('detail failed');
    });
});

// The matrix and the board show the tier each system was
// EVALUATED at, next to the declared one, whenever composition lifted it.
describe('ObligationsPage — evaluated vs declared tier', () => {
    const lifted = {
        ...BOARD,
        obligations: BOARD.obligations.map((o, i) =>
            i === 0
                ? {
                      ...o,
                      systems: [
                          ...(o.systems ?? []),
                          { id: 'sys-3', name: 'Address Validator', eu_ai_act_risk_tier: 'minimal', risk_tier_evaluated: 'high', eu_ai_act_role: 'deployer', status: 'applies', reason: 'risk tier is high' },
                      ],
                  }
                : o,
        ),
        matrix: {
            ...BOARD.matrix,
            systems: [
                ...(BOARD.matrix?.systems ?? []),
                {
                    ai_system_id: 'sys-3', name: 'Address Validator', profile_declared: true, risk_tier: 'high', risk_tier_declared: 'minimal', role: 'deployer',
                    effective_tier_sources: [{ system_id: 'sys-1', system_name: 'Copilot', tier: 'high' }],
                    cells: [],
                },
            ],
        },
    } as unknown as ComplianceObligationsResponse;

    it('the matrix row says evaluated high (declared minimal, via Copilot)', async () => {
        getComplianceObligations.mockResolvedValue(lifted);
        await renderMatrix(props());
        const tier = await screen.findByTestId('matrix-tier-sys-3');
        expect(tier).toHaveTextContent('evaluated high (declared minimal, via Copilot)');
        expect(screen.getByTestId('matrix-tier-sys-1')).toHaveTextContent('high');
        expect(screen.getByTestId('matrix-tier-sys-1')).toHaveAttribute('data-lifted', 'false');
    });

    it('the board lists the system at its evaluated tier', async () => {
        getComplianceObligations.mockResolvedValue(lifted);
        await renderMatrix(props());
        fireEvent.click(screen.getByTestId('obligations-view-board'));
        fireEvent.click(screen.getByText('Transparency obligations'));
        expect(screen.getByTestId('board-tier-art50-transparency-sys-3')).toHaveTextContent('evaluated high (declared minimal)');
        expect(screen.getByTestId('board-tier-art50-transparency-sys-1')).toHaveAttribute('data-lifted', 'false');
    });
});

// The matrix answers "how do my systems compare"; the question an operator
// actually arrives with is "what does THIS system owe". The selector answers
// it from the same load.
describe('ObligationsPage — one AI System', () => {
    const pick = async (id: string, p = props()) => {
        render(<ObligationsPage {...p} />);
        fireEvent.change(await screen.findByTestId('obligations-system'), { target: { value: id } });
        await screen.findByTestId('system-obligations');
        return p;
    };

    it('offers every system plus "All systems", and defaults to all', async () => {
        render(<ObligationsPage {...props()} />);
        const select = await screen.findByTestId('obligations-system');
        expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(['All systems (2)', 'Copilot', 'Undeclared']);
        expect((select as HTMLSelectElement).value).toBe('');
        expect(screen.getByTestId('obligation-board')).toBeInTheDocument();
    });

    it('groups the system\'s obligations in force / upcoming with status and n of m, from the load already made', async () => {
        await pick('sys-1');
        expect(getComplianceObligations).toHaveBeenCalledTimes(1);
        const inForce = within(screen.getByTestId('system-obligations-in-force'));
        expect(inForce.getByTestId('system-obligation-art50-transparency')).toHaveTextContent('Transparency obligations');
        expect(inForce.getByTestId('system-obligation-art50-transparency')).toHaveTextContent('since 2026-08-02');
        expect(inForce.getByTestId('system-obligation-status-art50-transparency')).toHaveTextContent('not met');
        expect(inForce.getByTestId('system-obligation-summary-art50-transparency')).toHaveTextContent('1 of 2 recomputed met');
        const upcoming = within(screen.getByTestId('system-obligations-upcoming'));
        expect(upcoming.getByTestId('system-obligation-annex3-provider-duties')).toHaveTextContent('from 2027-12-02');
        expect(screen.queryByTestId('system-obligations-not-applicable')).toBeNull();
        expect(screen.getByTestId('system-obligations-counts')).toHaveTextContent('In force1Upcoming1Undetermined0Not applicable0');
        expect(screen.getByTestId('system-obligations-tier')).toHaveTextContent('high');
        // The view toggle belongs to the all-systems comparison only.
        expect(screen.queryByTestId('obligations-view-matrix')).toBeNull();
        expect(screen.getByTestId('system-obligations').textContent).not.toMatch(/%/);
    });

    it('keeps undetermined on the dated side as a to-do, and not-applicable apart', async () => {
        const p = await pick('sys-2');
        expect(screen.getByTestId('system-obligations-profile')).toHaveTextContent('Profile not declared');
        const upcoming = within(screen.getByTestId('system-obligations-upcoming'));
        expect(upcoming.getByTestId('system-obligation-status-annex3-provider-duties')).toHaveTextContent('undetermined');
        expect(upcoming.getByTestId('system-obligation-summary-annex3-provider-duties')).toHaveTextContent('undetermined — regulated role not declared');
        expect(within(screen.getByTestId('system-obligations-not-applicable')).getByTestId('system-obligation-art50-transparency')).toBeInTheDocument();
        expect(screen.getByTestId('system-obligations-in-force')).toHaveTextContent('No obligation in force applies to this system.');
        expect(screen.getByTestId('system-obligations-counts')).toHaveTextContent('In force0Upcoming1Undetermined1Not applicable1');
        fireEvent.click(screen.getByTestId('system-obligations-edit-profile'));
        expect(p.onTabChange).toHaveBeenCalledWith('compliance-profiles');
    });

    it('a row opens its evaluated evidence under the row, for that system; clicking again closes it', async () => {
        await pick('sys-1');
        fireEvent.click(screen.getByTestId('system-obligation-art50-transparency'));
        const detail = await screen.findByTestId('system-obligation-detail-art50-transparency');
        await within(detail).findByTestId('drill-in-system-sys-1');
        expect(getComplianceObligationDetail).toHaveBeenCalledWith('art50-transparency', expect.objectContaining({ framework: 'eu-ai-act', ai_system_id: 'sys-1' }));
        expect(screen.getAllByTestId('obligation-drill-in')).toHaveLength(1);
        fireEvent.click(screen.getByTestId('system-obligation-art50-transparency'));
        expect(screen.queryByTestId('obligation-drill-in')).toBeNull();
    });

    it('switching system closes the open evidence; back to "All systems" restores the board', async () => {
        await pick('sys-1');
        fireEvent.click(screen.getByTestId('system-obligation-art50-transparency'));
        await screen.findByTestId('obligation-drill-in');
        fireEvent.change(screen.getByTestId('obligations-system'), { target: { value: 'sys-2' } });
        expect(screen.queryByTestId('obligation-drill-in')).toBeNull();
        fireEvent.change(screen.getByTestId('obligations-system'), { target: { value: '' } });
        expect(screen.getByTestId('obligation-board')).toBeInTheDocument();
        expect(screen.getByTestId('obligations-view-matrix')).toBeInTheDocument();
    });

    it('a system with no cell in the window says "not evaluated", never a status', async () => {
        getComplianceObligations.mockResolvedValue({
            ...BOARD,
            matrix: { ...BOARD.matrix, systems: [{ ai_system_id: 'sys-9', name: 'New', profile_declared: true, risk_tier: 'minimal', role: 'deployer', cells: [] }] },
        });
        await pick('sys-9');
        expect(screen.getByTestId('system-obligations-not-applicable')).toHaveTextContent('not evaluated in the window');
        expect(screen.getByTestId('system-obligations-counts')).toHaveTextContent('In force0Upcoming0Undetermined0Not applicable2');
    });
});

// The window has minute precision, so recomputing it on every render would
// change the load key on the first re-render after the clock crosses a minute:
// the page would go back to "Computing…" and lose every open row and drill-in.
// The window is fixed until the operator changes the preset.
describe('ObligationsPage — the window does not move under the operator', () => {
    it('a re-render minutes later neither reloads nor closes what is open', async () => {
        const realNow = Date.now;
        let now = new Date('2026-10-01T10:00:20Z').getTime();
        vi.spyOn(Date, 'now').mockImplementation(() => now);
        const RealDate = Date;
        vi.stubGlobal('Date', class extends RealDate {
            constructor(...args: unknown[]) {
                if (args.length === 0) super(now);
                else super(...(args as [number]));
            }
            static now() { return now; }
        });
        try {
            render(<ObligationsPage {...props()} />);
            await screen.findByTestId('obligation-board');
            fireEvent.click(screen.getByText('Transparency obligations'));
            expect(screen.getByTestId('obligation-evidence-art50-transparency-notice-shown-limit')).toBeInTheDocument();
            now += 3 * 60_000;
            // Picking a system re-renders the page; it must use what is loaded.
            fireEvent.change(screen.getByTestId('obligations-system'), { target: { value: 'sys-1' } });
            await screen.findByTestId('system-obligations');
            fireEvent.change(screen.getByTestId('obligations-system'), { target: { value: '' } });
            expect(screen.queryByTestId('obligations-loading')).toBeNull();
            expect(getComplianceObligations).toHaveBeenCalledTimes(1);
        } finally {
            vi.unstubAllGlobals();
            Date.now = realNow;
            vi.restoreAllMocks();
        }
    });
});

describe('ObligationBoard — not applicable', () => {
    it('sets an obligation with no system in scope apart from the dated groups', async () => {
        getComplianceObligations.mockResolvedValue({
            ...BOARD,
            obligations: [...BOARD.obligations, { ...BOARD.obligations[0], key: 'gpai-duties', title: 'GPAI model duties', systems: [], undetermined_count: 0 }],
        });
        render(<ObligationsPage {...props()} />);
        await screen.findByTestId('obligation-board');
        expect(within(screen.getByTestId('obligations-not-applicable')).getByTestId('obligation-gpai-duties')).toBeInTheDocument();
        expect(within(screen.getByTestId('obligations-in-force')).queryByTestId('obligation-gpai-duties')).toBeNull();
        expect(screen.getByTestId('obligation-scope-gpai-duties')).toHaveTextContent('no systems in scope');
    });

    it('shows no such group when every obligation has a system in scope', async () => {
        render(<ObligationsPage {...props()} />);
        await screen.findByTestId('obligation-board');
        expect(screen.queryByTestId('obligations-not-applicable')).toBeNull();
    });
});
