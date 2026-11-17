/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A resource group's A2A policy, and the allow-lists of the groups above.
 *
 * The gateway judges every level that sets a policy on its own: an outbound
 * address must match the allow-list of each level that states one. The
 * Control Plane answers the list of each level, root first, with the policy
 * (`remote_allow_lists`) and the sentence that says how they apply
 * (`remote_allow_lists_rule`). The tab shows the list of each group above by
 * that group, never one list merged from them, and asks for nothing else.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';

const getGroupA2AGovernance = vi.fn();
const fetchResourceGroup = vi.fn();

vi.mock('../../../../hooks/useAuth', () => {
    const gatewayClient = {
        getGroupA2AGovernance: (...a: unknown[]): unknown => getGroupA2AGovernance(...a),
        fetchResourceGroup: (...a: unknown[]): unknown => fetchResourceGroup(...a),
        updateResourceGroup: vi.fn(),
    };
    return { useAuth: () => ({ gatewayClient }) };
});
vi.mock('../../../common/Toast', () => ({ useToast: () => ({ success: vi.fn(), error: vi.fn() }) }));

import GroupA2AGovernanceTab from '../GroupA2AGovernanceTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-team', name: 'Team' } as ResourceGroup;
const RULE = 'An address must match every level: an outbound A2A call is allowed only when its address matches the allow-list of each level that states one (the agent card\'s own list included). The lists are not merged.';
const ANSWER = {
    group_id: 'rg-team',
    group_name: 'Team',
    a2a_global_governance: { remote: { allowed_globs: ['https://a.partner.example/*'] } },
    effective_a2a_governance: null,
    inherited_a2a_governance: null,
    // Every level that states a list, root first, this group's own last.
    remote_allow_lists: [
        { group_id: 'rg-root', group_name: 'Tenant', allowed_globs: ['https://*.partner.example/*'] },
        { group_id: 'rg-pay', group_name: 'Payments', allowed_globs: ['https://a.partner.example/*', 'https://b.partner.example/*'] },
        { group_id: 'rg-team', group_name: 'Team', allowed_globs: ['https://a.partner.example/*'] },
    ],
    remote_allow_lists_rule: RULE,
};

beforeEach(() => {
    vi.clearAllMocks();
    getGroupA2AGovernance.mockResolvedValue(ANSWER);
});
afterEach(cleanup);

describe('GroupA2AGovernanceTab', () => {
    it('says how the levels are judged', async () => {
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        expect(await screen.findByTestId('a2a-governance-banner')).toHaveTextContent(
            'The gateway judges every A2A call by this policy, by the policy of every group above, and by the policy of the agent card the call addresses. Each is judged on its own and the strictest verdict wins, so a group can tighten what is set above it and never relax it. An outbound address must match the allow-list of every level that states one.',
        );
    });

    it('shows the allow-list of each group above that states one, by group, from the top down, from the one answer', async () => {
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        const above = await screen.findByTestId('a2a-allow-lists-above');
        // The group's own list is the one being edited: not repeated among those above.
        expect(Array.from(above.querySelectorAll('li')).map((li) => li.textContent)).toEqual([
            'Tenant: https://*.partner.example/*',
            'Payments: https://a.partner.example/*, https://b.partner.example/*',
        ]);
        // One request for the policy; none per group above, none for the group's ancestors.
        expect(getGroupA2AGovernance).toHaveBeenCalledTimes(1);
        expect(getGroupA2AGovernance).toHaveBeenCalledWith('rg-team');
        expect(fetchResourceGroup).not.toHaveBeenCalled();
    });

    it('says how the lists apply in the Control Plane\'s words', async () => {
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        expect(await screen.findByTestId('a2a-allow-lists-rule')).toHaveTextContent(RULE);
    });

    it('shows no list of groups above when only the group itself states one', async () => {
        getGroupA2AGovernance.mockResolvedValue({ ...ANSWER, remote_allow_lists: [ANSWER.remote_allow_lists[2]] });
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        await waitFor(() => expect(screen.getByText('https://a.partner.example/*')).toBeInTheDocument());
        expect(screen.queryByTestId('a2a-allow-lists-above')).toBeNull();
        expect(screen.getByTestId('a2a-allow-lists-rule')).toHaveTextContent(RULE);
    });

    it('leaves out a level whose row names no group or no pattern', async () => {
        getGroupA2AGovernance.mockResolvedValue({
            ...ANSWER,
            remote_allow_lists: [{ group_id: 'rg-root', group_name: 'Tenant', allowed_globs: [] }, { group_name: 'Nameless' }, ANSWER.remote_allow_lists[1]],
        });
        render(<GroupA2AGovernanceTab group={GROUP} onUpdate={vi.fn()} />);
        const above = await screen.findByTestId('a2a-allow-lists-above');
        expect(Array.from(above.querySelectorAll('li')).map((li) => li.textContent)).toEqual(['Payments: https://a.partner.example/*, https://b.partner.example/*']);
    });
});
