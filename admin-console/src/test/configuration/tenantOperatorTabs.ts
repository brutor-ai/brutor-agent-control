/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of `TenantSettingsTab.operatorTabs.test` share: a
 * tenant with quotas, features and billing, and the sub-tab strip. The
 * Community half is the page with nothing contributed; the operator sub-tabs
 * are the Enterprise extension's, and their cases are under
 * `enterprise/src/host/`.
 */

import { screen } from '@testing-library/react';

import { request, resetTenantSettingsDoubles } from './tenantSettingsTab';

export const TENANT = {
    id: 't1',
    name: 't1',
    display_name: 'Tenant One',
    organization_name: 'Org',
    primary_contact_email: 'a@b.c',
    mfa_policy: 'optional',
    data_residency: null,
    data_retention: null,
    quotas: { max_users: 25 },
    features: { knowledge_base: false },
    billing_plan: 'enterprise',
    billing_email: 'billing@example.com',
};

/** How every case starts: the tenant answered, no model, no knowledge base. For a `beforeEach`. */
export function resetOperatorTabs(): void {
    resetTenantSettingsDoubles();
    request.mockImplementation((path: string) => {
        if (path === '/v1/admin/tenants/current') return Promise.resolve(TENANT);
        if (path.startsWith('/v1/admin/llms')) return Promise.resolve({ items: [], models: [], total: 0 });
        if (path.endsWith('/kb-stats')) return Promise.reject(new Error('no kb'));
        return Promise.resolve({});
    });
}

/** The labels of the sub-tab strip, in order. */
export function strip(): string[] {
    const bar = screen.getByRole('button', { name: 'Profile' }).parentElement!;
    return Array.from(bar.querySelectorAll('button')).map((b) => b.textContent ?? '');
}

export const heading = () => screen.getByRole('heading', { level: 3 }).textContent;
