/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Test for Governance → Agent Policies (grants surface).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import AgentPoliciesTab from '../components/governance/AgentPoliciesTab';

const _toast = { showToast: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../components/common/Toast', () => ({ useToast: () => _toast }));

const mockList = vi.fn();
const mockGrants = vi.fn();
const mockAddGrant = vi.fn();
const mockDelGrant = vi.fn();
const mockUpdate = vi.fn();

const _client = {
    listAgentIdentities: mockList,
    listAgentGrants: mockGrants,
    addAgentGrant: mockAddGrant,
    deleteAgentGrant: mockDelGrant,
    updateAgentIdentity: mockUpdate,
};
vi.mock('../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: _client }) }));

function agent(over = {}) {
    return {
        id: 'agent-1', tenant_id: 't', name: 'billing-bot', source: 'manual',
        enabled: true, status: 'active', default_deny: true, enforcement_mode: 'enforce', ...over,
    };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({ agents: [agent()], total: 1 });
    mockGrants.mockResolvedValue({ grants: [], total: 0 });
    mockAddGrant.mockResolvedValue({ id: 'g-1' });
});

describe('AgentPoliciesTab', () => {
    it('lists agents, auto-selects the first, and loads its grants', async () => {
        render(<AgentPoliciesTab />);
        await waitFor(() => expect(screen.getByText('How agent policies work')).toBeInTheDocument());
        await waitFor(() => expect(screen.getAllByText('billing-bot').length).toBeGreaterThan(0));
        // First agent auto-selected → grants fetched for it.
        await waitFor(() => expect(mockGrants).toHaveBeenCalledWith('agent-1'));
    });

    it('adds a grant to the selected agent', async () => {
        render(<AgentPoliciesTab />);
        await waitFor(() => expect(mockGrants).toHaveBeenCalledWith('agent-1'));
        fireEvent.click(screen.getByText('Add grant'));
        await waitFor(() => expect(mockAddGrant).toHaveBeenCalledWith(
            'agent-1', expect.objectContaining({ action_type: 'llm_call', effect: 'allow' }),
        ));
    });

    it('shows the empty-grants default-deny hint', async () => {
        render(<AgentPoliciesTab />);
        await waitFor(() => expect(mockGrants).toHaveBeenCalledWith('agent-1'));
        await waitFor(() =>
            expect(screen.getByText(/this agent can do nothing/i)).toBeInTheDocument(),
        );
    });
});
