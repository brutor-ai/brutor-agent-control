/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resolve which agent identities are members of a given resource group, and
 * (optionally) their grants. There is no server-side group→agents endpoint, so
 * we list identities and check each one's memberships client-side. Used by the
 * per-group Agents tab and the per-group Agent Policies view (read-only). Agent
 * counts per group are small, so the N+1 is acceptable for a detail view.
 */

import type { AgentGrant, AgentIdentity } from './agentIdentityTypes';

interface AgentClient {
    listAgentIdentities: () => Promise<{ agents?: AgentIdentity[] }>;
    listAgentMemberships: (agentId: string) => Promise<{ memberships?: { group_id: string }[] }>;
    listAgentGrants: (agentId: string) => Promise<{ grants?: AgentGrant[] }>;
}

/** Agent identities that are members of `groupId`. A membership lookup that fails fails the read: an agent left out would read as not a member. */
export async function resolveGroupAgents(
    client: AgentClient,
    groupId: string,
): Promise<AgentIdentity[]> {
    const r = await client.listAgentIdentities();
    const agents = r.agents || [];
    const checked = await Promise.all(
        agents.map(async (a) => {
            const m = await client.listAgentMemberships(a.id);
            return (m.memberships || []).some((x) => x.group_id === groupId) ? a : null;
        }),
    );
    return checked.filter((a: AgentIdentity | null): a is AgentIdentity => a !== null);
}

/** Member agents of `groupId` paired with their grants (read-only view). */
export async function resolveGroupAgentGrants(
    client: AgentClient,
    groupId: string,
): Promise<{ agent: AgentIdentity; grants: AgentGrant[] }[]> {
    const agents = await resolveGroupAgents(client, groupId);
    return Promise.all(
        agents.map(async (agent) => {
            const g = await client.listAgentGrants(agent.id);
            return { agent, grants: g.grants || [] };
        }),
    );
}
