/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant settings page offers no key of the tenant's own.
 *
 * Every stored secret is encrypted under the installation's encryption key
 * (`ENCRYPTION_KEY`), and the Control Plane's tenant record has no key
 * field: the page has no sub-tab for one and offers none.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const request = vi.fn();
const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
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
    useToast: () => toast,
}));
vi.mock('../TenantEvidencePanel', () => ({
    TenantEvidencePanel: () => <div>evidence panel</div>,
    default: () => <div>evidence panel</div>,
}));

import TenantSettingsTab from '../TenantSettingsTab';

const TENANT = {
    id: 't1',
    name: 't1',
    display_name: 'Tenant One',
    organization_name: 'Org',
    primary_contact_email: 'a@b.c',
    mfa_policy: 'optional',
    data_residency: null,
    data_retention: null,
};

async function open(initialSubTab?: string) {
    render(<TenantSettingsTab initialSubTab={initialSubTab} />);
    await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
    await screen.findByText('Profile');
}

/** The labels of the sub-tab strip, in order. */
function strip(): string[] {
    const bar = screen.getByRole('button', { name: 'Profile' }).parentElement!;
    return Array.from(bar.querySelectorAll('button')).map((b) => b.textContent ?? '');
}

const OFFERS_A_KEY = /BYOK|KMS|customer[- ]managed|key provider|master key/i;

describe('TenantSettingsTab — no key of the tenant\'s own', () => {
    beforeEach(() => {
        request.mockReset();
        Object.values(toast).forEach((fn) => fn.mockReset());
        request.mockImplementation((path: string, init?: RequestInit) => {
            if (path === '/v1/admin/tenants/current' && init?.method === 'PATCH') {
                const body = JSON.parse(init.body as string) as Record<string, unknown>;
                return Promise.resolve({ ...TENANT, ...body });
            }
            if (path === '/v1/admin/tenants/current') return Promise.resolve(TENANT);
            if (path.startsWith('/v1/admin/llms')) return Promise.resolve({ items: [], models: [], total: 0 });
            return Promise.resolve({});
        });
    });

    it('has no encryption sub-tab', async () => {
        await open();
        expect(strip()).toEqual([
            'Identity',
            'Profile',
            'Security',
            'Default Models',
            'Data Residency',
            'Data Retention',
            'AI System Assurance',
            'Evidence',
        ]);
        expect(strip().filter((label) => OFFERS_A_KEY.test(label))).toEqual([]);
    });

    it('a deep link to an encryption sub-tab opens on Identity', async () => {
        await open('encryption');
        expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Identity');
    });

    it('no sub-tab offers a key', async () => {
        await open();
        const shown: string[] = [];
        for (const label of strip()) {
            fireEvent.click(screen.getByRole('button', { name: label }));
            const fields = Array.from(document.body.querySelectorAll<HTMLInputElement>('input, textarea'));
            shown.push([document.body.textContent ?? '', ...fields.map((field) => field.value)].join('\n'));
        }
        expect(shown).toHaveLength(8);
        expect(shown.filter((text) => OFFERS_A_KEY.test(text))).toEqual([]);
    });
});
