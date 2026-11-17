/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resource Group view of Agent Policies — READ-ONLY.
 *
 * Lists the per-action grants of agent identities that are MEMBERS of this
 * group (membership owned under Resources → Agents → Identities; grants owned
 * under Governance → Agent Policies). Styled to match the sibling per-group
 * policy tabs.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertCircle, ArrowRight, Bot, Loader, ShieldOff,
} from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import { resolveGroupAgentGrants } from '../../../lib/agentGroupResolve';
import type { AgentGrant, AgentIdentity } from '../../../lib/agentIdentityTypes';
import { runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
}

const effectClass = (e: string) =>
    e === 'deny' ? 'text-red-600' : e === 'approval_required' ? 'text-yellow-600' : 'text-green-700';

const GroupAgentPoliciesTab: React.FC<Props> = ({ group }) => {
    const { gatewayClient } = useGateway();

    const [rows, setRows] = useState<{ agent: AgentIdentity; grants: AgentGrant[] }[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const load = useCallback(async () => {
        if (!gatewayClient) return;
        try {
            setLoading(true);
            setError('');
            const res = await resolveGroupAgentGrants(gatewayClient, group.id);
            setRows(res);
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to load agent policies');
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, group.id]);

    useEffect(() => { runBackground(load(), 'load'); }, [load]);

    const totalAgents = rows.length;
    const totalGrants = rows.reduce((n, r) => n + r.grants.length, 0);

    if (loading) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-16">
                    <Loader className="w-6 h-6 text-primary-500 animate-spin" />
                    <span className="ml-3 text-secondary">Loading agent policies...</span>
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div className="p-6">
                <div className="flex items-center space-x-3 text-red-600 bg-surface-alt rounded-lg p-4">
                    <AlertCircle className="w-5 h-5 flex-shrink-0" />
                    <div>
                        <p className="font-medium">Failed to load agent policies</p>
                        <p className="text-sm mt-1">{error}</p>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-5">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="p-2 rounded-lg bg-primary-100">
                        <Bot className="w-5 h-5 text-primary-600" />
                    </div>
                    <div>
                        <h2 className="text-lg font-semibold text-default">Agent Policies</h2>
                        <p className="text-sm text-muted">
                            {totalAgents} member agent{totalAgents !== 1 ? 's' : ''} · {totalGrants} grant{totalGrants !== 1 ? 's' : ''}
                        </p>
                    </div>
                </div>
            </div>

            {/* Info Banner */}
            <InfoBanner title="How agent policies apply to this group">
                <p>
                    These are the per-action grants of agent identities that are <strong>members of this
                    group</strong>. At runtime an agent must satisfy both its own grants ("actions, not
                    access", default-deny) <em>and</em> this group's ACL — strictest-wins. Membership is
                    managed under <strong>Resources → Agents → Identities</strong>; grants under{' '}
                    <strong>Governance → Agent Policies</strong>.
                </p>
            </InfoBanner>

            {/* Body */}
            {rows.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <ShieldOff className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">No Member Agents</p>
                    <p className="text-sm text-muted mt-2">
                        Add an agent to this group under Resources → Agents → Identities to see its policy here.
                    </p>
                </div>
            ) : (
                <div className="space-y-4">
                    {rows.map(({ agent, grants }) => (
                        <div key={agent.id} className="bg-surface rounded-xl border border-default overflow-hidden">
                            <div className="flex items-center justify-between px-5 py-3 bg-surface-alt border-b border-default">
                                <div className="flex items-center gap-2">
                                    <Bot className="w-4 h-4 text-primary-600" />
                                    <span className="font-medium text-default">{agent.name}</span>
                                    <span className="text-xs text-muted">
                                        {agent.default_deny ? 'default-deny' : 'allow-through'}
                                    </span>
                                </div>
                                <span className={`text-[11px] px-2 py-0.5 rounded font-semibold ${agent.enforcement_mode === 'dry_run' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}>
                                    {agent.enforcement_mode === 'dry_run' ? 'dry-run' : 'enforce'}
                                </span>
                            </div>
                            {grants.length === 0 ? (
                                <p className="px-5 py-3 text-xs text-muted">
                                    No grants — with default-deny this agent can do nothing.
                                </p>
                            ) : (
                                <div className="divide-y divide-default">
                                    {grants.map((g) => (
                                        <div key={g.id} className="flex items-center justify-between px-5 py-2 text-sm">
                                            <span className="text-secondary">
                                                <code className="text-xs">{g.action_type}</code>{' '}
                                                <ArrowRight className="w-3 h-3 inline text-muted" />{' '}
                                                <code className="text-xs">{g.target}</code>
                                            </span>
                                            <span className={`text-xs font-medium ${effectClass(g.effect)}`}>{g.effect}</span>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};

export default GroupAgentPoliciesTab;
