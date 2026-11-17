/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The declared outcomes editor. What is worth defending:
 *
 *   - it loads the LIVE catalogue into the form and shows the ACTIVE one as
 *     what the gateway judges against — saving is not signing;
 *   - when the two differ it says so, naming what would change;
 *   - it saves the full document (`{outcomes: [...]}`), clearing to null;
 *   - it refuses what the server refuses before a round trip, and puts the
 *     server's 422 field errors beside the fields they name.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetch = vi.fn();
const mockPut = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemOutcomes: mockFetch,
        putAISystemOutcomes: mockPut,
    }),
}));

import OutcomesPanel from '../OutcomesPanel';
import type { OutcomeDecl, OutcomesResponse } from '../../../lib/runOutcomes';

const CATALOGUE: OutcomeDecl[] = [
    {
        id: 'application_processed',
        label: 'Application processed',
        counts_toward_baseline: true,
        variants: [
            { id: 'loan_approved', label: 'Recommended: approve', expect: { requires_capabilities: ['applications_set_recommendation'] } },
            { id: 'loan_rejected', label: 'Recommended: decline' },
        ],
    },
    {
        id: 'no_applications_available',
        label: 'Nothing to screen',
        counts_toward_baseline: false,
        expect: { max_model_calls: 0, max_cost_usd: 0 },
    },
];

function resp(overrides: Partial<OutcomesResponse> = {}): OutcomesResponse {
    return {
        ai_system_id: 'sys-1',
        outcomes: CATALOGUE,
        active_outcomes: CATALOGUE,
        active_contract_version: 4,
        reportable_ids: [],
        config_drifted: false,
        ...overrides,
    };
}

const onChanged = vi.fn();

async function openPanel() {
    render(<OutcomesPanel aiSystemId="sys-1" onChanged={onChanged} />);
    return screen.findByTestId('outcomes-panel');
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(resp());
    mockPut.mockImplementation((_id: string, outcomes: OutcomeDecl[] | null) =>
        Promise.resolve(resp({ outcomes, config_drifted: true })),
    );
});

describe('OutcomesPanel — loading', () => {
    it('loads the live catalogue into the form', async () => {
        await openPanel();
        expect(mockFetch).toHaveBeenCalledWith('sys-1');
        expect(screen.getByTestId('outcome-0-id')).toHaveValue('application_processed');
        expect(screen.getByTestId('outcome-0-label')).toHaveValue('Application processed');
        expect(screen.getByTestId('outcome-0-counts')).toBeChecked();
        expect(screen.getByTestId('outcome-0-variant-0-id')).toHaveValue('loan_approved');
        expect(screen.getByTestId('outcome-0-variant-0-expect-requires_capabilities')).toHaveValue('applications_set_recommendation');
        expect(screen.getByTestId('outcome-1-counts')).not.toBeChecked();
        expect(screen.getByTestId('outcome-1-no-work-note')).toHaveTextContent('never a success');
        expect(screen.getByTestId('outcome-1-expect-max_model_calls')).toHaveValue('0');
        expect(screen.getByTestId('outcomes-reportable')).toHaveTextContent(
            'application_processed.loan_approved · application_processed.loan_rejected · no_applications_available',
        );
        // Nothing to save yet.
        expect(screen.getByTestId('outcomes-save')).toBeDisabled();
    });

    it('shows the active contract’s catalogue as what the gateway judges against', async () => {
        await openPanel();
        const judged = screen.getByTestId('outcomes-judged-against');
        expect(judged).toHaveTextContent('Judged against contract v4.');
        expect(judged).toHaveTextContent('application_processed.loan_approved');
        expect(judged).toHaveTextContent('no_applications_available');
        expect(screen.queryByTestId('outcomes-config-drift')).toBeNull();
    });

    it('says plainly when no active contract carries a catalogue', async () => {
        mockFetch.mockResolvedValue(resp({ active_outcomes: null, active_contract_version: null }));
        await openPanel();
        expect(screen.getByTestId('outcomes-none-active')).toHaveTextContent('No contract is active');
        expect(screen.getByTestId('outcomes-none-active')).toHaveTextContent('judged undeclared');
    });

    it('names what would change when the live catalogue differs from the active one', async () => {
        mockFetch.mockResolvedValue(
            resp({
                active_outcomes: [CATALOGUE[0]],
                outcomes: [
                    { ...CATALOGUE[0], variants: [CATALOGUE[0].variants![0]] },
                    CATALOGUE[1],
                ],
                config_drifted: true,
            }),
        );
        await openPanel();
        const drift = screen.getByTestId('outcomes-config-drift');
        expect(drift).toHaveTextContent('not signed yet');
        expect(drift).toHaveTextContent('contract v4');
        expect(screen.getByTestId('outcomes-drift-added')).toHaveTextContent('no_applications_available');
        expect(screen.getByTestId('outcomes-drift-removed')).toHaveTextContent('application_processed.loan_rejected');
    });

    it('flags a change that keeps the ids but moves a baseline flag', async () => {
        mockFetch.mockResolvedValue(resp({ outcomes: [CATALOGUE[0], { ...CATALOGUE[1], counts_toward_baseline: true }] }));
        await openPanel();
        expect(screen.getByTestId('outcomes-config-drift')).toBeInTheDocument();
        expect(screen.queryByTestId('outcomes-drift-added')).toBeNull();
    });

    it('explains an empty catalogue', async () => {
        mockFetch.mockResolvedValue(resp({ outcomes: null, active_outcomes: null }));
        await openPanel();
        expect(screen.getByTestId('outcomes-empty')).toHaveTextContent('no completed run can count as a success');
    });

    it('surfaces a load failure', async () => {
        mockFetch.mockRejectedValue(new Error('control plane unreachable'));
        render(<OutcomesPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('outcomes-error')).toHaveTextContent('control plane unreachable');
    });
});

describe('OutcomesPanel — editing and saving', () => {
    it('adds an outcome with a variant and expectations, and saves the full document', async () => {
        await openPanel();
        fireEvent.click(screen.getByTestId('outcomes-add'));
        fireEvent.change(screen.getByTestId('outcome-2-id'), { target: { value: 'applications_dispatched' } });
        fireEvent.change(screen.getByTestId('outcome-2-label'), { target: { value: 'Dispatched to application runs' } });
        fireEvent.click(screen.getByTestId('outcome-2-counts'));
        fireEvent.change(screen.getByTestId('outcome-2-expect-max_cost_usd'), { target: { value: '0' } });
        fireEvent.change(screen.getByTestId('outcome-2-expect-forbids_capabilities'), {
            target: { value: 'applications_set_recommendation, applications_add_note' },
        });
        fireEvent.click(screen.getByTestId('outcome-0-add-variant'));
        fireEvent.change(screen.getByTestId('outcome-0-variant-2-id'), { target: { value: 'loan_referred' } });
        fireEvent.change(screen.getByTestId('outcome-0-variant-2-label'), { target: { value: 'Recommended: refer' } });
        fireEvent.change(screen.getByTestId('outcome-0-variant-2-expect-max_actions'), { target: { value: '20' } });

        fireEvent.click(screen.getByTestId('outcomes-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
        const [id, outcomes] = mockPut.mock.calls[0] as [string, Record<string, unknown>[]];
        expect(id).toBe('sys-1');
        expect(outcomes).toHaveLength(3);
        expect(outcomes[0]).toEqual({
            id: 'application_processed',
            label: 'Application processed',
            counts_toward_baseline: true,
            variants: [
                { id: 'loan_approved', label: 'Recommended: approve', expect: { requires_capabilities: ['applications_set_recommendation'] } },
                { id: 'loan_rejected', label: 'Recommended: decline' },
                { id: 'loan_referred', label: 'Recommended: refer', expect: { max_actions: 20 } },
            ],
        });
        expect(outcomes[2]).toEqual({
            id: 'applications_dispatched',
            label: 'Dispatched to application runs',
            counts_toward_baseline: false,
            expect: { max_cost_usd: 0, forbids_capabilities: ['applications_set_recommendation', 'applications_add_note'] },
        });
        // Saving is not signing.
        expect(await screen.findByTestId('outcomes-notice')).toHaveTextContent('mint, approve and promote a new version');
        expect(onChanged).toHaveBeenCalled();
        expect(screen.getByTestId('outcomes-save')).toBeDisabled();
    });

    it('removing every outcome clears the catalogue (null, not [])', async () => {
        await openPanel();
        fireEvent.click(screen.getByTestId('outcome-1-remove'));
        fireEvent.click(screen.getByTestId('outcome-0-remove'));
        expect(screen.getByTestId('outcomes-empty')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('outcomes-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalledWith('sys-1', null));
    });

    it('removes one variant without touching the others', async () => {
        await openPanel();
        fireEvent.click(screen.getByTestId('outcome-0-variant-0-remove'));
        expect(screen.getByTestId('outcome-0-variant-0-id')).toHaveValue('loan_rejected');
        fireEvent.click(screen.getByTestId('outcomes-save'));
        await waitFor(() => expect(mockPut).toHaveBeenCalled());
        const outcomes = mockPut.mock.calls[0][1] as Record<string, unknown>[];
        expect(outcomes[0].variants).toEqual([{ id: 'loan_rejected', label: 'Recommended: decline' }]);
    });

    it('discards unsaved changes', async () => {
        await openPanel();
        fireEvent.change(screen.getByTestId('outcome-0-label'), { target: { value: 'Renamed' } });
        expect(screen.getByTestId('outcomes-save')).toBeEnabled();
        fireEvent.click(screen.getByTestId('outcomes-discard'));
        expect(screen.getByTestId('outcome-0-label')).toHaveValue('Application processed');
        expect(screen.getByTestId('outcomes-save')).toBeDisabled();
    });

    it('refuses what the server would refuse, before a round trip', async () => {
        await openPanel();
        fireEvent.click(screen.getByTestId('outcomes-add'));
        fireEvent.change(screen.getByTestId('outcome-2-id'), { target: { value: 'application_processed' } });
        fireEvent.change(screen.getByTestId('outcome-0-variant-1-id'), { target: { value: 'Loan Rejected' } });
        fireEvent.change(screen.getByTestId('outcome-1-expect-max_model_calls'), { target: { value: '1.5' } });
        fireEvent.change(screen.getByTestId('outcome-1-expect-requires_capabilities'), { target: { value: 'x' } });
        fireEvent.change(screen.getByTestId('outcome-1-expect-forbids_capabilities'), { target: { value: 'x' } });
        fireEvent.click(screen.getByTestId('outcomes-save'));

        expect(await screen.findByTestId('outcomes-error')).toHaveTextContent('Fix the highlighted fields');
        expect(screen.getByTestId('outcome-2-error-id')).toHaveTextContent("duplicate outcome id 'application_processed'");
        expect(screen.getByTestId('outcome-2-error-label')).toHaveTextContent('label is required');
        expect(screen.getByTestId('outcome-0-variant-1-error-id')).toHaveTextContent('id must match');
        expect(screen.getByTestId('outcome-1-expect-error-max_model_calls')).toHaveTextContent('whole number');
        expect(screen.getByTestId('outcome-1-expect-error')).toHaveTextContent('both required and forbidden: x');
        expect(mockPut).not.toHaveBeenCalled();
    });

    it('puts the server’s 422 field errors beside the fields they name', async () => {
        mockPut.mockRejectedValue(
            Object.assign(new Error('outcomes.0.variants.1.id: duplicate'), {
                status: 422,
                details: {
                    detail: [
                        { loc: ['outcomes', 0, 'variants', 1, 'id'], msg: "duplicate variant id 'loan_rejected'" },
                        { loc: ['outcomes', 1, 'expect', 'max_cost_usd'], msg: 'max_cost_usd must be a number ≥ 0' },
                        { loc: ['outcomes'], msg: 'at most 32 outcomes' },
                    ],
                },
            }),
        );
        await openPanel();
        fireEvent.change(screen.getByTestId('outcome-0-label'), { target: { value: 'Application processed (v2)' } });
        fireEvent.click(screen.getByTestId('outcomes-save'));
        expect(await screen.findByTestId('outcomes-error')).toHaveTextContent('The catalogue was refused');
        expect(screen.getByTestId('outcome-0-variant-1-error-id')).toHaveTextContent("duplicate variant id 'loan_rejected'");
        expect(screen.getByTestId('outcome-1-expect-error-max_cost_usd')).toHaveTextContent('max_cost_usd must be a number');
        expect(screen.getByTestId('outcomes-error-outcomes')).toHaveTextContent('at most 32 outcomes');
        // The form keeps the operator's edit.
        expect(screen.getByTestId('outcome-0-label')).toHaveValue('Application processed (v2)');

        // Editing again clears the server's positional errors.
        fireEvent.change(screen.getByTestId('outcome-0-label'), { target: { value: 'Application processed' } });
        expect(screen.queryByTestId('outcome-0-variant-1-error-id')).toBeNull();
    });

    it('shows a refusal without field errors as a message', async () => {
        mockPut.mockRejectedValue(new Error('Forbidden'));
        await openPanel();
        fireEvent.change(screen.getByTestId('outcome-0-label'), { target: { value: 'X' } });
        fireEvent.click(screen.getByTestId('outcomes-save'));
        expect(await screen.findByTestId('outcomes-error')).toHaveTextContent('Forbidden');
    });

    it('says when a save left nothing to sign', async () => {
        mockPut.mockImplementation((_id: string, outcomes: OutcomeDecl[] | null) =>
            Promise.resolve(resp({ outcomes, config_drifted: false })),
        );
        await openPanel();
        fireEvent.change(screen.getByTestId('outcome-0-description'), { target: { value: 'One screened application' } });
        fireEvent.click(screen.getByTestId('outcomes-save'));
        expect(await screen.findByTestId('outcomes-notice')).toHaveTextContent(/^Saved\.$/);
        expect(within(screen.getByTestId('outcomes-panel')).queryByTestId('outcomes-error')).toBeNull();
    });
});
