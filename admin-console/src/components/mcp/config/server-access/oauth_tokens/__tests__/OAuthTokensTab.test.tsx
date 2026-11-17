/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuth Tokens tab: diagnostics plus revocation, which cuts off a user's access.
 *
 * Pinned: nothing is revoked without confirmation, a bulk revoke sends the
 * filter the backend understands (and an unfiltered one is an empty predicate,
 * which means "everything"), a search narrows to per-row calls since the backend
 * cannot see it, and an IdP failure is a warning, not a clean success.
 *
 * Per-row failures during a searched bulk revoke are reported: swallowed, they
 * would give the operator a green "complete" while those tokens are still live.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gateway = {
    fetchOAuthTokens: vi.fn(),
    revokeOAuthToken: vi.fn(),
    revokeOAuthTokensBulk: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
const confirm = vi.fn();

vi.mock('../../../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../../../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import OAuthTokensTab from '../OAuthTokensTab';

function token(id: number, overrides: Record<string, unknown> = {}) {
    return {
        id,
        mcp_server_id: `srv-${id}`,
        mcp_server_name: `Server ${id}`,
        idp_id: 'idp-1',
        idp_name: 'Okta',
        gateway_username: `user${id}`,
        idp_user_id: `idp-user-${id}`,
        status: 'active',
        status_reason: `reason ${id}`,
        expires_at: new Date(Date.now() + 2 * 3600 * 1000).toISOString(),
        has_refresh_token: false,
        refresh_count: 0,
        last_refreshed_at: null,
        scopes: 'read write',
        ...overrides,
    };
}
const summaryOf = { total: 9, active: 3, expired_with_refresh: 2, expired_no_refresh: 1, revoked: 2, idp_unavailable: 1 };

async function renderTokens(tokens: unknown[] = [token(1), token(2)]) {
    gateway.fetchOAuthTokens.mockResolvedValue({ tokens, summary: summaryOf });
    render(<OAuthTokensTab />);
    await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenCalled());
    await screen.findAllByText(tokens.length ? /^srv-/ : 'No OAuth tokens');
}
const rowRevoke = (id: number) =>
    within(screen.getByText(`srv-${id}`).closest('tr') as HTMLElement).getByRole('button', { name: /Revoke/ });

describe('OAuthTokensTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        confirm.mockResolvedValue(true);
    });

    afterEach(cleanup);

    describe('display', () => {
        it('summary counts come from the server; rows show status, guidance and expiry', async () => {
            await renderTokens([
                token(1, {
                    has_refresh_token: true,
                    refresh_count: 4,
                    last_refreshed_at: new Date(Date.now() - 90 * 60 * 1000).toISOString(),
                }),
                token(2, {
                    status: 'expired_no_refresh',
                    gateway_username: null,
                    idp_name: null,
                    mcp_server_name: null,
                    scopes: null,
                    expires_at: new Date(Date.now() - 3 * 86400 * 1000).toISOString(),
                }),
            ]);
            expect(gateway.fetchOAuthTokens).toHaveBeenCalledWith({});
            const activeCard = screen.getByTitle('Show only Active');
            expect(within(activeCard).getByText('3')).toBeTruthy();
            expect(within(screen.getByTitle('Show only IdP unavailable')).getByText('1')).toBeTruthy();

            expect(screen.getByText('in 2h')).toBeTruthy();
            expect(screen.getByText('3d ago')).toBeTruthy();
            expect(screen.getByText('refreshed × 4')).toBeTruthy();
            expect(screen.getByText('last 2h ago')).toBeTruthy();
            expect(screen.getByText('reason 1')).toBeTruthy();
            // A user with no gateway account falls back to the IdP identity.
            expect(screen.getByText('idp-user-2')).toBeTruthy();
            expect(screen.getByText('None')).toBeTruthy();
        });

        it('an unparseable or missing expiry is shown as is, and a revoked row has no action', async () => {
            await renderTokens([
                token(1, { status: 'revoked', expires_at: null }),
                token(2, { status: 'idp_unavailable', expires_at: 'not-a-date' }),
            ]);
            expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
            expect(screen.getAllByText('not-a-date').length).toBe(2);
            const revokedRow = screen.getByText('srv-1').closest('tr') as HTMLElement;
            expect(within(revokedRow).queryByRole('button')).toBeNull();
            expect(within(revokedRow).getByText('revoked')).toBeTruthy();
            expect(rowRevoke(2)).toBeTruthy();
        });

        it('a load failure says why', async () => {
            gateway.fetchOAuthTokens.mockRejectedValue(new Error('forbidden'));
            render(<OAuthTokensTab />);
            expect(await screen.findByText('Failed to load OAuth tokens')).toBeTruthy();
            expect(screen.getByText('forbidden')).toBeTruthy();
        });

        it('tolerates a response with neither tokens nor summary', async () => {
            gateway.fetchOAuthTokens.mockResolvedValue({});
            render(<OAuthTokensTab />);
            expect(await screen.findByText('No user has authorized an MCP server in this tenant yet.')).toBeTruthy();
            expect(screen.queryByTitle('Revoke every token matching the current filters')).toBeNull();
        });
    });

    describe('filtering', () => {
        it('a summary card filters on the server; clicking again, or clear, drops it', async () => {
            await renderTokens();
            fireEvent.click(screen.getByTitle('Show only Revoked'));
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenLastCalledWith({ status: 'revoked' }));
            fireEvent.click(await screen.findByTitle('Clear filter'));
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenLastCalledWith({}));

            fireEvent.click(await screen.findByTitle('Show only Active'));
            fireEvent.click(await screen.findByText('Clear status filter'));
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenLastCalledWith({}));
        });

        it('search is local, covers server, user and scopes, and says when nothing matches', async () => {
            await renderTokens([token(1), token(2, { scopes: 'admin:all' })]);
            const box = screen.getByPlaceholderText(/Search by MCP server/);
            fireEvent.change(box, { target: { value: 'ADMIN:ALL' } });
            expect(screen.queryByText('srv-1')).toBeNull();
            expect(screen.getByText('srv-2')).toBeTruthy();
            fireEvent.change(box, { target: { value: 'user1' } });
            expect(screen.getByText('srv-1')).toBeTruthy();
            fireEvent.change(box, { target: { value: 'nope' } });
            expect(screen.getByText('No tokens match the current filters.')).toBeTruthy();
            expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(1);
        });

        it('Refresh refetches', async () => {
            await renderTokens();
            fireEvent.click(screen.getByText('Refresh', { selector: 'button' }));
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(2));
        });
    });

    describe('revoke one', () => {
        it('declined confirmation revokes nothing', async () => {
            confirm.mockResolvedValue(false);
            await renderTokens();
            fireEvent.click(rowRevoke(1));
            await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
            expect(confirm.mock.calls[0][0]).toMatchObject({ variant: 'danger', confirmLabel: 'Revoke token' });
            expect(gateway.revokeOAuthToken).not.toHaveBeenCalled();
        });

        it('names the user in the confirmation, revokes by id, confirms and reloads', async () => {
            await renderTokens();
            gateway.revokeOAuthToken.mockResolvedValue({ revoked_count: 1, results: [{ idp_revoked: true }] });
            fireEvent.click(rowRevoke(1));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith(
                'Token revoked',
                'Gateway and IdP revocation both succeeded for Server 1 (user1).',
            ));
            expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
                message: expect.stringContaining('"Server 1 (user1)"') as unknown as string,
            }));
            expect(gateway.revokeOAuthToken).toHaveBeenCalledWith(1);
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(2));
        });

        it('an IdP failure is a warning carrying its reason', async () => {
            await renderTokens([token(1, { mcp_server_name: null })]);
            gateway.revokeOAuthToken.mockResolvedValue({ revoked_count: 1, results: [{ idp_revoked: false, idp_error: 'idp 500' }] });
            fireEvent.click(rowRevoke(1));
            await waitFor(() => expect(toast.warning).toHaveBeenCalledWith(
                'Token revoked locally',
                expect.stringContaining('IdP revocation failed: idp 500'),
            ));
            expect(toast.success).not.toHaveBeenCalled();
        });

        it('a failed revoke reports, does not reload, and re-enables the row', async () => {
            await renderTokens();
            gateway.revokeOAuthToken.mockRejectedValue(new Error('denied'));
            fireEvent.click(rowRevoke(1));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Revoke failed', 'denied'));
            expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(1);
            await waitFor(() => expect(rowRevoke(1).hasAttribute('disabled')).toBe(false));
        });

        it('disables the row while in flight', async () => {
            await renderTokens();
            gateway.revokeOAuthToken.mockReturnValue(new Promise(() => {}));
            fireEvent.click(rowRevoke(1));
            await waitFor(() => expect(rowRevoke(1).hasAttribute('disabled')).toBe(true));
            expect(rowRevoke(2).hasAttribute('disabled')).toBe(false);
        });
    });

    describe('bulk revoke', () => {
        const bulkButton = () => screen.getByTitle('Revoke every token matching the current filters');

        it('counts only non-revoked rows; no filter sends an empty predicate after a clear warning', async () => {
            await renderTokens([token(1), token(2), token(3, { status: 'revoked' })]);
            expect(bulkButton().textContent).toContain('Revoke 2');
            gateway.revokeOAuthTokensBulk.mockResolvedValue({ revoked_count: 2, results: [{ idp_revoked: true }, { idp_revoked: true }] });

            fireEvent.click(bulkButton());

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Bulk revoke complete', expect.stringContaining('Revoked 2 tokens')));
            expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
                title: 'Revoke 2 OAuth tokens?',
                confirmLabel: 'Revoke 2 tokens',
                message: expect.stringContaining('every non-revoked token in this tenant') as unknown as string,
            }));
            expect(gateway.revokeOAuthTokensBulk).toHaveBeenCalledWith({ status_filter: undefined });
            expect(gateway.revokeOAuthToken).not.toHaveBeenCalled();
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(2));
        });

        it('declined confirmation sends nothing', async () => {
            confirm.mockResolvedValue(false);
            await renderTokens();
            fireEvent.click(bulkButton());
            await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
            expect(gateway.revokeOAuthTokensBulk).not.toHaveBeenCalled();
        });

        it('a status filter is forwarded, and named in the confirmation', async () => {
            await renderTokens();
            fireEvent.click(screen.getByTitle('Show only Expired — re-auth required'));
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenLastCalledWith({ status: 'expired_no_refresh' }));
            gateway.revokeOAuthTokensBulk.mockResolvedValue({ revoked_count: 1, results: [{ idp_revoked: false }] });

            fireEvent.click(await screen.findByTitle('Revoke every token matching the current filters'));

            await waitFor(() => expect(toast.warning).toHaveBeenCalledWith('Bulk revoke complete with IdP errors', expect.stringContaining('1 revoked locally')));
            expect(gateway.revokeOAuthTokensBulk).toHaveBeenCalledWith({ status_filter: 'expired_no_refresh' });
            expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
                message: expect.stringContaining('"expired no refresh"') as unknown as string,
            }));
        });

        it('a search revokes only the matching rows one by one, never the bulk endpoint', async () => {
            await renderTokens([token(1), token(2, { scopes: 'match-me' }), token(3, { scopes: 'match-me', status: 'revoked' })]);
            fireEvent.change(screen.getByPlaceholderText(/Search by MCP server/), { target: { value: 'match-me' } });
            gateway.revokeOAuthToken.mockResolvedValue({ revoked_count: 1, results: [{ idp_revoked: true }] });

            fireEvent.click(bulkButton());

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Bulk revoke complete', expect.stringContaining('Revoked 1 token')));
            expect(gateway.revokeOAuthToken.mock.calls).toEqual([[2]]);
            expect(gateway.revokeOAuthTokensBulk).not.toHaveBeenCalled();
        });

        it('a searched revoke with failures says tokens are still live, not "complete"', async () => {
            await renderTokens([token(1), token(2), token(3)]);
            fireEvent.change(screen.getByPlaceholderText(/Search by MCP server/), { target: { value: 'read' } });
            gateway.revokeOAuthToken
                .mockRejectedValueOnce(new Error('x'))
                .mockResolvedValueOnce({ revoked_count: 1, results: [{ idp_revoked: true }] })
                .mockRejectedValueOnce(new Error('y'));

            fireEvent.click(bulkButton());

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
                'Bulk revoke incomplete',
                '2 tokens are still active: revocation failed (1× x; 1× y). 1 revoked.',
            ));
            expect(gateway.revokeOAuthToken).toHaveBeenCalledTimes(3);
            expect(toast.success).not.toHaveBeenCalled();
            expect(toast.warning).not.toHaveBeenCalled();
            await waitFor(() => expect(gateway.fetchOAuthTokens).toHaveBeenCalledTimes(2));
        });

        it('the reasons of the failures are told once each with their count, the most frequent first, and no more than three kinds', async () => {
            await renderTokens([token(1), token(2), token(3), token(4), token(5), token(6)]);
            fireEvent.change(screen.getByPlaceholderText(/Search by MCP server/), { target: { value: 'read' } });
            gateway.revokeOAuthToken
                .mockRejectedValueOnce(new Error('forbidden'))
                .mockRejectedValueOnce(new Error('The identity provider is not reachable'))
                .mockRejectedValueOnce(new Error('forbidden'))
                .mockRejectedValueOnce(new Error('timeout'))
                .mockRejectedValueOnce({})
                .mockRejectedValueOnce(new Error('forbidden'));

            fireEvent.click(bulkButton());

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
                'Bulk revoke incomplete',
                '6 tokens are still active: revocation failed (3× forbidden; 1× The identity provider is not reachable; 1× timeout; and 1 other reason). 0 revoked.',
            ));
        });

        it('a failed bulk call reports it', async () => {
            await renderTokens();
            gateway.revokeOAuthTokensBulk.mockRejectedValue(new Error('boom'));
            fireEvent.click(bulkButton());
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Bulk revoke failed', 'boom'));
            expect(toast.success).not.toHaveBeenCalled();
        });
    });
});
