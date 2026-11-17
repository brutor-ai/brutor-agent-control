/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Admin sessions: the "account compromised, device lost" screen.
 *
 * Pinned: the revoke goes to the right session id, revoking your own session
 * warns differently and ends in sign-out rather than a doomed refresh, a declined
 * confirmation sends nothing, and a failed revoke is shown with the session still
 * listed. The Actions column must also not call a logged-out or expired session
 * "Revoked": on an incident screen that reads as an action someone took.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const SESSIONS = '/v1/admin/users/tenant/sessions';
const request = vi.fn();
const logout = vi.fn();
const confirm = vi.fn();
/** The client's own methods for the sessions, as the client issues them: through `request`. */
const gateway = {
    gatewayClient: {
        request,
        listTenantAdminSessions: (query?: { include_inactive?: boolean }) =>
            query?.include_inactive ? request(`${SESSIONS}?include_inactive=true`) : request(SESSIONS),
        revokeTenantAdminSession: (id: string) => request(`${SESSIONS}/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),
    },
};
const auth = { logout };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import TenantAdminSessions from '../TenantAdminSessions';

const LIST = '/v1/admin/users/tenant/sessions';

function session(id: string, over: Record<string, unknown> = {}) {
    return {
        id,
        tenant_id: 't-1',
        admin_id: `adm-${id}`,
        admin_username: `user-${id}`,
        admin_email: `${id}@acme.test`,
        ip_address: '10.0.0.1',
        user_agent: 'Mozilla',
        risk_score: 0,
        created_at: '2026-10-01T10:00:00Z',
        last_activity: '2026-10-01T11:00:00Z',
        expires_at: '2026-10-02T10:00:00Z',
        is_active: true,
        is_expired: false,
        deactivation_reason: null,
        is_current: false,
        ...over,
    };
}

function serveList(...sessions: unknown[]) {
    request.mockImplementation((url: string) =>
        Promise.resolve(url.startsWith(LIST) && !url.endsWith('/revoke') ? { sessions } : {}),
    );
}

function rowOf(username: string): HTMLElement {
    const row = screen.getByText(username).closest('tr');
    if (!row) throw new Error(`no row for ${username}`);
    return row;
}

function listCalls(): number {
    return (request.mock.calls as unknown as Array<[string]>).filter(([url]) => !url.endsWith('/revoke')).length;
}

describe('TenantAdminSessions', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        confirm.mockResolvedValue(true);
        logout.mockResolvedValue(undefined);
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('list', () => {
        it('reads active sessions by default and adds inactive only when asked', async () => {
            serveList(session('a'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');
            expect(request).toHaveBeenCalledWith(LIST);

            fireEvent.click(screen.getByRole('checkbox', { name: 'Include inactive' }));
            await waitFor(() => expect(request).toHaveBeenCalledWith(`${LIST}?include_inactive=true`));
        });

        it('sorts this device first, then active newest first, then inactive', async () => {
            serveList(
                session('inactive', { is_active: false, created_at: '2026-10-09T00:00:00Z' }),
                session('old', { created_at: '2026-10-01T00:00:00Z' }),
                session('mine', { is_current: true, created_at: '2026-09-01T00:00:00Z' }),
                session('new', { created_at: '2026-10-05T00:00:00Z' }),
                session('undated', { created_at: null }),
            );
            render(<TenantAdminSessions />);
            await screen.findByText('user-mine');

            const order = screen
                .getAllByRole('row')
                .slice(1)
                .map((tr) => tr.querySelector('td div')?.textContent);
            expect(order).toEqual(['user-mine', 'user-new', 'user-old', 'user-undated', 'user-inactive']);
        });

        it('badges this device, expiry, the deactivation reason and plain active', async () => {
            serveList(
                session('mine', { is_current: true }),
                session('live'),
                session('stale', { is_expired: true }),
                session('ended', { is_active: false, deactivation_reason: 'revoked_by_admin' }),
                session('bare', { is_active: false }),
            );
            render(<TenantAdminSessions />);
            await screen.findByText('user-mine');

            expect(within(rowOf('user-mine')).getByText('This device')).toBeInTheDocument();
            expect(within(rowOf('user-live')).getByText('Active')).toBeInTheDocument();
            expect(within(rowOf('user-stale')).getByText('Expired')).toBeInTheDocument();
            expect(within(rowOf('user-ended')).getByText('revoked_by_admin')).toBeInTheDocument();
            expect(within(rowOf('user-bare')).getAllByText('Inactive').length).toBeGreaterThan(0);
        });

        it('offers revoke only on active sessions, and does not call ended ones revoked', async () => {
            serveList(
                session('live'),
                session('stale', { is_expired: true }),
                session('out', { is_active: false, deactivation_reason: 'user_logout' }),
            );
            render(<TenantAdminSessions />);
            await screen.findByText('user-live');

            expect(within(rowOf('user-live')).getByRole('button', { name: 'Revoke' })).toBeInTheDocument();
            // Still has a live row server-side until it is swept: revocable.
            expect(within(rowOf('user-stale')).getByRole('button', { name: 'Revoke' })).toBeInTheDocument();
            const out = within(rowOf('user-out'));
            expect(out.queryByRole('button')).not.toBeInTheDocument();
            expect(out.getByText('user_logout')).toBeInTheDocument();
            expect(out.queryByText('Revoked')).not.toBeInTheDocument();
            expect(out.getByText('Inactive')).toBeInTheDocument();
        });

        it('falls back to the admin id and dashes for unknown fields', async () => {
            serveList(
                session('z', {
                    admin_username: null,
                    admin_email: null,
                    ip_address: null,
                    user_agent: null,
                    created_at: null,
                }),
            );
            render(<TenantAdminSessions />);

            await screen.findByText('adm-z');
            const row = within(rowOf('adm-z'));
            expect(row.getAllByText('—').length).toBe(3);
            expect(row.queryByText('z@acme.test')).not.toBeInTheDocument();
        });

        it('shows the empty state', async () => {
            serveList();
            render(<TenantAdminSessions />);
            expect(await screen.findByText('No sessions to show.')).toBeInTheDocument();
        });

        it('shows a load failure', async () => {
            request.mockRejectedValue(new Error('forbidden'));
            render(<TenantAdminSessions />);
            expect(await screen.findByText('forbidden')).toBeInTheDocument();
        });

        it('keeps the rows on screen when a refresh fails', async () => {
            serveList(session('a'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');

            request.mockRejectedValue(new Error('refresh failed'));
            fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

            expect(await screen.findByText('refresh failed')).toBeInTheDocument();
            expect(screen.getByText('user-a')).toBeInTheDocument();
        });
    });

    describe('revoke', () => {
        it('confirms naming the target, POSTs its id, and refreshes in place', async () => {
            serveList(session('a'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');

            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            await waitFor(() =>
                expect(request).toHaveBeenCalledWith(`${LIST}/a/revoke`, { method: 'POST' }),
            );
            expect(confirm).toHaveBeenCalledWith(
                expect.objectContaining({ title: 'Revoke session?', variant: 'danger', confirmLabel: 'Revoke' }),
            );
            await waitFor(() => expect(listCalls()).toBe(2));
            expect(logout).not.toHaveBeenCalled();
        });

        it('names the target in the confirmation', async () => {
            let message: unknown;
            confirm.mockImplementation((opts: { message: unknown }) => {
                message = opts.message;
                return Promise.resolve(false);
            });
            serveList(session('a', { ip_address: '192.0.2.7' }));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');
            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
            await waitFor(() => expect(confirm).toHaveBeenCalled());

            cleanup();
            render(<div>{message as React.ReactNode}</div>);
            expect(screen.getByText('user-a')).toBeInTheDocument();
            expect(screen.getByText('192.0.2.7')).toBeInTheDocument();
        });

        it('encodes the session id into the path', async () => {
            serveList(session('a/b'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a/b');
            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            await waitFor(() =>
                expect(request).toHaveBeenCalledWith(`${LIST}/a%2Fb/revoke`, { method: 'POST' }),
            );
        });

        it('sends nothing when the confirmation is declined', async () => {
            confirm.mockResolvedValue(false);
            serveList(session('a'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');

            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(request).toHaveBeenCalledTimes(1);
        });

        it('warns before revoking your own session, then signs out instead of refreshing', async () => {
            serveList(session('mine', { is_current: true }));
            render(<TenantAdminSessions />);
            await screen.findByText('user-mine');

            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
            expect(confirm).toHaveBeenCalledWith(
                expect.objectContaining({
                    title: 'Sign out of this device?',
                    variant: 'warning',
                    confirmLabel: 'Sign out',
                }),
            );
            expect(request).toHaveBeenCalledWith(`${LIST}/mine/revoke`, { method: 'POST' });
            // The token just died; a follow-up list call could only 401.
            expect(listCalls()).toBe(1);
        });

        it('does not sign out if revoking your own session failed', async () => {
            serveList(session('mine', { is_current: true }));
            render(<TenantAdminSessions />);
            await screen.findByText('user-mine');

            request.mockRejectedValue(new Error('still active'));
            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            expect(await screen.findByText('still active')).toBeInTheDocument();
            expect(logout).not.toHaveBeenCalled();
        });

        it('shows a failed revoke with the session still listed and the button back', async () => {
            serveList(session('a'));
            render(<TenantAdminSessions />);
            await screen.findByText('user-a');

            request.mockRejectedValue(new Error('Session not found'));
            fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

            expect(await screen.findByText('Session not found')).toBeInTheDocument();
            expect(screen.getByText('user-a')).toBeInTheDocument();
            await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke' })).toBeEnabled());
        });
    });
});
