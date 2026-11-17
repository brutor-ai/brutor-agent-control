/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `AuthProvider — hasPermission`: what its cases share. The cases for a
 * tenant administrator are Community tests (`hooks/__tests__/useAuth.test.tsx`);
 * the case for a system administrator, whose principal the Enterprise
 * extension contributes, is under `enterprise/src/host/`. Each renders the
 * provider with its own slot items.
 *
 * Import this from a test file only, never from a mock factory: it imports
 * the provider, whose API client the test file mocks (`authClient`).
 */

import { afterEach, beforeEach, expect, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import { SlotProvider } from '../../app/SlotProvider';
import type { SlotRegistry } from '../../app/slots';
import { AuthProvider, useAuth } from '../../hooks/useAuth';
import { mocks, seedStoredSession } from './authClient';

/** The set-up of the `hasPermission` cases: nothing stored, the console quiet. */
export function permissionSetup(): void {
    beforeEach(() => {
        localStorage.clear();
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });
}

/**
 * Signs `user` in (nobody, for `null`) and answers the element that says
 * whether they hold `permission`: `yes` or `no`. The session is a tenant
 * administrator's unless `adminType` names another kind (the id of a
 * contributed principal).
 *
 * That a system administrator holds every permission is contributed to the
 * provider (slot `app.principals`): rendered as the console
 * renders it, with the slot items of `slots`.
 */
export async function renderWithUser(
    slots: SlotRegistry,
    user: Record<string, unknown> | null,
    permission: string,
    adminType?: string,
) {
    function PermissionProbe() {
        const { hasPermission, isLoading } = useAuth();
        if (isLoading) return <div data-testid="allowed">loading</div>;
        return <span data-testid="allowed">{hasPermission(permission) ? 'yes' : 'no'}</span>;
    }
    if (user) {
        seedStoredSession(adminType);
        mocks.validateToken.mockResolvedValue({ valid: true, user });
    }
    render(
        <SlotProvider slots={slots}>
            <AuthProvider>
                <PermissionProbe />
            </AuthProvider>
        </SlotProvider>,
    );
    await waitFor(() =>
        expect(screen.getByTestId('allowed')).not.toHaveTextContent('loading'),
    );
    return screen.getByTestId('allowed');
}
