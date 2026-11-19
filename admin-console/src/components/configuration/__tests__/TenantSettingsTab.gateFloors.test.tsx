/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → AI System Assurance: the lifecycle gate's vocabulary states
 * every requirement the gate re-adds after a tenant override (its
 * `immutable_floors`), each with the condition under which it applies, so
 * the policy editor never suggests a requirement can be relaxed that the
 * gate will demand anyway.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const request = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: {
        request,
        getCurrentTenant: () => request('/v1/admin/tenants/current'),
        getGatePolicyVocabulary: () => request('/v1/admin/ai-systems/gate-policy/vocabulary'),
        listLLMs: () => request('/v1/admin/llms?mode=chat&enabled=true', { method: 'GET' }),
        updateCurrentTenantSettings: (body: unknown) => request('/v1/admin/tenants/current', { method: 'PATCH', body: JSON.stringify(body) }) } }),
}));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => vi.fn().mockResolvedValue(true) }));
vi.mock('../../common/Toast', () => ({ useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }) }));

import TenantSettingsTab from '../TenantSettingsTab';

const TENANT = {
    id: 't1', name: 't1', display_name: 'Tenant One', organization_name: 'Org', primary_contact_email: 'a@b.c',
    mfa_policy: 'optional', data_residency: null, data_retention: null,
};

/** The vocabulary as the Control Plane answers it. */
const VOCABULARY = {
    transitions: [{ key: 'approved->active', from_stage: 'approved', to_stage: 'active', default_requirements: ['contract_minted'] }],
    requirements: [
        { key: 'human_approval', label: 'A named human approved this transition' },
        { key: 'workload_identity_enforced', label: 'Machine callers authenticate with workload identity' },
    ],
    immutable_floors: [
        { condition: "eu_ai_act_risk_tier == 'high'", always_required: ['human_approval'], note: 'Re-added by the gate after any tenant override.' },
        { condition: "to_stage == 'active'", transitions: ['approved->active', 'deprecated->active'], always_required: ['workload_identity_enforced'], note: 'Re-added by the gate after any tenant override.' },
    ],
    current_policy: null,
};

beforeEach(() => {
    vi.clearAllMocks();
    request.mockImplementation((path: string) => {
        if (path === '/v1/admin/tenants/current') return Promise.resolve(TENANT);
        if (path === '/v1/admin/ai-systems/gate-policy/vocabulary') return Promise.resolve(VOCABULARY);
        if (path.startsWith('/v1/admin/llms')) return Promise.resolve({ items: [], models: [], total: 0 });
        return Promise.resolve({});
    });
});

describe('TenantSettingsTab: the gate requirements a tenant cannot relax', () => {
    it('states every floor of the vocabulary, with the condition it applies under', async () => {
        render(<TenantSettingsTab initialSubTab="assurance" />);
        const floors = await screen.findAllByTestId('gate-immutable-floor');
        expect(floors).toHaveLength(2);
        expect(floors[0]).toHaveTextContent('On a high-risk system');
        expect(floors[0]).toHaveTextContent('human_approval');
        expect(floors[1]).toHaveTextContent('On every way into active (approved->active, deprecated->active)');
        expect(floors[1]).toHaveTextContent('workload_identity_enforced');
        for (const floor of floors) expect(floor).toHaveTextContent('is re-added by the gate even if your policy omits it');
    });

    it('states none when the vocabulary has none', async () => {
        request.mockImplementation((path: string) => {
            if (path === '/v1/admin/tenants/current') return Promise.resolve(TENANT);
            if (path === '/v1/admin/ai-systems/gate-policy/vocabulary') return Promise.resolve({ ...VOCABULARY, immutable_floors: [] });
            return Promise.resolve({});
        });
        render(<TenantSettingsTab initialSubTab="assurance" />);
        await screen.findByText('Requirement keys');
        expect(screen.queryByTestId('gate-immutable-floor')).not.toBeInTheDocument();
    });
});
