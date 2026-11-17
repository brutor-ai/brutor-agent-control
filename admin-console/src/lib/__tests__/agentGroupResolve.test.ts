/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * There is no server-side group→agents endpoint, so membership is resolved
 * client-side, one call per agent. The rule that matters: a failing
 * membership or grant lookup must degrade to "not a member" / "no grants"
 * for that agent alone — never take the whole tab down, and never fabricate
 * a membership it could not confirm.
 */

import { describe, it, expect, vi } from 'vitest';
import { resolveGroupAgentGrants, resolveGroupAgents } from '../agentGroupResolve';
import type { AgentGrant, AgentIdentity } from '../agentIdentityTypes';

const agent = (id: string): AgentIdentity => ({ id, name: id }) as AgentIdentity;
const grant = (id: string): AgentGrant => ({ id }) as AgentGrant;

const client = (opts: {
    agents?: AgentIdentity[];
    memberships?: Record<string, { group_id: string }[] | Error>;
    grants?: Record<string, AgentGrant[] | Error>;
}) => ({
    listAgentIdentities: vi.fn().mockResolvedValue({ agents: opts.agents }),
    listAgentMemberships: vi.fn((id: string) => {
        const m = opts.memberships?.[id];
        if (m instanceof Error) return Promise.reject(m);
        return Promise.resolve({ memberships: m });
    }),
    listAgentGrants: vi.fn((id: string) => {
        const g = opts.grants?.[id];
        if (g instanceof Error) return Promise.reject(g);
        return Promise.resolve({ grants: g });
    }),
});

describe('resolveGroupAgents', () => {
    it('keeps only the agents whose memberships name the group, in list order', async () => {
        const c = client({
            agents: [agent('a1'), agent('a2'), agent('a3')],
            memberships: {
                a1: [{ group_id: 'g1' }],
                a2: [{ group_id: 'g2' }],
                a3: [{ group_id: 'g9' }, { group_id: 'g1' }],
            },
        });
        expect((await resolveGroupAgents(c, 'g1')).map((a) => a.id)).toEqual(['a1', 'a3']);
    });

    it('a membership lookup that fails fails the whole read: an agent left out would read as "not in the group"', async () => {
        const c = client({
            agents: [agent('a1'), agent('a2')],
            memberships: { a1: new Error('403'), a2: [{ group_id: 'g1' }] },
        });
        await expect(resolveGroupAgents(c, 'g1')).rejects.toThrow('403');
    });

    it('returns nothing when no agent belongs to the group', async () => {
        const c = client({ agents: [agent('a1')], memberships: { a1: [] } });
        expect(await resolveGroupAgents(c, 'g1')).toEqual([]);
    });

    it('handles an empty or absent agent list without calling for memberships', async () => {
        const empty = client({ agents: [] });
        expect(await resolveGroupAgents(empty, 'g1')).toEqual([]);
        expect(empty.listAgentMemberships).not.toHaveBeenCalled();

        const absent = client({});
        expect(await resolveGroupAgents(absent, 'g1')).toEqual([]);
    });

    it('treats an absent memberships field as no memberships', async () => {
        const c = client({ agents: [agent('a1')] });
        expect(await resolveGroupAgents(c, 'g1')).toEqual([]);
    });

    it('lets a failure listing identities surface — the tab has nothing to show', async () => {
        const c = {
            ...client({}),
            listAgentIdentities: vi.fn().mockRejectedValue(new Error('500')),
        };
        await expect(resolveGroupAgents(c, 'g1')).rejects.toThrow('500');
    });

    it('asks about every agent exactly once', async () => {
        const c = client({
            agents: [agent('a1'), agent('a2')],
            memberships: { a1: [{ group_id: 'g1' }], a2: [{ group_id: 'g1' }] },
        });
        await resolveGroupAgents(c, 'g1');
        expect(c.listAgentMemberships).toHaveBeenCalledTimes(2);
    });
});

describe('resolveGroupAgentGrants', () => {
    it('pairs each member agent with its grants', async () => {
        const c = client({
            agents: [agent('a1'), agent('a2')],
            memberships: { a1: [{ group_id: 'g1' }], a2: [{ group_id: 'g2' }] },
            grants: { a1: [grant('x')], a2: [grant('y')] },
        });
        const out = await resolveGroupAgentGrants(c, 'g1');
        expect(out).toHaveLength(1);
        expect(out[0].agent.id).toBe('a1');
        expect(out[0].grants.map((g) => g.id)).toEqual(['x']);
    });

    it('a grant lookup that fails fails the whole read: an empty grant list would read as "this agent may do nothing"', async () => {
        const c = client({
            agents: [agent('a1')],
            memberships: { a1: [{ group_id: 'g1' }] },
            grants: { a1: new Error('403') },
        });
        await expect(resolveGroupAgentGrants(c, 'g1')).rejects.toThrow('403');
    });

    it('normalises an absent grants field to an empty array', async () => {
        const c = client({ agents: [agent('a1')], memberships: { a1: [{ group_id: 'g1' }] } });
        expect((await resolveGroupAgentGrants(c, 'g1'))[0].grants).toEqual([]);
    });

    it('does not ask for grants for non-members', async () => {
        const c = client({
            agents: [agent('a1'), agent('a2')],
            memberships: { a1: [{ group_id: 'g1' }], a2: [] },
        });
        await resolveGroupAgentGrants(c, 'g1');
        expect(c.listAgentGrants).toHaveBeenCalledTimes(1);
        expect(c.listAgentGrants).toHaveBeenCalledWith('a1');
    });

    it('is empty when the group has no member agents', async () => {
        const c = client({ agents: [agent('a1')], memberships: { a1: [] } });
        expect(await resolveGroupAgentGrants(c, 'g1')).toEqual([]);
        expect(c.listAgentGrants).not.toHaveBeenCalled();
    });
});
