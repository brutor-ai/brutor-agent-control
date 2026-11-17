/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Data Retention sub-tab.
 *
 * The behaviours that matter: a save from another sub-tab must not
 * round-trip the policy (touched-gate), a policy the operator sets is sent
 * as the PATCH's `data_retention`, turning it off sends an explicit null,
 * and the one contradiction the server rejects (body window longer than
 * log window) is flagged before the operator hits save.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const request = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: {
        request,
        // The client's own methods, as the client issues them: through `request`.
        getCurrentTenant: () => request('/v1/admin/tenants/current'),
        getGatePolicyVocabulary: () => request('/v1/admin/ai-systems/gate-policy/vocabulary'),
        listLLMs: () => request('/v1/admin/llms?mode=chat&enabled=true', { method: 'GET' }),
        updateCurrentTenantSettings: (body: unknown) => request('/v1/admin/tenants/current', { method: 'PATCH', body: JSON.stringify(body) }) } }),
}));
vi.mock('../../common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));
vi.mock('../../common/Toast', () => ({
    useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
}));

import TenantSettingsTab from '../TenantSettingsTab';

const tenant = (over: Record<string, unknown> = {}) => ({
    id: 't1',
    name: 't1',
    display_name: 'Tenant One',
    organization_name: 'Org',
    primary_contact_email: 'a@b.c',
    mfa_policy: 'optional',
    data_residency: null,
    data_retention: null,
    ...over,
});

function wireRequests(current: Record<string, unknown>) {
    request.mockImplementation(async (path: string, init?: RequestInit) => {
        if (path === '/v1/admin/tenants/current') return current;
        if (path.startsWith('/v1/admin/llms')) return { items: [], models: [], total: 0 };
        if (path.endsWith('/kb-stats')) throw new Error('no kb');
        if (path === '/v1/admin/tenants/current' && init?.method === 'PATCH') {
            return { ...current, ...JSON.parse(String(init.body)) };
        }
        return {};
    });
}

async function openRetention() {
    render(<TenantSettingsTab />);
    await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
    fireEvent.click(await screen.findByText('Data Retention'));
    return screen.findByTestId('retention-tab');
}

function lastPatchBody(): Record<string, unknown> {
    const patch = request.mock.calls.filter(
        ([p, init]) => p === '/v1/admin/tenants/current' && (init as RequestInit)?.method === 'PATCH',
    );
    expect(patch.length).toBeGreaterThan(0);
    return JSON.parse(String((patch[patch.length - 1][1] as RequestInit).body));
}

describe('TenantSettingsTab — Data Retention', () => {
    beforeEach(() => {
        request.mockReset();
    });

    it('shows the stored policy and sends the edit as data_retention', async () => {
        wireRequests(tenant({ data_retention: { store_bodies: 'full', body_days: 30, log_days: 90, run_days: null } }));
        await openRetention();

        expect(screen.getByTestId('retention-enabled')).toBeChecked();
        expect(screen.getByTestId('retention-body_days')).toHaveValue(30);
        expect(screen.getByTestId('retention-log_days')).toHaveValue(90);

        fireEvent.change(screen.getByTestId('retention-store-bodies'), { target: { value: 'redacted' } });
        fireEvent.change(screen.getByTestId('retention-run_days'), { target: { value: '180' } });
        fireEvent.click(screen.getByText('Save changes'));

        await waitFor(() => {
            expect(lastPatchBody().data_retention).toEqual({
                store_bodies: 'redacted',
                body_days: 30,
                log_days: 90,
                run_days: 180,
            });
        });
    });

    it('does not round-trip the policy when another tab is saved', async () => {
        wireRequests(tenant({ data_retention: { store_bodies: 'none' } }));
        render(<TenantSettingsTab />);
        await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
        fireEvent.click(await screen.findByText('Profile'));
        fireEvent.click(await screen.findByText('Save changes'));
        await waitFor(() => expect(lastPatchBody()).not.toHaveProperty('data_retention'));
    });

    it('turning the policy off sends an explicit null', async () => {
        wireRequests(tenant({ data_retention: { store_bodies: 'redacted', log_days: 30 } }));
        await openRetention();
        fireEvent.click(screen.getByTestId('retention-enabled'));
        fireEvent.click(screen.getByText('Save changes'));
        await waitFor(() => expect(lastPatchBody().data_retention).toBeNull());
    });

    it('flags a body window longer than the log window before save', async () => {
        wireRequests(tenant());
        await openRetention();
        fireEvent.click(screen.getByTestId('retention-enabled'));
        fireEvent.change(screen.getByTestId('retention-log_days'), { target: { value: '30' } });
        fireEvent.change(screen.getByTestId('retention-body_days'), { target: { value: '90' } });
        expect(screen.getByTestId('retention-window-error')).toBeInTheDocument();
        fireEvent.change(screen.getByTestId('retention-body_days'), { target: { value: '10' } });
        expect(screen.queryByTestId('retention-window-error')).toBeNull();
    });
});
