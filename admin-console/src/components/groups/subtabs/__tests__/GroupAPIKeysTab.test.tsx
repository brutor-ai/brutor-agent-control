/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Group API keys.
 *
 * Pinned: enable/disable sends the opposite of the key's current state, revoke
 * is confirmed first and offered only while the key is live, a state badge shows
 * the most final state, and the one-time secret of a new key is shown and
 * copyable whichever field the server used for it.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const fetchIdentityPolicy = vi.fn();
const granted = new Set<string>();
const gateway = {
    gatewayClient: { fetchIdentityPolicy },
    fetchGroupAPIKeys: vi.fn(),
    fetchGroupAPIKeyUsage: vi.fn(),
    revokeAPIKey: vi.fn(),
    enableAPIKey: vi.fn(),
    disableAPIKey: vi.fn(),
};
const confirm = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../hooks/useAuth', () => ({ useAuth: () => ({ hasPermission: (permission: string) => granted.has(permission) }) }));
vi.mock('../../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));
vi.mock('../CreateGroupKeyModal', () => ({
    default: (p: { onKeyCreated: (k: unknown) => void; onClose: () => void; group: { id: string } }) => (
        <div data-testid="create-modal">
            {p.group.id}
            <button
                onClick={() => {
                    p.onKeyCreated(createdKey.value);
                    p.onClose();
                }}
            >
                mint
            </button>
        </div>
    ),
}));

import GroupAPIKeysTab from '../GroupAPIKeysTab';

const createdKey: { value: unknown } = { value: null };
const GROUP = { id: 'rg-1', name: 'eng' } as ResourceGroup;

const key = (id: string, extra: object = {}) => ({
    id,
    name: id,
    key_prefix: `pre-${id}`,
    enabled: true,
    revoked: false,
    is_expired: false,
    usage_count: 1,
    scope: 'shared',
    ...extra,
});
const LIVE = key('live', { description: 'CI', last_used_at: '2026-10-01T00:00:00Z', usage_count: 5 });
const OFF = key('off', { enabled: false, scope: 'user', user_email: 'alice@acme.test', expires_at: '2027-01-01T00:00:00Z' });
const DEAD = key('dead', { revoked: true });
const OLD = key('old', { is_expired: true, expires_at: '2020-01-01T00:00:00Z' });

async function open() {
    render(<GroupAPIKeysTab group={GROUP} />);
    await screen.findByText('live');
}
const rowOf = (name: string) => within(screen.getByText(name).closest('tr') as HTMLElement);

describe('GroupAPIKeysTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        for (const fn of [gateway.fetchGroupAPIKeys, gateway.fetchGroupAPIKeyUsage, gateway.revokeAPIKey, gateway.enableAPIKey, gateway.disableAPIKey, fetchIdentityPolicy]) fn.mockReset();
        granted.clear();
        gateway.fetchGroupAPIKeys.mockResolvedValue({ keys: [LIVE, OFF, DEAD, OLD] });
        gateway.fetchGroupAPIKeyUsage.mockResolvedValue({ active_keys: 2, total_usage: 1_500_000 });
        gateway.enableAPIKey.mockResolvedValue({});
        gateway.disableAPIKey.mockResolvedValue({});
        gateway.revokeAPIKey.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('shows state, type and usage for each key', async () => {
        await open();
        expect(gateway.fetchGroupAPIKeys).toHaveBeenCalledWith('rg-1');
        expect(gateway.fetchGroupAPIKeyUsage).toHaveBeenCalledWith('rg-1');

        expect(rowOf('live').getByText('Enabled')).toBeInTheDocument();
        expect(rowOf('live').getByText('Shared')).toBeInTheDocument();
        expect(rowOf('live').getByText('5 requests')).toBeInTheDocument();
        expect(rowOf('off').getByText('Disabled')).toBeInTheDocument();
        expect(rowOf('off').getByText('User-Specific')).toBeInTheDocument();
        expect(rowOf('off').getByText('alice@acme.test')).toBeInTheDocument();
        expect(rowOf('off').getByText('1 request')).toBeInTheDocument();
        // Revoked outranks enabled, and leaves no way to act on the key.
        expect(rowOf('dead').getByText('Revoked')).toBeInTheDocument();
        expect(rowOf('dead').queryByTitle('Revoke key')).toBeNull();
        expect(rowOf('dead').queryByTitle('Disable key')).toBeNull();
        expect(rowOf('old').getAllByText('Expired').length).toBeGreaterThan(0);

        expect(screen.getByText('4 keys')).toBeInTheDocument();
        expect(screen.getByText('1.5M')).toBeInTheDocument();
    });

    it('searches by name or prefix, and can clear', async () => {
        await open();
        const search = screen.getByPlaceholderText('Search by key name or prefix...');
        fireEvent.change(search, { target: { value: 'PRE-OFF' } });
        expect(screen.getByText('off')).toBeInTheDocument();
        expect(screen.queryByText('live')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
        expect(screen.getByText('live')).toBeInTheDocument();

        fireEvent.change(search, { target: { value: 'nothing' } });
        expect(screen.getByText('No keys found matching your search')).toBeInTheDocument();
    });

    it('disables an enabled key and enables a disabled one, then reloads', async () => {
        await open();
        fireEvent.click(rowOf('live').getByTitle('Disable key'));
        await waitFor(() => expect(gateway.disableAPIKey).toHaveBeenCalledWith('live'));
        expect(gateway.enableAPIKey).not.toHaveBeenCalled();
        await waitFor(() => expect(gateway.fetchGroupAPIKeys).toHaveBeenCalledTimes(2));

        fireEvent.click((await screen.findAllByTitle('Enable key'))[0]);
        await waitFor(() => expect(gateway.enableAPIKey).toHaveBeenCalledWith('off'));
    });

    it('a failed toggle is shown', async () => {
        gateway.disableAPIKey.mockRejectedValue(new Error('locked'));
        await open();
        fireEvent.click(rowOf('live').getByTitle('Disable key'));
        expect(await screen.findByText('locked')).toBeInTheDocument();
    });

    it('revokes only after confirmation', async () => {
        confirm.mockResolvedValue(false);
        await open();
        fireEvent.click(rowOf('live').getByTitle('Revoke key'));
        await waitFor(() => expect(confirm).toHaveBeenCalled());
        expect(gateway.revokeAPIKey).not.toHaveBeenCalled();

        confirm.mockResolvedValue(true);
        fireEvent.click(rowOf('live').getByTitle('Revoke key'));
        await waitFor(() => expect(gateway.revokeAPIKey).toHaveBeenCalledWith('live'));
        await waitFor(() => expect(gateway.fetchGroupAPIKeyUsage).toHaveBeenCalledTimes(2));
    });

    it('a failed revoke is shown', async () => {
        confirm.mockResolvedValue(true);
        gateway.revokeAPIKey.mockRejectedValue({});
        await open();
        fireEvent.click(rowOf('live').getByTitle('Revoke key'));
        expect(await screen.findByText('Failed to revoke key')).toBeInTheDocument();
    });

    it('a failed load is shown; a failed usage load does not hide the keys', async () => {
        gateway.fetchGroupAPIKeyUsage.mockRejectedValue(new Error('x'));
        await open();
        expect(screen.queryByText('Total Requests:')).toBeNull();
        cleanup();

        gateway.fetchGroupAPIKeys.mockRejectedValue(new Error('cp down'));
        render(<GroupAPIKeysTab group={GROUP} />);
        expect(await screen.findByText('cp down')).toBeInTheDocument();
    });

    it('the empty state explains what a key is for', async () => {
        gateway.fetchGroupAPIKeys.mockResolvedValue({ keys: [] });
        render(<GroupAPIKeysTab group={GROUP} />);
        expect(await screen.findByText('No API keys yet')).toBeInTheDocument();
    });

    it('copies a prefix and flags it as copied', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
        await open();
        fireEvent.click(rowOf('live').getByTitle('Copy prefix'));
        expect(writeText).toHaveBeenCalledWith('pre-live');
        vi.unstubAllGlobals();
    });

    it.each([
        ['key_value', { key_value: 'sk-a' }, 'sk-a'],
        ['full_key', { full_key: 'sk-b' }, 'sk-b'],
        ['key', { key: 'sk-c' }, 'sk-c'],
    ])('shows the one-time secret from %s and can copy it', async (_field, secret, expected) => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
        createdKey.value = { name: 'new-key', key_prefix: 'pre', access_mode: 'user', user_email: 'bob@acme.test', ...secret };
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));
        expect(screen.getByTestId('create-modal')).toHaveTextContent('rg-1');
        fireEvent.click(screen.getByText('mint'));

        expect(screen.getByText('API Key Created Successfully')).toBeInTheDocument();
        expect(screen.getByText(expected)).toBeInTheDocument();
        expect(screen.getByText('User-Specific', { selector: 'p' })).toBeInTheDocument();
        expect(screen.getByText('bob@acme.test')).toBeInTheDocument();
        // The list refreshes behind the dialog.
        await waitFor(() => expect(gateway.fetchGroupAPIKeys).toHaveBeenCalledTimes(2));

        fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
        expect(writeText).toHaveBeenCalledWith(expected);
        expect(await screen.findByText('Copied!')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Done' }));
        expect(screen.queryByText('API Key Created Successfully')).toBeNull();
        vi.unstubAllGlobals();
    });

    it('opens and closes the explainer, and Refresh reloads', async () => {
        await open();
        fireEvent.click(screen.getByTitle('About API Keys'));
        expect(screen.getByText('About Group API Keys')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Got it' }));
        expect(screen.queryByText('About Group API Keys')).toBeNull();

        fireEvent.click(screen.getByTitle('Refresh'));
        await waitFor(() => expect(gateway.fetchGroupAPIKeys).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(gateway.fetchGroupAPIKeyUsage).toHaveBeenCalledTimes(2));
    });

    describe('the credential level of each key under the identity policy', () => {
        const development = { mode: 'audit', min_assurance: 'key', allow_user_keys: true, allowed_issuers: null, max_token_lifetime_s: 3600, max_key_ttl_days: null };
        const production = { ...development, mode: 'enforce', min_assurance: 'workload', allow_user_keys: false };
        const policy = (overrides: object) => ({
            group_id: 'rg-1', group_name: 'eng', is_ai_system: true, lifecycle_stage: 'approved', eu_ai_act_risk_tier: null,
            attributed_to_ai_system: true, policy: null, effective: development, inherited_floor: development,
            sources: Object.fromEntries(['mode', 'min_assurance', 'allow_user_keys', 'allowed_issuers', 'max_token_lifetime_s', 'max_key_ttl_days'].map((f) => [f, { kind: 'default' }])),
            max_user_key_ttl_days: 90,
            ...overrides,
        });

        it('badges every key as level key, and reads no policy without the permission', async () => {
            await open();
            expect(rowOf('live').getByTestId('key-level-live')).toHaveTextContent('key');
            expect(rowOf('dead').getByTestId('key-level-dead')).toHaveTextContent('key');
            expect(fetchIdentityPolicy).not.toHaveBeenCalled();
            expect(screen.queryByTestId('key-warning-live')).not.toBeInTheDocument();
        });

        it('warns that promotion will refuse a usable key of a development AI System', async () => {
            granted.add('identity_policy:read');
            fetchIdentityPolicy.mockResolvedValue(policy({}));
            await open();
            await waitFor(() => expect(fetchIdentityPolicy).toHaveBeenCalledWith('rg-1'));
            expect(await screen.findByTestId('key-warning-live')).toHaveTextContent(/promotion will refuse this key/i);
            // A revoked or expired key is not usable: nothing to warn about.
            expect(screen.queryByTestId('key-warning-dead')).not.toBeInTheDocument();
            expect(screen.queryByTestId('key-warning-old')).not.toBeInTheDocument();
        });

        it('says the key is refused now when the policy enforces a higher level', async () => {
            granted.add('identity_policy:read');
            fetchIdentityPolicy.mockResolvedValue(policy({ lifecycle_stage: 'active', effective: production }));
            await open();
            expect(await screen.findByTestId('key-warning-live')).toHaveTextContent(/refused now.*workload/i);
        });

        it('says when the identity policy cannot be read, and still badges the level', async () => {
            granted.add('identity_policy:read');
            fetchIdentityPolicy.mockRejectedValue(new Error('forbidden'));
            await open();
            expect(await screen.findByTestId('keys-identity-failure')).toHaveTextContent(/identity policy of this group.*forbidden/);
            expect(rowOf('live').getByTestId('key-level-live')).toHaveTextContent('key');
        });

        it('warns of nothing where no AI System governs the group', async () => {
            granted.add('identity_policy:read');
            fetchIdentityPolicy.mockResolvedValue(policy({ is_ai_system: false, attributed_to_ai_system: false }));
            await open();
            await waitFor(() => expect(fetchIdentityPolicy).toHaveBeenCalled());
            expect(screen.queryByTestId('key-warning-live')).not.toBeInTheDocument();
        });
    });
});
