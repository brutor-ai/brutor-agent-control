/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the administrator profile test share: the gateway
 * and session doubles, a tenant administrator, and the ways to fill in the
 * change-password form. The page for a tenant administrator is a Community
 * test (`components/users/__tests__/TenantAdminProfile.test.tsx`); the page
 * for a system administrator, whose principal the Enterprise extension
 * contributes, is under `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes what they return from here.
 * This module does not import the page, so that a mock factory can await it.
 */

import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

export const request = vi.fn();
/** The client's own method for the change of password, as the client issues it: a POST through `request`. */
const changeOwnPassword = (passwords: unknown) =>
    request('/v1/admin/users/me/change-password', { method: 'POST', body: JSON.stringify(passwords) });
export const gateway = { gatewayClient: { request, changeOwnPassword } };

export interface TestUser {
    id?: string;
    username?: string;
    email?: string;
    full_name?: string;
    role_name?: string;
    role_id?: string;
    admin_type?: string;
    tenant_id?: string;
    tenant_display_name?: string;
    last_login?: string;
}
export const auth: { user: TestUser | null } = { user: null };

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => gateway });
/** `TwoFactorSetup`. */
export const twoFactorSetupModule = () => ({ TwoFactorSetup: () => <div>two factor setup</div> });

export const TENANT_ADMIN: TestUser = {
    id: 'adm-1',
    username: 'admin',
    email: 'admin@acme.test',
    full_name: 'Ada Admin',
    role_name: 'Tenant Admin',
    role_id: 'role-1',
    admin_type: 'tenant',
    tenant_id: 'default',
    tenant_display_name: 'Acme Corp',
    last_login: '2026-10-01T10:00:00Z',
};

export function field(label: string): HTMLInputElement {
    const input = screen.getByText(label).parentElement?.querySelector('input');
    if (!input) throw new Error(`no input for ${label}`);
    return input;
}

export function fill(current: string, next: string, confirm: string) {
    fireEvent.change(field('Current password'), { target: { value: current } });
    fireEvent.change(field('New password'), { target: { value: next } });
    fireEvent.change(field('Confirm new password'), { target: { value: confirm } });
}

export const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Update password' }));

export function row(label: string): string | null | undefined {
    return screen.getByText(label).nextElementSibling?.textContent;
}
