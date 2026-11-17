/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Test for Resources → Agents → Identities (definition surface).
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import AgentIdentitiesResourceTab from '../components/agent/AgentIdentitiesResourceTab';

const _toast = { showToast: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../components/common/Toast', () => ({ useToast: () => _toast }));

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockRevoke = vi.fn();
const mockMembers = vi.fn();

const _client = {
    listAgentIdentities: mockList,
    createAgentIdentity: mockCreate,
    updateAgentIdentity: mockUpdate,
    revokeAgentIdentity: mockRevoke,
    listAgentMemberships: mockMembers,
    addAgentMembership: vi.fn(),
    removeAgentMembership: vi.fn(),
};
const _fetchTree = vi.fn().mockResolvedValue({ tree: [] });
vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _client, fetchGroupHierarchyTree: _fetchTree }),
}));

function agent(over = {}) {
    return {
        id: 'agent-1', tenant_id: 't', name: 'billing-bot', source: 'manual',
        enabled: true, status: 'active', default_deny: true, enforcement_mode: 'enforce', ...over,
    };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ agents: [agent()], total: 1 });
    mockMembers.mockResolvedValue({ memberships: [], total: 0 });
    mockCreate.mockResolvedValue(agent({ id: 'agent-2', name: 'new-bot' }));
    mockRevoke.mockResolvedValue(agent({ enabled: false, status: 'suspended' }));
});

describe('AgentIdentitiesResourceTab', () => {
    it('lists agent identities with default-deny posture', async () => {
        render(<AgentIdentitiesResourceTab />);
        await waitFor(() => expect(screen.getByText('billing-bot')).toBeInTheDocument());
        expect(mockList).toHaveBeenCalled();
        expect(screen.getAllByText('deny').length).toBeGreaterThan(0);
    });

    it('creates a new agent identity', async () => {
        render(<AgentIdentitiesResourceTab />);
        await waitFor(() => expect(screen.getByText('billing-bot')).toBeInTheDocument());
        fireEvent.click(screen.getByText('New Agent'));
        const input = await screen.findByPlaceholderText('billing-bot');
        fireEvent.change(input, { target: { value: 'invoice-agent' } });
        fireEvent.click(screen.getByText('Create'));
        await waitFor(() => expect(mockCreate).toHaveBeenCalledWith(
            expect.objectContaining({ name: 'invoice-agent', default_deny: true }),
        ));
    });

    it('opens the merged Edit dialog: identity fields + memberships (not grants)', async () => {
        render(<AgentIdentitiesResourceTab />);
        await waitFor(() => expect(screen.getByText('billing-bot')).toBeInTheDocument());
        // Single "Edit" action (Manage + Edit merged) opens the form dialog.
        fireEvent.click(screen.getByText('Edit'));
        await waitFor(() => expect(mockMembers).toHaveBeenCalledWith('agent-1'));
        expect(screen.getByText(/Resource-group memberships/)).toBeInTheDocument();
        // Grants are NOT managed here — they live under Agent Policies.
        expect(screen.getAllByText(/Governance → Agent Policies/).length).toBeGreaterThan(0);
    });

    describe('an identity past its expiry', () => {
        // The gateway refuses it at request time; its stored status stays
        // "active". The expiry is read on the browser's clock.
        const NOW = Date.parse('2026-11-13T12:00:00Z');
        const EXPIRED = '2026-11-12T08:30:00+00:00';

        beforeEach(() => {
            vi.spyOn(Date, 'now').mockReturnValue(NOW);
        });
        afterEach(() => {
            vi.restoreAllMocks();
        });

        it('is listed as expired, not as active, with the server\'s value', async () => {
            mockList.mockResolvedValue({
                agents: [agent({ expires_at: EXPIRED }), agent({ id: 'agent-2', name: 'fresh-bot', expires_at: '2027-01-01T00:00:00+00:00' })],
                total: 2,
            });
            render(<AgentIdentitiesResourceTab />);
            const expired = await screen.findByTestId('agent-status-agent-1');
            expect(expired).toHaveTextContent('expired');
            expect(expired).not.toHaveTextContent('active');
            expect(expired).toHaveAttribute(
                'title',
                'This identity expired (expires_at 2026-11-12T08:30:00+00:00). The gateway refuses every request made with it. Its stored status is still "active": nothing changes it when the expiry passes.',
            );
            expect(screen.getByTestId('agent-status-agent-2')).toHaveTextContent('active');
        });

        it('says so in the identity\'s dialog', async () => {
            mockList.mockResolvedValue({ agents: [agent({ expires_at: EXPIRED })], total: 1 });
            render(<AgentIdentitiesResourceTab />);
            await screen.findByText('billing-bot');
            fireEvent.click(screen.getByText('Edit'));
            expect(await screen.findByTestId('agent-expired-note')).toHaveTextContent(
                'This identity expired (expires_at 2026-11-12T08:30:00+00:00). The gateway refuses every request made with it. Its stored status is still "active": nothing changes it when the expiry passes.',
            );
        });

        it('an identity that still stands shows its expiry in the dialog, and no note', async () => {
            mockList.mockResolvedValue({ agents: [agent({ expires_at: '2027-01-01T00:00:00+00:00' })], total: 1 });
            render(<AgentIdentitiesResourceTab />);
            await screen.findByText('billing-bot');
            fireEvent.click(screen.getByText('Edit'));
            expect(await screen.findByTestId('agent-expiry')).toHaveTextContent('Expires 2027-01-01T00:00:00+00:00');
            expect(screen.queryByTestId('agent-expired-note')).toBeNull();
        });
    });

    describe('the expiry of an identity, in its dialog', () => {
        const NOW = Date.parse('2026-11-13T12:00:00Z');
        /** What the dialog sends for a local date and time typed into the field. */
        const sent = (local: string): string => new Date(local).toISOString();

        beforeEach(() => {
            vi.spyOn(Date, 'now').mockReturnValue(NOW);
            mockUpdate.mockResolvedValue(agent());
        });
        afterEach(() => {
            vi.restoreAllMocks();
        });

        const openEdit = async (over = {}) => {
            mockList.mockResolvedValue({ agents: [agent(over)], total: 1 });
            render(<AgentIdentitiesResourceTab />);
            await screen.findByText('billing-bot');
            fireEvent.click(screen.getByText('Edit'));
            await waitFor(() => expect(mockMembers).toHaveBeenCalledWith('agent-1'));
            return screen.getByLabelText<HTMLInputElement>('Expires');
        };

        it('states the rule in plain words', async () => {
            await openEdit();
            expect(screen.getByTestId('agent-expiry-rule')).toHaveTextContent(
                'Leave empty for an identity that does not expire. The gateway refuses an identity that is past its expiry at the next request. To renew an expired identity, set a later time or clear the field.',
            );
        });

        it('a new identity is created without an expiry unless one is set', async () => {
            render(<AgentIdentitiesResourceTab />);
            await screen.findByText('billing-bot');
            fireEvent.click(screen.getByText('New Agent'));
            fireEvent.change(await screen.findByPlaceholderText('billing-bot'), { target: { value: 'invoice-agent' } });
            expect(screen.getByLabelText('Expires')).toHaveValue('');
            fireEvent.click(screen.getByText('Create'));
            await waitFor(() => expect(mockCreate).toHaveBeenCalled());
            expect(mockCreate.mock.calls[0][0]).not.toHaveProperty('expires_at');
        });

        it('a new identity is created with the expiry that is set, as an instant with its zone', async () => {
            render(<AgentIdentitiesResourceTab />);
            await screen.findByText('billing-bot');
            fireEvent.click(screen.getByText('New Agent'));
            fireEvent.change(await screen.findByPlaceholderText('billing-bot'), { target: { value: 'invoice-agent' } });
            fireEvent.change(screen.getByLabelText('Expires'), { target: { value: '2027-03-01T09:00' } });
            fireEvent.click(screen.getByText('Create'));
            await waitFor(() => expect(mockCreate).toHaveBeenCalledWith(expect.objectContaining({ expires_at: sent('2027-03-01T09:00') })));
            expect(sent('2027-03-01T09:00')).toMatch(/Z$/);
        });

        it('opens on the stored expiry, in the reader\'s own time, and does not send it back when it is not touched', async () => {
            const field = await openEdit({ expires_at: '2027-01-01T00:00:00+00:00' });
            expect(sent(field.value)).toBe('2027-01-01T00:00:00.000Z');
            fireEvent.click(screen.getByText('Save Changes'));
            await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
            expect(mockUpdate.mock.calls[0][1]).not.toHaveProperty('expires_at');
        });

        it('a stored expiry without a zone is the server\'s, which is UTC', async () => {
            const field = await openEdit({ expires_at: '2027-01-01T00:00:00' });
            expect(sent(field.value)).toBe('2027-01-01T00:00:00.000Z');
        });

        it('renews an expired identity: a later time is sent', async () => {
            const field = await openEdit({ expires_at: '2026-11-12T08:30:00+00:00' });
            expect(screen.getByTestId('agent-expired-note')).toBeInTheDocument();
            fireEvent.change(field, { target: { value: '2027-06-30T18:00' } });
            expect(screen.queryByTestId('agent-expiry-passed')).toBeNull();
            fireEvent.click(screen.getByText('Save Changes'));
            await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('agent-1', expect.objectContaining({ expires_at: sent('2027-06-30T18:00') })));
        });

        it('clears the expiry: null is sent, which the control plane stores as no expiry', async () => {
            const field = await openEdit({ expires_at: '2026-11-12T08:30:00+00:00' });
            fireEvent.click(screen.getByRole('button', { name: 'No expiry' }));
            expect(field).toHaveValue('');
            fireEvent.click(screen.getByText('Save Changes'));
            await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('agent-1', expect.objectContaining({ expires_at: null })));
        });

        it('has nothing to clear on an identity without an expiry, and sends none', async () => {
            await openEdit();
            expect(screen.queryByRole('button', { name: 'No expiry' })).toBeNull();
            fireEvent.click(screen.getByText('Save Changes'));
            await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
            expect(mockUpdate.mock.calls[0][1]).not.toHaveProperty('expires_at');
        });

        it('says what a time that has passed means, and still saves it', async () => {
            const field = await openEdit();
            fireEvent.change(field, { target: { value: '2026-01-01T00:00' } });
            expect(screen.getByTestId('agent-expiry-passed')).toHaveTextContent(
                'This time has passed. Saved like this, the gateway refuses this identity from its next request.',
            );
            fireEvent.click(screen.getByText('Save Changes'));
            await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('agent-1', expect.objectContaining({ expires_at: sent('2026-01-01T00:00') })));
        });
    });

    it('revokes an agent', async () => {
        render(<AgentIdentitiesResourceTab />);
        await waitFor(() => expect(screen.getByText('billing-bot')).toBeInTheDocument());
        fireEvent.click(screen.getByText('Revoke'));
        await waitFor(() => expect(mockRevoke).toHaveBeenCalledWith('agent-1'));
    });
});
