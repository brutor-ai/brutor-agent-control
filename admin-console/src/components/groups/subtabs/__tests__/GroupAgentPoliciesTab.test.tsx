/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The read-only view of what the agents in this group are allowed to do.
 *
 * Agent authorization is "actions, not access" and default-deny, so the
 * fact this pane must never blur is the difference between an agent with NO
 * grants (which, under default-deny, can do nothing at all) and one whose
 * grants simply have not loaded. An empty grant list therefore says so in
 * words rather than rendering as an empty box.
 *
 * The per-agent header carries the two facts that change how a grant list
 * should be read: default-deny vs allow-through, and dry-run vs enforce. A
 * deny in dry-run is a note; a deny in enforce stops traffic.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const mockResolveGroupAgentGrants = vi.fn();

// One stable client identity: the tab's loader is a useCallback keyed on it,
// so a fresh object per render would re-fetch forever.
vi.mock('../../../../hooks/useGateway', () => {
    const gatewayClient = { some: 'client' };
    return { useGateway: () => ({ gatewayClient }) };
});

vi.mock('../../../../lib/agentGroupResolve', () => ({
    resolveGroupAgentGrants: (...args: unknown[]): unknown => mockResolveGroupAgentGrants(...args),
}));

import GroupAgentPoliciesTab from '../GroupAgentPoliciesTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-1', name: 'revenue-copilot' } as ResourceGroup;

const agent = (overrides: Record<string, unknown> = {}) => ({
    id: 'a-1',
    name: 'revenue-sentinel',
    default_deny: true,
    enforcement_mode: 'enforce',
    ...overrides,
});

const grant = (overrides: Record<string, unknown> = {}) => ({
    id: 'g-1',
    action_type: 'mcp.tool.call',
    target: 'salesforce.query',
    effect: 'allow',
    ...overrides,
});

beforeEach(() => {
    vi.clearAllMocks();
    mockResolveGroupAgentGrants.mockResolvedValue([]);
});

const renderTab = () => render(<GroupAgentPoliciesTab group={GROUP} />);

describe('GroupAgentPoliciesTab — loading', () => {
    it('says it is loading before it claims anything about the agents', () => {
        mockResolveGroupAgentGrants.mockReturnValue(new Promise(() => { /* never settles */ }));
        renderTab();
        expect(screen.getByText(/Loading agent policies/i)).toBeInTheDocument();
        expect(screen.queryByText('No Member Agents')).toBeNull();
    });

    it('resolves the grants for this group', async () => {
        renderTab();
        await waitFor(() =>
            expect(mockResolveGroupAgentGrants).toHaveBeenCalledWith({ some: 'client' }, 'rg-1'),
        );
    });

    it('a failed load names the reason and does NOT read as "no agents"', async () => {
        mockResolveGroupAgentGrants.mockRejectedValue(new Error('identity service down'));
        renderTab();
        expect(await screen.findByText('Failed to load agent policies')).toBeInTheDocument();
        expect(screen.getByText('identity service down')).toBeInTheDocument();
        expect(screen.queryByText('No Member Agents')).toBeNull();
    });
});

describe('GroupAgentPoliciesTab — the empty estate', () => {
    it('explains where membership is managed instead of showing a blank pane', async () => {
        renderTab();
        expect(await screen.findByText('No Member Agents')).toBeInTheDocument();
        expect(
            screen.getByText(/Add an agent to this group under Resources → Agents → Identities/),
        ).toBeInTheDocument();
    });

    it('counts nothing, in the plural', async () => {
        renderTab();
        expect(await screen.findByText('0 member agents · 0 grants')).toBeInTheDocument();
    });
});

describe('GroupAgentPoliciesTab — reading the grants', () => {
    it('counts a single agent and a single grant in the singular', async () => {
        mockResolveGroupAgentGrants.mockResolvedValue([{ agent: agent(), grants: [grant()] }]);
        renderTab();
        expect(await screen.findByText('1 member agent · 1 grant')).toBeInTheDocument();
    });

    it('totals grants across every member agent', async () => {
        mockResolveGroupAgentGrants.mockResolvedValue([
            { agent: agent(), grants: [grant(), grant({ id: 'g-2' })] },
            { agent: agent({ id: 'a-2', name: 'smart-memory' }), grants: [grant({ id: 'g-3' })] },
        ]);
        renderTab();
        expect(await screen.findByText('2 member agents · 3 grants')).toBeInTheDocument();
    });

    it('shows each grant as action → target with its effect', async () => {
        mockResolveGroupAgentGrants.mockResolvedValue([
            {
                agent: agent(),
                grants: [
                    grant(),
                    grant({ id: 'g-2', action_type: 'llm.call', target: 'gpt-5', effect: 'deny' }),
                    grant({ id: 'g-3', action_type: 'mcp.tool.call', target: 'refund', effect: 'approval_required' }),
                ],
            },
        ]);
        renderTab();
        expect(await screen.findByText('salesforce.query')).toBeInTheDocument();
        expect(screen.getByText('allow')).toBeInTheDocument();
        expect(screen.getByText('deny')).toBeInTheDocument();
        expect(screen.getByText('approval_required')).toBeInTheDocument();
    });

    it('spells out that an agent with no grants can do nothing', async () => {
        mockResolveGroupAgentGrants.mockResolvedValue([{ agent: agent(), grants: [] }]);
        renderTab();
        expect(
            await screen.findByText(/No grants — with default-deny this agent can do nothing/),
        ).toBeInTheDocument();
    });

    it('marks a dry-run agent apart from an enforcing one', async () => {
        mockResolveGroupAgentGrants.mockResolvedValue([
            { agent: agent(), grants: [] },
            { agent: agent({ id: 'a-2', name: 'shadow', enforcement_mode: 'dry_run', default_deny: false }), grants: [] },
        ]);
        renderTab();
        expect(await screen.findByText('enforce')).toBeInTheDocument();
        expect(screen.getByText('dry-run')).toBeInTheDocument();
        expect(screen.getByText('default-deny')).toBeInTheDocument();
        expect(screen.getByText('allow-through')).toBeInTheDocument();
    });
});
