/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GroupAgentsTab — Shows A2A agent cards assigned to this resource group.
 *
 * Uses the server-side effective-agent-cards endpoint to resolve
 * inheritance. Same shape as GroupKBTab / GroupAgentSkillsTab:
 *   * All / Direct / Inherited view toggle.
 *   * Inherited chip + source group name on each row.
 *   * Trust-tier badge pulled from the agent-card detail (unsigned /
 *     tenant_signed / partner_signed / revoked) so operators see which
 *     cards their group can invoke and at what trust level.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertCircle,
    ArrowUpRight,
    Bot,
    IdCard,
    Loader2,
} from 'lucide-react';

import { useAuth } from '../../../hooks/useAuth';
import { ResourceGroup } from '../../../lib/groupTypes';
import InfoBanner from '../../common/InfoBanner';
import { trustTierBadge } from '../../a2a/a2aFormatters';
import type { AgentCardTrustTier } from '../../../lib/a2aTypes';
import { resolveGroupAgents } from '../../../lib/agentGroupResolve';
import type { AgentIdentity } from '../../../lib/agentIdentityTypes';
import { agentIdentityStanding } from '../../../lib/agentIdentityStanding';
import { errorMessage } from '../../../lib/narrow';
import { runBackground } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

type ViewMode = 'all' | 'direct' | 'inherited';

interface AgentCardInfo {
    id: string;
    name: string;
    version: string;
    description?: string | null;
    trust_tier: string;
    is_trusted: boolean;
    enabled: boolean;
    capability_count: number;
    is_inherited: boolean;
    source_group_id?: string | null;
    source_group_name?: string | null;
    group_ids: string[];
}

interface Props {
    group: ResourceGroup;
}

const GroupAgentsTab: React.FC<Props> = ({ group }) => {
    const { gatewayClient } = useAuth();

    const [direct, setDirect] = useState<AgentCardInfo[]>([]);
    const [inherited, setInherited] = useState<AgentCardInfo[]>([]);
    const [effective, setEffective] = useState<AgentCardInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [viewMode, setViewMode] = useState<ViewMode>('all');

    // Agent identities (security principals) that are members of this group —
    // distinct from the Agent Cards (capability advertisements) above.
    const [identities, setIdentities] = useState<AgentIdentity[]>([]);
    const [identitiesLoading, setIdentitiesLoading] = useState(true);
    const [readFailure, reads] = useReadFailures<'identities'>();
    // The browser's clock when the identities were read: an expiry is judged against it.
    const [identitiesReadAt, setIdentitiesReadAt] = useState(() => Date.now());

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const resp = await gatewayClient.getEffectiveAgentCards(group.id);
            setDirect(resp.direct_cards ?? []);
            setInherited(resp.inherited_cards ?? []);
            setEffective(resp.effective_cards ?? []);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load agent cards'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, group.id]);

    const loadIdentities = useCallback(async () => {
        setIdentitiesLoading(true);
        try {
            setIdentities(await resolveGroupAgents(gatewayClient, group.id));
            setIdentitiesReadAt(Date.now());
            reads.loaded('identities');
        } catch (err) {
            setIdentities([]);
            reads.lost('identities', 'Failed to load the agent identities of this group', err);
        } finally {
            setIdentitiesLoading(false);
        }
    }, [gatewayClient, group.id, reads]);

    useEffect(() => {
        runBackground(load(), 'load');
        runBackground(loadIdentities(), 'identities');
    }, [load, loadIdentities]);

    const filtered =
        viewMode === 'direct' ? direct : viewMode === 'inherited' ? inherited : effective;

    if (loading) {
        return (
            <div className="flex items-center justify-center py-16">
                <Loader2 className="w-6 h-6 animate-spin text-muted" />
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center justify-center py-16 text-red-500">
                <AlertCircle className="w-5 h-5 mr-2" />
                <span className="text-sm">{error}</span>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-4">
            {/* Info box — matches the KB / Skills tabs in shape and tone */}
            <InfoBanner>
                <p>
                    <strong>Agent Cards</strong> are signed advertisements of agents that can be
                    called at their advertised endpoints — a registry of the <em>callees</em> this
                    group may delegate to. Assigned from{' '}
                    <strong>Resources &gt; Agent-to-Agent &gt; Agent Cards</strong>; cards from parent
                    groups are inherited when <em>inherit_resources</em> is enabled. Members of this
                    group can invoke only the capabilities of the cards listed below (any
                    capability-level <code>handler_type</code> still applies).
                </p>
            </InfoBanner>

            {/* View toggle */}
            {effective.length > 0 && (
                <div className="flex items-center space-x-1 text-sm">
                    <button
                        onClick={() => setViewMode('all')}
                        className={`px-3 py-1.5 rounded-md font-medium transition-colors ${
                            viewMode === 'all'
                                ? 'bg-primary-100 text-primary-700'
                                : 'text-muted hover:text-secondary hover:bg-surface-alt'
                        }`}
                    >
                        All ({effective.length})
                    </button>
                    <button
                        onClick={() => setViewMode('direct')}
                        className={`px-3 py-1.5 rounded-md font-medium transition-colors ${
                            viewMode === 'direct'
                                ? 'bg-primary-100 text-primary-700'
                                : 'text-muted hover:text-secondary hover:bg-surface-alt'
                        }`}
                    >
                        Direct ({direct.length})
                    </button>
                    <button
                        onClick={() => setViewMode('inherited')}
                        className={`px-3 py-1.5 rounded-md font-medium transition-colors ${
                            viewMode === 'inherited'
                                ? 'bg-primary-100 text-primary-700'
                                : 'text-muted hover:text-secondary hover:bg-surface-alt'
                        }`}
                    >
                        Inherited ({inherited.length})
                    </button>
                </div>
            )}

            {/* Inherited banner */}
            {viewMode === 'inherited' && inherited.length > 0 && (
                <div className="bg-primary-pale border border-primary rounded-lg p-3">
                    <p className="text-xs text-primary">
                        <ArrowUpRight className="w-3.5 h-3.5 inline mr-1" />
                        These cards are inherited from parent groups. Edit the
                        parent group’s assignment to remove or change them.
                    </p>
                </div>
            )}

            {filtered.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <IdCard className="w-12 h-12 text-disabled mx-auto mb-3" />
                    <h3 className="text-sm font-semibold text-default mb-1">
                        {viewMode === 'inherited'
                            ? 'No Inherited Agent Cards'
                            : viewMode === 'direct'
                              ? 'No Direct Agent Cards'
                              : 'No Agent Cards Assigned'}
                    </h3>
                    <p className="text-xs text-muted max-w-sm mx-auto">
                        Go to{' '}
                        <strong>Resources &gt; Agent-to-Agent &gt; Agent Cards</strong>{' '}
                        and bind a card to this group to let its members invoke it.
                    </p>
                </div>
            ) : (
                <div className="space-y-3">
                    {filtered.map(card => {
                        const tier = trustTierBadge(
                            card.trust_tier as AgentCardTrustTier,
                        );
                        return (
                            <div
                                key={card.id}
                                className={`border rounded-lg p-4 transition-colors ${
                                    card.is_inherited
                                        ? 'bg-surface-alt border-default'
                                        : 'bg-surface border-default hover:bg-surface-alt'
                                }`}
                            >
                                <div className="flex items-start justify-between">
                                    <div className="flex items-center gap-3">
                                        <div
                                            className={`p-2 rounded-lg ${
                                                card.enabled
                                                    ? 'bg-primary-100 text-primary-600'
                                                    : 'bg-surface-raised text-muted'
                                            }`}
                                        >
                                            <IdCard className="w-5 h-5" />
                                        </div>
                                        <div>
                                            <div className="flex items-center gap-2">
                                                <span className="font-medium text-default">
                                                    {card.name}
                                                </span>
                                                <span className="text-xs text-muted">
                                                    v{card.version}
                                                </span>
                                                <span
                                                    className={`px-2 py-0.5 text-xs rounded ${tier.bg} ${tier.text}`}
                                                    title={tier.title}
                                                >
                                                    {tier.label}
                                                </span>
                                                {card.is_inherited && (
                                                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium text-secondary bg-surface-alt rounded">
                                                        <ArrowUpRight className="w-3 h-3" />
                                                        Inherited
                                                        {card.source_group_name
                                                            ? ` from ${card.source_group_name}`
                                                            : ''}
                                                    </span>
                                                )}
                                                {!card.enabled && (
                                                    <span className="px-1.5 py-0.5 text-[10px] font-medium text-muted bg-surface-raised rounded">
                                                        Disabled
                                                    </span>
                                                )}
                                            </div>
                                            {card.description && (
                                                <p className="text-xs text-muted mt-0.5">
                                                    {card.description}
                                                </p>
                                            )}
                                        </div>
                                    </div>
                                </div>

                                <div className="flex items-center gap-4 mt-3 text-xs text-muted">
                                    <span>
                                        {card.capability_count} capabilit
                                        {card.capability_count === 1 ? 'y' : 'ies'}
                                    </span>
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}

            {/* ── Agent Identities (members of this group) ── */}
            <div className="pt-4 border-t border-default space-y-3">
                <div>
                    <h3 className="text-sm font-semibold text-default flex items-center gap-2">
                        <Bot className="w-4 h-4 text-primary-600" /> Agent Identities
                    </h3>
                    <p className="text-xs text-muted mt-1">
                        Agent <strong>identities</strong> are the security principals that <em>act</em> —
                        the <em>callers</em>. Each is an IdP identifier (e.g. a SPIFFE subject) plus
                        grants for what it may do. Distinct from the Agent <strong>Cards</strong> above,
                        which advertise <em>callable</em> agents: an agent that acts has an identity, an
                        agent others call has a card, and one that does both has both. Membership and
                        grants are managed under <strong>Resources &gt; Agents &gt; Identities</strong>{' '}
                        and <strong>Governance &gt; Agent Policies</strong>.
                    </p>
                </div>

                {identitiesLoading ? (
                    <div className="flex items-center justify-center py-6">
                        <Loader2 className="w-5 h-5 animate-spin text-muted" />
                    </div>
                ) : readFailure('identities') ? (
                    <ReadFailureNotice failure={readFailure('identities')} onRetry={() => runBackground(loadIdentities(), 'identities')} />
                ) : identities.length === 0 ? (
                    <div className="text-center py-8 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                        <Bot className="w-10 h-10 text-disabled mx-auto mb-2" />
                        <p className="text-xs text-muted max-w-sm mx-auto">
                            No agent identities are members of this group. Add one under
                            Resources &gt; Agents &gt; Identities.
                        </p>
                    </div>
                ) : (
                    <div className="space-y-2">
                        {identities.map(a => (
                            <div key={a.id} className="flex items-center justify-between border border-default rounded-lg px-4 py-2.5 bg-surface">
                                <div className="flex items-center gap-3 min-w-0">
                                    <div className={`p-1.5 rounded-lg ${agentIdentityStanding(a, identitiesReadAt).state === 'active' ? 'bg-primary-100 text-primary-600' : 'bg-surface-raised text-muted'}`}>
                                        <Bot className="w-4 h-4" />
                                    </div>
                                    <div className="min-w-0">
                                        <div className="font-medium text-default truncate">{a.name}</div>
                                        <div className="text-xs text-muted truncate">{a.description || a.source}</div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2 text-xs flex-shrink-0">
                                    <span className={a.default_deny ? 'text-red-600' : 'text-muted'}>
                                        {a.default_deny ? 'default-deny' : 'allow-through'}
                                    </span>
                                    <span className={`px-2 py-0.5 rounded font-semibold ${a.enforcement_mode === 'dry_run' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}>
                                        {a.enforcement_mode === 'dry_run' ? 'dry-run' : 'enforce'}
                                    </span>
                                    {(() => {
                                        // Not standing: revoked, or past its expiry (which nothing
                                        // records in the stored status).
                                        const standing = agentIdentityStanding(a, identitiesReadAt);
                                        return standing.state === 'active' ? null : (
                                            <span
                                                data-testid={`group-agent-standing-${a.id}`}
                                                title={standing.detail ?? undefined}
                                                className="px-1.5 py-0.5 text-[10px] font-medium text-muted bg-surface-raised rounded"
                                            >
                                                {standing.label}
                                            </span>
                                        );
                                    })()}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
};

export default GroupAgentsTab;
