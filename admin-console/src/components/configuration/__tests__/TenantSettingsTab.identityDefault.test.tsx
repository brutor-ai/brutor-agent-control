/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant offers the tenant identity default on its own sub-tab,
 * handed the tenant record as read. The sub-tab saves itself: the page's
 * save bar is not shown there, and a save elsewhere never sends the field.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const request = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();
const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: {
        request,
        getCurrentTenant: () => request('/v1/admin/tenants/current'),
        getGatePolicyVocabulary: () => request('/v1/admin/ai-systems/gate-policy/vocabulary'),
        listLLMs: () => request('/v1/admin/llms?mode=chat&enabled=true', { method: 'GET' }),
        updateCurrentTenantSettings: (body: unknown) => request('/v1/admin/tenants/current', { method: 'PATCH', body: JSON.stringify(body) }) } }),
}));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => vi.fn().mockResolvedValue(true) }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../TenantIdentityDefaultPanel', () => ({
    default: (p: { tenant: { identity_policy_default?: unknown } }) => (
        <div data-testid="identity-default-panel">{JSON.stringify(p.tenant.identity_policy_default)}</div>
    ),
}));

import TenantSettingsTab from '../TenantSettingsTab';

const TENANT = {
    id: 't1', name: 't1', display_name: 'Tenant One', organization_name: 'Org', primary_contact_email: 'a@b.c',
    mfa_policy: 'optional', data_residency: null, data_retention: null,
    identity_policy_default: { mode: 'enforce' },
};

beforeEach(() => {
    vi.clearAllMocks();
    request.mockImplementation((path: string, init?: RequestInit) => {
        if (path === '/v1/admin/tenants/current' && init?.method === 'PATCH') return Promise.resolve(TENANT);
        if (path === '/v1/admin/tenants/current') return Promise.resolve(TENANT);
        if (path.startsWith('/v1/admin/llms')) return Promise.resolve({ items: [], models: [], total: 0 });
        return Promise.resolve({});
    });
});

describe('TenantSettingsTab: Workload Identity', () => {
    it('shows the tenant identity default panel with the tenant as read, and no save bar', async () => {
        render(<TenantSettingsTab />);
        await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
        fireEvent.click(await screen.findByText('Workload Identity'));
        expect(await screen.findByTestId('identity-default-panel')).toHaveTextContent('"mode":"enforce"');
        expect(screen.queryByRole('button', { name: /^save changes$/i })).not.toBeInTheDocument();
    });

    it('opens on it from a deep link', async () => {
        render(<TenantSettingsTab initialSubTab="workload-identity" />);
        expect(await screen.findByTestId('identity-default-panel')).toBeInTheDocument();
    });
});
