/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The owning tab around the A2A limits form.
 *
 * The load-bearing thing here is the shape of what goes on the wire. The
 * form emits `null` for a cleared field so the UI can tell "unset" from
 * "zero", but the control plane validates the stored blob — so nulls are
 * stripped before the PATCH, and a section left entirely empty is dropped
 * rather than persisted as `{}`. A stored `{invocations: {}}` reads to a
 * later inheritance walk as a governed-but-empty surface.
 *
 * After a successful save the tab RE-READS, because the effective values
 * are recomputed server-side by the restrictive merge: what the operator
 * typed is not necessarily what now applies.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockGetGroupA2ALimits = vi.fn();
const mockUpdateResourceGroup = vi.fn();
const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock('../../../../hooks/useAuth', () => {
    const gatewayClient = {
        getGroupA2ALimits: (...a: unknown[]): unknown => mockGetGroupA2ALimits(...a),
        updateResourceGroup: (...a: unknown[]): unknown => mockUpdateResourceGroup(...a),
    };
    return { useAuth: () => ({ gatewayClient }) };
});

vi.mock('../../../common/Toast', () => ({
    useToast: () => ({ success: mockToastSuccess, error: mockToastError }),
}));

import GroupA2ALimitsTab from '../GroupA2ALimitsTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import { STORED_WITH_REFUSED, controlPlaneRefusal } from '../../../../test/controlPlaneRefusal';

const GROUP = { id: 'rg-1', name: 'agents' } as ResourceGroup;

function renderTab() {
    const onUpdate = vi.fn();
    render(<GroupA2ALimitsTab group={GROUP} onUpdate={onUpdate} />);
    return { onUpdate };
}

const inputFor = (label: string) =>
    screen.getByText(label).closest('div')?.parentElement?.querySelector('input') as HTMLInputElement;

const saveButton = () => screen.getByRole('button', { name: /save/i });
const revertButton = () => screen.getByRole('button', { name: /revert/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockGetGroupA2ALimits.mockResolvedValue({ a2a_global_limits: {} });
    mockUpdateResourceGroup.mockResolvedValue({});
});

describe('GroupA2ALimitsTab — loading', () => {
    it('reads this group’s A2A limits', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 250 } },
        });
        renderTab();
        await waitFor(() => expect(mockGetGroupA2ALimits).toHaveBeenCalledWith('rg-1'));
        expect(await screen.findByDisplayValue('250')).toBeInTheDocument();
    });

    it('states the absolute proxy cap in the explainer', async () => {
        renderTab();
        expect(await screen.findByText(/refuses any chain above\s+32 delegations/)).toBeInTheDocument();
    });

    it('a failed load says why', async () => {
        mockGetGroupA2ALimits.mockRejectedValue(new Error('a2a limits unavailable'));
        renderTab();
        expect(await screen.findByText('a2a limits unavailable')).toBeInTheDocument();
    });

    it('passes the inherited and effective views down to the form', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 900 } },
            inherited_a2a_limits: { invocations: { daily_limit: 1000 } },
            effective_a2a_limits: { invocations: { daily_limit: 500 } },
        });
        renderTab();
        expect(await screen.findByText('Inherited: 1000')).toBeInTheDocument();
        expect(screen.getByText('Effective: 500')).toBeInTheDocument();
    });
});

describe('GroupA2ALimitsTab — saving', () => {
    it('Save and Revert stay disabled until something changes', async () => {
        renderTab();
        await screen.findByText('Daily limit');
        expect(saveButton()).toBeDisabled();
        expect(revertButton()).toBeDisabled();

        fireEvent.change(inputFor('Daily limit'), { target: { value: '10' } });
        expect(saveButton()).toBeEnabled();
        expect(revertButton()).toBeEnabled();
    });

    it('PATCHes the group and tells the caller', async () => {
        const { onUpdate } = renderTab();
        await screen.findByText('Daily limit');
        fireEvent.change(inputFor('Daily limit'), { target: { value: '250' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                a2a_global_limits: { invocations: { daily_limit: 250 } },
            }),
        );
        expect(mockToastSuccess).toHaveBeenCalledWith('A2A limits saved');
        await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    });

    it('strips cleared fields rather than persisting explicit nulls', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 100, monthly_limit: 2000 } },
        });
        renderTab();
        await screen.findByDisplayValue('100');
        fireEvent.change(inputFor('Daily limit'), { target: { value: '' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                a2a_global_limits: { invocations: { monthly_limit: 2000 } },
            }),
        );
    });

    it('drops a section emptied of every value instead of storing an empty husk', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 100 }, chain: { max_delegation_depth: 4 } },
        });
        renderTab();
        await screen.findByDisplayValue('100');
        fireEvent.change(inputFor('Daily limit'), { target: { value: '' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', {
                a2a_global_limits: { chain: { max_delegation_depth: 4 } },
            }),
        );
    });

    it('re-reads after a save — the merge may have tightened what was typed', async () => {
        renderTab();
        await screen.findByText('Daily limit');
        mockGetGroupA2ALimits.mockClear();

        fireEvent.change(inputFor('Daily limit'), { target: { value: '250' } });
        fireEvent.click(saveButton());
        await waitFor(() => expect(mockGetGroupA2ALimits).toHaveBeenCalledWith('rg-1'));
    });

    it('a failed save reports it and does NOT claim the limits were stored', async () => {
        mockUpdateResourceGroup.mockRejectedValue(new Error('exceeds inherited depth'));
        const { onUpdate } = renderTab();
        await screen.findByText('Daily limit');
        fireEvent.change(inputFor('Daily limit'), { target: { value: '250' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockToastError).toHaveBeenCalledWith('Save failed', 'exceeds inherited depth'),
        );
        expect(mockToastSuccess).not.toHaveBeenCalled();
        expect(onUpdate).not.toHaveBeenCalled();
        expect(saveButton()).toBeEnabled();
    });

    it('revert re-reads from the server rather than trusting a local snapshot', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 10 } },
        });
        renderTab();
        await screen.findByDisplayValue('10');
        fireEvent.change(inputFor('Daily limit'), { target: { value: '999' } });

        mockGetGroupA2ALimits.mockResolvedValue({
            a2a_global_limits: { invocations: { daily_limit: 42 } },
        });
        fireEvent.click(revertButton());
        await waitFor(() => expect(screen.getByDisplayValue('42')).toBeInTheDocument());
        expect(saveButton()).toBeDisabled();
    });
});

// A stored value of a setting the Control Plane now refuses is not
// sent again when the limits are saved.
describe('GroupA2ALimitsTab — a stored setting the Control Plane refuses', () => {
    it('a save sends none of it, and is accepted', async () => {
        mockGetGroupA2ALimits.mockResolvedValue({ a2a_global_limits: STORED_WITH_REFUSED.a2a_limits });
        mockUpdateResourceGroup.mockImplementation((_id: string, body: { a2a_global_limits?: unknown }) => {
            const refusal = controlPlaneRefusal('a2a_limits', body.a2a_global_limits);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve({});
        });
        const { onUpdate } = renderTab();
        expect(await screen.findByDisplayValue('100')).toBeInTheDocument();
        fireEvent.change(screen.getByDisplayValue('100'), { target: { value: '100' } });
        fireEvent.change(screen.getByDisplayValue('100'), { target: { value: '90' } });
        fireEvent.click(saveButton());
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        expect(mockUpdateResourceGroup).toHaveBeenCalledWith('rg-1', { a2a_global_limits: { invocations: { daily_limit: 90 } } });
        expect(mockToastError).not.toHaveBeenCalled();
    });
});
