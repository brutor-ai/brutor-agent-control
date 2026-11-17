/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → Approvals: how long a hold waits for a human.
 *
 * Found live: every hold no policy gave a window lapsed after 300 s, so an
 * approver who does not work nights could approve nothing raised after
 * dinner. Pinned: the tab says what applies and WHY (this group, an
 * ancestor, the tenant, the platform), writes seconds, clears with null, and
 * refuses — before Save — a window longer than the inherited ceiling (the
 * server's 400, min-wins).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockGet = vi.fn();
const mockPut = vi.fn();
const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock('../../../../hooks/useAuth', () => {
    const gatewayClient = {
        getApprovalWindow: (...a: unknown[]): unknown => mockGet(...a),
        updateApprovalWindow: (...a: unknown[]): unknown => mockPut(...a),
    };
    return { useAuth: () => ({ gatewayClient }) };
});

vi.mock('../../../common/Toast', () => ({
    useToast: () => ({ success: mockToastSuccess, error: mockToastError }),
}));

import GroupApprovalWindowTab from '../GroupApprovalWindowTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-sys', name: 'demo-system' } as ResourceGroup;

const BASE = {
    group_id: 'rg-sys',
    group_name: 'demo-system',
    approval_window_seconds: null,
    inherited_window_seconds: null,
    inherited_from_group_id: null,
    inherited_from_group_name: null,
    tenant_default_seconds: null,
    platform_default_seconds: 300,
    effective_seconds: 300,
    effective_source: 'platform',
    min_seconds: 60,
    max_seconds: 604800,
};

const input = () => screen.getByLabelText(/default approval window on this group/i);
const saveButton = () => screen.getByRole('button', { name: /save/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(BASE);
    mockPut.mockImplementation((_id: string, body: { approval_window_seconds: number | null }) =>
        Promise.resolve({
            ...BASE,
            approval_window_seconds: body.approval_window_seconds,
            effective_seconds: body.approval_window_seconds ?? 300,
            effective_source: body.approval_window_seconds ? 'resource_group' : 'platform',
        }),
    );
});

describe('GroupApprovalWindowTab', () => {
    it('says the platform default applies when nothing is set', async () => {
        render(<GroupApprovalWindowTab group={GROUP} onUpdate={vi.fn()} />);
        const line = await screen.findByTestId('approval-window-effective');
        expect(line).toHaveTextContent(/last 5 m 00 s — the platform default/);
        expect(mockGet).toHaveBeenCalledWith('rg-sys');
    });

    it('names the ancestor a window is inherited from', async () => {
        mockGet.mockResolvedValue({
            ...BASE,
            inherited_window_seconds: 86400,
            inherited_from_group_id: 'rg-org',
            inherited_from_group_name: 'Borealis',
            effective_seconds: 86400,
            effective_source: 'resource_group',
        });
        render(<GroupApprovalWindowTab group={GROUP} onUpdate={vi.fn()} />);
        const line = await screen.findByTestId('approval-window-effective');
        expect(line).toHaveTextContent(/last 1 d 0 h — inherited from 'Borealis'/);
    });

    it('saves minutes as seconds, then shows the new effective window', async () => {
        const onUpdate = vi.fn();
        render(<GroupApprovalWindowTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByTestId('approval-window-effective');
        fireEvent.click(screen.getByRole('button', { name: '24 hours' }));
        expect(input()).toHaveValue(1440);
        fireEvent.click(saveButton());
        await waitFor(() =>
            expect(mockPut).toHaveBeenCalledWith('rg-sys', { approval_window_seconds: 86400 }),
        );
        await waitFor(() => expect(onUpdate).toHaveBeenCalled());
        expect(screen.getByTestId('approval-window-effective')).toHaveTextContent(/set on this group/);
    });

    it('clearing sends null (inherit), not zero', async () => {
        mockGet.mockResolvedValue({ ...BASE, approval_window_seconds: 3600, effective_seconds: 3600, effective_source: 'resource_group' });
        render(<GroupApprovalWindowTab group={GROUP} onUpdate={vi.fn()} />);
        await screen.findByTestId('approval-window-effective');
        fireEvent.click(screen.getByRole('button', { name: 'Inherit' }));
        fireEvent.click(saveButton());
        await waitFor(() =>
            expect(mockPut).toHaveBeenCalledWith('rg-sys', { approval_window_seconds: null }),
        );
    });

    it('refuses a window longer than the inherited ceiling before Save', async () => {
        mockGet.mockResolvedValue({
            ...BASE,
            inherited_window_seconds: 3600,
            inherited_from_group_name: 'Org',
            effective_seconds: 3600,
            effective_source: 'resource_group',
        });
        render(<GroupApprovalWindowTab group={GROUP} onUpdate={vi.fn()} />);
        await screen.findByTestId('approval-window-effective');
        fireEvent.change(input(), { target: { value: '1440' } });
        expect(await screen.findByTestId('approval-window-problem')).toHaveTextContent(/only shorten/);
        expect(saveButton()).toBeDisabled();
        fireEvent.change(input(), { target: { value: '30' } });
        expect(screen.queryByTestId('approval-window-problem')).toBeNull();
        expect(saveButton()).not.toBeDisabled();
    });
});
