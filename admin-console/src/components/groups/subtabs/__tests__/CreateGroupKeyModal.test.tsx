/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Create API Key for a resource group.
 *
 * Pinned: the payload for each access mode (a shared key must never carry a
 * user id, even if one was picked before switching back), the agent binding, and
 * that the freshly minted key reaches the parent, since that is the one moment
 * its secret can be shown.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import { GatewayAPIError } from '../../../../lib/gatewayApi/base';

const gatewayClient = { listAgentIdentities: vi.fn() };
const gateway = { createGroupAPIKey: vi.fn(), fetchEndUsers: vi.fn(), gatewayClient };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import CreateGroupKeyModal from '../CreateGroupKeyModal';

const GROUP = { id: 'rg-1', name: 'eng', display_name: 'Engineering' } as ResourceGroup;
const onClose = vi.fn();
const onKeyCreated = vi.fn();

const ALICE = { id: 7, email: 'alice@acme.test', first_name: 'Alice', last_name: 'Lund', is_active: true, is_verified: true };
const BOB = { id: 8, email: 'bob@acme.test', is_active: true, is_verified: true };

const renderModal = () => render(<CreateGroupKeyModal group={GROUP} onClose={onClose} onKeyCreated={onKeyCreated} />);
const typeName = (v: string) =>
    fireEvent.change(screen.getByPlaceholderText('e.g., Production API Key'), { target: { value: v } });
const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));
const userRadio = () => screen.getByRole('radio', { name: /User-Specific Access/ });
const search = () => screen.findByPlaceholderText('Search by name or email...');

describe('CreateGroupKeyModal', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        gateway.createGroupAPIKey.mockReset();
        gateway.fetchEndUsers.mockReset();
        gatewayClient.listAgentIdentities.mockReset();
        gateway.createGroupAPIKey.mockResolvedValue({ id: 'k1', key: 'sk-secret' });
        gateway.fetchEndUsers.mockResolvedValue({ users: [ALICE, BOB] });
        gatewayClient.listAgentIdentities.mockResolvedValue({ agents: [{ id: 'ag-1', name: 'Scout' }] });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('creates a shared key, omitting everything left blank, and hands it to the parent', async () => {
        renderModal();
        expect(screen.getByText('Engineering')).toBeInTheDocument();
        typeName('  CI key  ');
        submit();
        await waitFor(() =>
            expect(gateway.createGroupAPIKey).toHaveBeenCalledWith('rg-1', {
                name: 'CI key',
                description: undefined,
                access_mode: 'shared',
                user_id: undefined,
                expires_at: undefined,
                agent_id: undefined,
            }),
        );
        expect(onKeyCreated).toHaveBeenCalledWith({ id: 'k1', key: 'sk-secret' });
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(gateway.fetchEndUsers).not.toHaveBeenCalled();
    });

    it('sends description, expiry and the agent binding', async () => {
        renderModal();
        await screen.findByRole('option', { name: 'Scout' });
        typeName('agent key');
        fireEvent.change(screen.getByPlaceholderText('What will this key be used for?'), { target: { value: ' for Scout ' } });
        fireEvent.change(document.querySelector('input[type="datetime-local"]') as HTMLInputElement, {
            target: { value: '2027-01-01T00:00' },
        });
        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ag-1' } });
        submit();
        await waitFor(() =>
            expect(gateway.createGroupAPIKey).toHaveBeenCalledWith('rg-1', {
                name: 'agent key',
                description: 'for Scout',
                access_mode: 'shared',
                user_id: undefined,
                expires_at: '2027-01-01T00:00',
                agent_id: 'ag-1',
            }),
        );
    });

    it('a blank name is refused', async () => {
        renderModal();
        typeName('   ');
        submit();
        expect(await screen.findByText('Name is required')).toBeInTheDocument();
        expect(gateway.createGroupAPIKey).not.toHaveBeenCalled();
    });

    it('user mode loads active users and requires one before Create is enabled', async () => {
        renderModal();
        typeName('personal');
        fireEvent.click(userRadio());
        await waitFor(() => expect(gateway.fetchEndUsers).toHaveBeenCalledWith({ active_only: true, limit: 100 }));
        await search();
        expect(screen.getByRole('button', { name: 'Create Key' })).toBeDisabled();
    });

    it('searches users, and sends the chosen id as a user-specific key', async () => {
        renderModal();
        typeName('personal');
        fireEvent.click(userRadio());
        const input = await search();

        fireEvent.focus(input);
        expect(screen.getByText('Alice Lund')).toBeInTheDocument();
        expect(screen.getByText('ID: 8')).toBeInTheDocument();

        fireEvent.change(input, { target: { value: 'zzz' } });
        expect(screen.getByText('No users found matching your search')).toBeInTheDocument();

        fireEvent.change(input, { target: { value: 'alice' } });
        fireEvent.click(screen.getByText('Alice Lund'));
        expect((input as HTMLInputElement).value).toBe('Alice Lund (alice@acme.test)');
        expect(screen.getByText('User ID: 7')).toBeInTheDocument();

        submit();
        await waitFor(() => expect(gateway.createGroupAPIKey).toHaveBeenCalled());
        expect(gateway.createGroupAPIKey.mock.calls[0][1]).toMatchObject({ access_mode: 'user', user_id: 7 });
    });

    it('clearing the search clears the selection', async () => {
        renderModal();
        fireEvent.click(userRadio());
        const input = await search();
        fireEvent.focus(input);
        fireEvent.click(screen.getByText('bob@acme.test'));
        expect(screen.getByText('User ID: 8')).toBeInTheDocument();
        fireEvent.change(input, { target: { value: '' } });
        expect(screen.queryByText('User ID: 8')).toBeNull();
        expect(screen.getByRole('button', { name: 'Create Key' })).toBeDisabled();
    });

    it('a user picked before switching back to shared is not sent', async () => {
        renderModal();
        typeName('k');
        fireEvent.click(userRadio());
        const input = await search();
        fireEvent.focus(input);
        fireEvent.click(screen.getByText('bob@acme.test'));

        fireEvent.click(screen.getByRole('radio', { name: /Shared Access/ }));
        submit();
        await waitFor(() => expect(gateway.createGroupAPIKey).toHaveBeenCalled());
        expect(gateway.createGroupAPIKey.mock.calls[0][1]).toMatchObject({ access_mode: 'shared', user_id: undefined });
    });

    it('a failed user load is reported and can be retried', async () => {
        gateway.fetchEndUsers.mockRejectedValueOnce(new Error('You do not have the permission end_users:read'));
        renderModal();
        fireEvent.click(userRadio());
        expect(await screen.findByText('Failed to load users: You do not have the permission end_users:read')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Refresh user list' }));
        await waitFor(() => expect(gateway.fetchEndUsers).toHaveBeenCalledTimes(2));
        fireEvent.focus(await search());
        expect(await screen.findByText('Alice Lund')).toBeInTheDocument();
    });

    it('clicking outside closes the user dropdown', async () => {
        renderModal();
        fireEvent.click(userRadio());
        fireEvent.focus(await search());
        expect(screen.getByText('Alice Lund')).toBeInTheDocument();
        fireEvent.mouseDown(document.body);
        expect(screen.queryByText('Alice Lund')).toBeNull();
    });

    it('a refused create shows why and keeps the modal open', async () => {
        gateway.createGroupAPIKey.mockRejectedValue(new Error('quota exceeded'));
        renderModal();
        typeName('k');
        submit();
        expect(await screen.findByText('quota exceeded')).toBeInTheDocument();
        expect(onKeyCreated).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
    });

    it('a failed agent list just offers no binding; a bare array response also works', async () => {
        gatewayClient.listAgentIdentities.mockRejectedValueOnce(new Error('x'));
        renderModal();
        await waitFor(() => expect(gatewayClient.listAgentIdentities).toHaveBeenCalled());
        expect(screen.getAllByRole('option')).toHaveLength(1);
        cleanup();

        gatewayClient.listAgentIdentities.mockResolvedValueOnce([{ id: 'ag-2', name: 'Bare' }]);
        renderModal();
        expect(await screen.findByRole('option', { name: 'Bare' })).toBeInTheDocument();
    });

    it('Cancel closes without creating', () => {
        renderModal();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(gateway.createGroupAPIKey).not.toHaveBeenCalled();
    });

    it('shows what an API key and workload identity prove, and what production needs, with the guide', () => {
        renderModal();
        const box = screen.getByTestId('trade-offs-keys');
        expect(box).toHaveTextContent(/API key/);
        expect(box).not.toHaveTextContent(/bound key/i);
        expect(box).toHaveTextContent(/production requires workload identity/i);
        expect(screen.getByRole('link', { name: /choosing how agents authenticate/i })).toHaveAttribute(
            'href',
            'https://docs.brutor.ai/build-an-agent/identity/choosing/',
        );
    });

    it('a create the identity policy refuses says what the policy requires', async () => {
        gateway.createGroupAPIKey.mockRejectedValue(
            new GatewayAPIError('refused', 422, undefined, {
                detail: {
                    message: 'refused',
                    code: 'key_refused_by_identity_policy',
                    effective_policy: { mode: 'enforce', min_assurance: 'workload', allow_user_keys: false, allowed_issuers: null, max_token_lifetime_s: 3600, max_key_ttl_days: null },
                    sources: { min_assurance: { kind: 'lifecycle', group_id: 'rg-1', group_name: 'eng', stage: 'active' } },
                },
            }),
        );
        renderModal();
        typeName('k');
        submit();
        expect(await screen.findByText(/requires credential level workload \(this group \(lifecycle: active\)\)/)).toBeInTheDocument();
        expect(onClose).not.toHaveBeenCalled();
    });

    it('a create refused for its expiry says the ceiling', async () => {
        gateway.createGroupAPIKey.mockRejectedValue(
            new GatewayAPIError('refused', 422, undefined, {
                detail: {
                    message: 'An expiry is required.',
                    code: 'key_expiry_required',
                    effective_policy: { mode: 'audit', min_assurance: 'key', allow_user_keys: true, allowed_issuers: null, max_token_lifetime_s: 3600, max_key_ttl_days: 30 },
                    sources: {},
                },
            }),
        );
        renderModal();
        typeName('k');
        submit();
        expect(await screen.findByText(/at most 30 days/)).toBeInTheDocument();
    });
});
