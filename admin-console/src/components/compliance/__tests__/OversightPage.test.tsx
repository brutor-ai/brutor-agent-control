/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Human Oversight: assignment declared, override and approvals recomputed
 * as n of m, movement as an indicator with no verdict colour, the blind
 * review queue and agreement per judged check with a link to its queue.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const getComplianceOversight = vi.fn();
const fetchAISystems = vi.fn();

const gateway = { gatewayClient: { getComplianceOversight }, fetchAISystems };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));

import OversightPage from '../pages/OversightPage';

const SUMMARY = {
    tenant_id: 'default', ai_system_id: null, window_start: '2026-08-23T00:00:00Z', window_end: '2026-09-22T00:00:00Z',
    approvals: { requested: 40, decided: 35, decided_by_human: 33, lapsed: 2, in_flight: 5 },
    systems: [
        {
            ai_system_id: 'sys-1', name: 'Copilot', oversight_assignment: 'Ops on-call (ops@example.com)', assignment_basis: 'declared',
            override_available: { basis: 'recomputed', status: 'met', n: 1, m: 1, detail: 'run:abort held by the oversight group', limit: null, citations: ['rec-abort'], facts: {} },
            approvals: { basis: 'recomputed', status: 'met', n: 33, m: 40, detail: '33 of 40 gates decided by a person', limit: 'Counts gates the gateway opened.', citations: [], facts: {} },
            movement: { basis: 'indicator', status: 'indicator', n: null, m: null, detail: 'Approvals are faster than the baseline.', limit: 'A change in pace is a question, not a result.', citations: [], facts: { latency_change: -0.42, denial_rate_change: 0.01, baseline_n: 120 } },
        },
        {
            ai_system_id: 'sys-2', name: 'Triage', oversight_assignment: null, assignment_basis: 'declared',
            override_available: { basis: 'recomputed', status: 'not_met', n: 0, m: 1, detail: 'nobody holds run:abort', limit: null, citations: [], facts: {} },
            approvals: { basis: 'recomputed', status: 'not_evaluable', n: null, m: null, detail: 'no approval gate opened', limit: null, citations: [], facts: {} },
            movement: { basis: 'indicator', status: 'indicator', detail: 'no baseline yet', facts: {} },
        },
    ],
    review_queue: { pending: 4, graded: 30 },
    judged_checks: [
        { check_id: 'chk-1', name: 'Notice is clear', template: 'notice_clear', enabled: true, review_queue: { pending: 4, graded: 30, skipped: 1 }, agreement: { n: 34, observed_agreement: 0.92, ci95: [0.83, 0.97], kappa: 0.8, blind: true, insufficient_sample: false, below_threshold: false } },
        { check_id: 'chk-2', name: 'Per instructions', template: 'per_instructions', enabled: false, review_queue: { pending: 0, graded: 0, skipped: 0 }, agreement: { n: 12, observed_agreement: 0.6, ci95: [0.4, 0.8], kappa: 0.2, blind: true, insufficient_sample: false, below_threshold: true } },
    ],
    movement_note: 'Approval latency and denial rate are indicators of movement against each system\'s own baseline, never thresholds.',
};

const props = () => ({ framework: 'eu-ai-act', frameworks: [], registry: [], refreshFrameworks: vi.fn(), onTabChange: vi.fn(), setActiveTab: vi.fn() });

beforeEach(() => {
    vi.clearAllMocks();
    getComplianceOversight.mockResolvedValue(SUMMARY);
    fetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'copilot', display_name: 'Copilot', governance_gaps: [] }], total: 1 });
});

describe('OversightPage', () => {
    it('shows approvals totals, per-system assignment / override / approvals, and movement as an indicator', async () => {
        render(<OversightPage {...props()} />);
        await screen.findByTestId('compliance-oversight');
        expect(screen.getByTestId('approvals-requested')).toHaveTextContent('40');
        expect(screen.getByTestId('approvals-decided_by_human')).toHaveTextContent('33');
        expect(screen.getByTestId('approvals-lapsed')).toHaveTextContent('2');
        expect(screen.getByTestId('approvals-in_flight')).toHaveTextContent('5');

        expect(screen.getByTestId('oversight-assignment-sys-1')).toHaveTextContent('DECLARED');
        expect(screen.getByTestId('oversight-assignment-sys-1')).toHaveTextContent('Ops on-call');
        expect(screen.getByTestId('oversight-assignment-sys-2')).toHaveTextContent('not declared');
        expect(screen.getByTestId('oversight-override-sys-1')).toHaveTextContent('RECOMPUTED');
        expect(screen.getByTestId('oversight-override-sys-1')).toHaveTextContent('1 of 1');
        expect(screen.getByTestId('oversight-approvals-sys-1')).toHaveTextContent('33 of 40');
        expect(screen.getByTestId('oversight-approvals-sys-1')).toHaveTextContent('Limit: Counts gates the gateway opened.');
        expect(screen.getByTestId('oversight-approvals-sys-2')).toHaveTextContent('not evaluable');
        expect(screen.getByTestId('oversight-approvals-sys-2').textContent).not.toMatch(/%.*of/);

        const movement = screen.getByTestId('oversight-movement-sys-1');
        expect(movement).toHaveTextContent('INDICATOR');
        expect(movement).toHaveTextContent('approval latency −42 % vs baseline · denial rate +1 % vs baseline · baseline n = 120');
        expect(movement).toHaveTextContent('Limit: A change in pace is a question, not a result.');
        expect(movement.querySelector('.text-green-800, .text-red-800, .bg-green-100, .bg-red-100')).toBeNull();
        expect(screen.getByTestId('oversight-movement-sys-2')).toHaveTextContent('no baseline yet');
        expect(screen.getByTestId('oversight-systems')).toHaveTextContent('never thresholds');
    });

    it('lists judged checks with their queue and agreement, links to the queue, and opens cited records', async () => {
        const p = props();
        render(<OversightPage {...p} />);
        await screen.findByTestId('compliance-oversight');
        expect(screen.getByTestId('oversight-checks')).toHaveTextContent('4 pending · 30 graded');
        expect(screen.getByTestId('oversight-queue-chk-1')).toHaveTextContent('4 pending · 30 graded · 1 skipped');
        expect(screen.getByTestId('oversight-agreement-chk-1')).toHaveTextContent('human agreement 0.92 [0.83–0.97], n = 34, blind');
        expect(screen.getByTestId('oversight-agreement-chk-2')).toHaveTextContent('below threshold — judge uncalibrated');
        expect(screen.getByTestId('oversight-check-chk-2')).toHaveTextContent('disabled');
        fireEvent.click(screen.getByTestId('oversight-open-check-chk-1'));
        expect(p.setActiveTab).toHaveBeenCalledWith('inbox:reviews:chk-1');
        fireEvent.click(screen.getByTestId('record-link-rec-abort'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-abort');
    });

    it('states the holds raised inside incident windows beside the complete counts', async () => {
        getComplianceOversight.mockResolvedValueOnce({
            ...SUMMARY,
            approvals: { requested: 301, decided: 51, decided_by_human: 51, lapsed: 250, in_flight: 0, excluded_in_incident_windows: 250 },
        });
        render(<OversightPage {...props()} />);
        await screen.findByTestId('compliance-oversight');
        // The tiles are the complete record — nothing hidden.
        expect(screen.getByTestId('approvals-requested')).toHaveTextContent('301');
        expect(screen.getByTestId('approvals-lapsed')).toHaveTextContent('250');
        const note = screen.getByTestId('approvals-excluded');
        expect(note).toHaveTextContent('250 inside incident windows, not judged');
        expect(note).toHaveTextContent('holds raised outside every window that lapsed still count');
    });

    it('shows no exclusion note when no hold lay inside a window', async () => {
        render(<OversightPage {...props()} />);
        await screen.findByTestId('compliance-oversight');
        expect(screen.queryByTestId('approvals-excluded')).toBeNull();
    });

    it('filters by system and window', async () => {
        render(<OversightPage {...props()} />);
        await screen.findByTestId('compliance-oversight');
        expect(getComplianceOversight).toHaveBeenCalledWith(expect.objectContaining({ ai_system_id: undefined }));
        fireEvent.change(await screen.findByTestId('oversight-system'), { target: { value: 'sys-1' } });
        await waitFor(() => expect(getComplianceOversight).toHaveBeenLastCalledWith(expect.objectContaining({ ai_system_id: 'sys-1' })));
        fireEvent.change(screen.getByTestId('oversight-window'), { target: { value: '90d' } });
        await waitFor(() => expect(getComplianceOversight).toHaveBeenCalledTimes(3));
    });

    it('renders empty and error states', async () => {
        getComplianceOversight.mockResolvedValueOnce({ ...SUMMARY, systems: [], judged_checks: [] });
        const { unmount } = render(<OversightPage {...props()} />);
        expect(await screen.findByTestId('oversight-no-systems')).toBeInTheDocument();
        expect(screen.getByTestId('oversight-no-checks')).toBeInTheDocument();
        unmount();

        getComplianceOversight.mockRejectedValueOnce(new Error('oversight down'));
        render(<OversightPage {...props()} />);
        expect(await screen.findByTestId('oversight-error')).toHaveTextContent('oversight down');
    });
});
