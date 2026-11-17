/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The group's Agents tab: Agent Cards (callees) and Agent Identities (callers).
 * Read-only. Pinned: the Direct/Inherited/All split, trust tier and provenance on
 * each card, identities resolved by membership in this group (not all agents),
 * and each identity's enforcement stance as stated by the server.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const gatewayClient = {
    getEffectiveAgentCards: vi.fn(),
    listAgentIdentities: vi.fn(),
    listAgentMemberships: vi.fn(),
};
const auth = { gatewayClient };
vi.mock('../../../../hooks/useAuth', () => ({ useAuth: () => auth }));

import GroupAgentsTab from '../GroupAgentsTab';

const GROUP = { id: 'rg-1', name: 'eng' } as ResourceGroup;

const card = (id: string, extra: object = {}) => ({
    id,
    name: id,
    version: '1.2.0',
    trust_tier: 'tenant_signed',
    is_trusted: true,
    enabled: true,
    capability_count: 2,
    is_inherited: false,
    group_ids: [],
    ...extra,
});
const OWN = card('own-card', { description: 'Does things', capability_count: 1 });
const HEIR = card('org-card', { is_inherited: true, source_group_name: 'Org', enabled: false });

const agent = (id: string, extra: object = {}) => ({
    id,
    name: id,
    enabled: true,
    status: 'active',
    default_deny: true,
    enforcement_mode: 'enforce',
    ...extra,
});

async function open() {
    render(<GroupAgentsTab group={GROUP} />);
    await screen.findByRole('heading', { name: /Agent Identities|Identities/ });
    expect(gatewayClient.getEffectiveAgentCards).toHaveBeenCalledWith('rg-1');
}

describe('GroupAgentsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        for (const fn of Object.values(gatewayClient)) fn.mockReset();
        gatewayClient.getEffectiveAgentCards.mockResolvedValue({
            direct_cards: [OWN],
            inherited_cards: [HEIR],
            effective_cards: [OWN, HEIR],
        });
        gatewayClient.listAgentIdentities.mockResolvedValue({ agents: [] });
    });

    afterEach(cleanup);

    it('shows each card with version, trust tier and capability count', async () => {
        await open();
        expect(screen.getByText('own-card')).toBeInTheDocument();
        expect(screen.getAllByText('v1.2.0')).toHaveLength(2);
        expect(screen.getAllByText('Tenant signed')).toHaveLength(2);
        expect(screen.getByText('Does things')).toBeInTheDocument();
        expect(screen.getByText(/1 capability/)).toBeInTheDocument();
        expect(screen.getByText(/2 capabilities/)).toBeInTheDocument();
    });

    it('splits by source, naming the parent, without leaking a raw escape into the text', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Direct (1)' }));
        expect(screen.queryByText('org-card')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Inherited (1)' }));
        expect(screen.queryByText('own-card')).toBeNull();
        expect(screen.getByText(/Inherited from Org/)).toBeInTheDocument();
        expect(screen.getByText('Disabled')).toBeInTheDocument();
        expect(screen.getByText(/parent group’s assignment/)).toBeInTheDocument();
        expect(document.body.textContent).not.toContain('\\u2019');
    });

    it.each([
        ['Direct', 'No Direct Agent Cards'],
        ['Inherited', 'No Inherited Agent Cards'],
    ])('the empty %s view says so', async (label, title) => {
        gatewayClient.getEffectiveAgentCards.mockResolvedValue({ effective_cards: [OWN] });
        await open();
        fireEvent.click(screen.getByRole('button', { name: `${label} (0)` }));
        expect(screen.getByText(title)).toBeInTheDocument();
    });

    it('says so when nothing is assigned, with no toggle', async () => {
        gatewayClient.getEffectiveAgentCards.mockResolvedValue({});
        await open();
        expect(screen.getByText('No Agent Cards Assigned')).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /^All/ })).toBeNull();
    });

    it('a failed load names what failed', async () => {
        gatewayClient.getEffectiveAgentCards.mockRejectedValue({});
        render(<GroupAgentsTab group={GROUP} />);
        expect(await screen.findByText('Failed to load agent cards')).toBeInTheDocument();
    });

    it('marks a member identity past its expiry as expired: the gateway refuses it although its status is still active', async () => {
        const now = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-13T12:00:00Z'));
        gatewayClient.listAgentIdentities.mockResolvedValue({
            agents: [agent('lapsed', { expires_at: '2026-11-12T08:30:00+00:00' }), agent('standing', { expires_at: '2027-01-01T00:00:00+00:00' })],
        });
        gatewayClient.listAgentMemberships.mockResolvedValue({ memberships: [{ group_id: 'rg-1' }] });
        await open();
        const lapsed = await screen.findByTestId('group-agent-standing-lapsed');
        expect(lapsed).toHaveTextContent('expired');
        expect(lapsed).toHaveAttribute('title', expect.stringContaining('expires_at 2026-11-12T08:30:00+00:00'));
        expect(screen.queryByTestId('group-agent-standing-standing')).toBeNull();
        now.mockRestore();
    });

    it('lists only the identities that are members of this group, with their stance', async () => {
        gatewayClient.listAgentIdentities.mockResolvedValue({
            agents: [
                agent('member-a'),
                agent('member-b', { default_deny: false, enforcement_mode: 'dry_run', status: 'quarantined' }),
                agent('elsewhere'),
            ],
        });
        gatewayClient.listAgentMemberships.mockImplementation((id: string) => {
            return Promise.resolve({ memberships: id.startsWith('member') ? [{ group_id: 'rg-1' }] : [{ group_id: 'rg-9' }] });
        });
        await open();
        expect(await screen.findByText('member-a')).toBeInTheDocument();
        expect(screen.getByText('member-b')).toBeInTheDocument();
        expect(screen.queryByText('elsewhere')).toBeNull();
        expect(screen.getByText('default-deny')).toBeInTheDocument();
        expect(screen.getByText('allow-through')).toBeInTheDocument();
        expect(screen.getByText('enforce')).toBeInTheDocument();
        expect(screen.getByText('dry-run')).toBeInTheDocument();
    });

    it('says so when no identity is a member', async () => {
        await open();
        expect(await screen.findByText(/No agent identities are members of this group/)).toBeInTheDocument();
    });

    it('a listing that fails is not "no identity is a member": the section says the identities could not be loaded', async () => {
        gatewayClient.listAgentIdentities.mockRejectedValue(new Error('x'));
        await open();
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the agent identities of this group: x');
        expect(screen.queryByText(/No agent identities are members of this group/)).toBeNull();
    });

    it('one membership lookup that fails fails the section: an identity left out would read as not a member', async () => {
        gatewayClient.listAgentIdentities.mockResolvedValue({ agents: [agent('member-a'), agent('broken')] });
        gatewayClient.listAgentMemberships.mockImplementation((id: string) =>
            id === 'broken' ? Promise.reject(new Error('403')) : Promise.resolve({ memberships: [{ group_id: 'rg-1' }] }),
        );
        await open();
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the agent identities of this group: 403');
        expect(screen.queryByText('member-a')).toBeNull();
    });
});
