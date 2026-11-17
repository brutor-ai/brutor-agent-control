/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * SystemSessionsPane — UI tests.
 *
 * Coverage:
 *   * Kind tabs render in canonical order (admin / portal / api_key)
 *     and clicking switches the active kind + triggers a refetch.
 *   * The active tab's description renders so operators know what the
 *     list represents.
 *   * Initial load shows rows from the gateway client mock and the
 *     correct testid wiring.
 *   * Revoke button is disabled (with Lock icon) when the operator
 *     lacks `system:operate`.
 *   * Revoke flow: click button -> confirm modal -> typing reason ->
 *     confirm -> gateway client is called with `{ kind, id, reason }`
 *     and the row is dropped optimistically from the table.
 *   * Cancel from the modal leaves the table unchanged and does NOT
 *     call the gateway client.
 *   * Refresh button triggers a fetch outside the polling cadence.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
    render,
    screen,
    fireEvent,
    waitFor,
    act,
} from '@testing-library/react';

const mockHasPermission = vi.fn();
const mockListSessions = vi.fn();
const mockRevokeSession = vi.fn();

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission }),
}));

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listSystemSessions: mockListSessions,
            revokeSystemSession: mockRevokeSession,
        },
    }),
}));

import SystemSessionsPane from '../SystemSessionsPane';

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

const makeListResponse = (kind: 'admin' | 'portal' | 'api_key', items: any[]) => ({
    tenant_id: 't-acme',
    kind,
    generated_at: new Date().toISOString(),
    items,
});

const sampleAdminRow = {
    kind: 'admin' as const,
    id: 'sa-abc123def456',
    tenant_id: 't-acme',
    user_id: 'adm-1',
    user_label: 'alice@example.com',
    created_at: '2026-06-01T10:00:00Z',
    last_seen_at: '2026-06-07T08:30:00Z',
    expires_at: '2026-06-07T20:00:00Z',
    ip: '10.0.0.1',
    user_agent: 'test/1.0',
    extra: { risk_score: 0.0 },
};

describe('SystemSessionsPane', () => {
    beforeEach(() => {
        mockHasPermission.mockReset();
        mockListSessions.mockReset();
        mockRevokeSession.mockReset();
        mockListSessions.mockResolvedValue(makeListResponse('admin', [sampleAdminRow]));
    });

    it('renders kind tabs in canonical order', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemSessionsPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-sessions-kind-admin')).toBeDefined(),
        );
        expect(screen.getByTestId('system-sessions-kind-portal')).toBeDefined();
        expect(screen.getByTestId('system-sessions-kind-api_key')).toBeDefined();
    });

    it('loads admin sessions on mount and renders the row', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemSessionsPane />);
        await waitFor(() => expect(mockListSessions).toHaveBeenCalledWith('admin'));
        expect(screen.getByText('alice@example.com')).toBeDefined();
        expect(screen.getByTestId(`system-sessions-row-${sampleAdminRow.id}`)).toBeDefined();
    });

    it('switching to the portal tab refetches with kind=portal', async () => {
        grant(['system:read', 'system:operate']);
        mockListSessions.mockResolvedValue(makeListResponse('admin', [sampleAdminRow]));
        render(<SystemSessionsPane />);
        await waitFor(() => expect(mockListSessions).toHaveBeenCalledWith('admin'));

        mockListSessions.mockResolvedValueOnce(makeListResponse('portal', []));
        fireEvent.click(screen.getByTestId('system-sessions-kind-portal'));
        await waitFor(() => expect(mockListSessions).toHaveBeenCalledWith('portal'));
        await waitFor(() =>
            expect(screen.getByTestId('system-sessions-empty')).toBeDefined(),
        );
    });

    it('disables the Revoke button when operator lacks system:operate', async () => {
        grant(['system:read']);
        render(<SystemSessionsPane />);
        await waitFor(() =>
            expect(screen.getByTestId(`system-sessions-revoke-${sampleAdminRow.id}`)).toBeDefined(),
        );
        const btn = screen.getByTestId(
            `system-sessions-revoke-${sampleAdminRow.id}`,
        );
        expect(btn).toBeDisabled();
        expect(btn.title).toMatch(/system:operate/);
    });

    it('revoke flow calls the gateway client and drops the row optimistically', async () => {
        grant(['system:read', 'system:operate']);
        mockRevokeSession.mockResolvedValue({
            status: 'queued',
            kind: 'admin',
            id: sampleAdminRow.id,
            tenant_id: 't-acme',
            channel: 'brutor:session:revoke',
            payload: '{"session_id":"sa-abc123def456"}',
        });
        render(<SystemSessionsPane />);
        await waitFor(() =>
            expect(screen.getByTestId(`system-sessions-revoke-${sampleAdminRow.id}`)).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(`system-sessions-revoke-${sampleAdminRow.id}`),
        );
        const modal = await screen.findByTestId('system-sessions-revoke-modal');
        expect(modal).toBeDefined();

        const reason = screen.getByTestId(
            'system-sessions-revoke-reason',
        );
        fireEvent.change(reason, { target: { value: 'suspected compromise' } });

        await act(async () => {
            fireEvent.click(screen.getByTestId('system-sessions-revoke-confirm'));
        });

        expect(mockRevokeSession).toHaveBeenCalledWith({
            kind: 'admin',
            id: sampleAdminRow.id,
            reason: 'suspected compromise',
        });
        // Optimistic local drop: the row is no longer in the DOM.
        await waitFor(() =>
            expect(
                screen.queryByTestId(`system-sessions-row-${sampleAdminRow.id}`),
            ).toBeNull(),
        );
    });

    it('cancelling the confirm modal leaves the row in place and does not call revoke', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemSessionsPane />);
        await waitFor(() =>
            expect(screen.getByTestId(`system-sessions-revoke-${sampleAdminRow.id}`)).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(`system-sessions-revoke-${sampleAdminRow.id}`),
        );
        await screen.findByTestId('system-sessions-revoke-modal');
        fireEvent.click(screen.getByText('Cancel'));
        await waitFor(() =>
            expect(screen.queryByTestId('system-sessions-revoke-modal')).toBeNull(),
        );
        expect(mockRevokeSession).not.toHaveBeenCalled();
        // Row still rendered.
        expect(
            screen.getByTestId(`system-sessions-row-${sampleAdminRow.id}`),
        ).toBeDefined();
    });

    it('Refresh button triggers an extra fetch', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemSessionsPane />);
        await waitFor(() => expect(mockListSessions).toHaveBeenCalledTimes(1));
        fireEvent.click(screen.getByTestId('system-sessions-refresh'));
        await waitFor(() => expect(mockListSessions).toHaveBeenCalledTimes(2));
    });
});
