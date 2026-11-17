/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resource Group view of assigned Semantic Policies — READ-ONLY.
 *
 * Policy bindings are owned by the Semantic Policy itself (set on the
 * form's Access Control tab under Governance → Semantic Policies). This
 * view shows what's currently bound and is styled to match
 * GroupArgumentPoliciesTab exactly.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    AlertCircle,
    Loader,
    ScrollText,
    Search,
    ShieldOff,
    X,
} from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import type { SemanticPolicy, Surface } from '../../../lib/semanticPolicyTypes';
import { ACTION_LABEL, SURFACE_LABEL } from '../../../lib/semanticPolicyTypes';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
}

const GroupSemanticPoliciesTab: React.FC<Props> = ({ group }) => {
    const { gatewayClient } = useGateway();

    const [assigned, setAssigned] = useState<SemanticPolicy[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');

    const loadPolicies = useCallback(async () => {
        if (!gatewayClient) return;
        try {
            setLoading(true);
            setError('');
            const result = await gatewayClient.listSemanticPolicies();
            const all: SemanticPolicy[] = result?.policies || [];
            // A policy with NO group binding is tenant-wide and applies to
            // every group — that is what the runtime does. The core's query is
            //   AND (b.semantic_policy_id IS NULL OR b.resource_group_id = $2)
            // on a LEFT JOIN, so an unbound policy matches every group.
            // Filtering on `includes(group.id)` alone would drop exactly those,
            // and this pane would report "No Semantic Policies Apply" for a
            // group that IS being governed — under-reporting governance
            // coverage, which is the worse direction to be wrong in.
            // Matches `policyAppliesToGroup` in lib/argumentPolicyGates.ts.
            setAssigned(
                all.filter((p) => {
                    const ids = p.resource_group_ids ?? [];
                    return ids.length === 0 || ids.includes(group.id);
                })
            );
        } catch (err) {
            console.error('Failed to load semantic policies:', errorKind(err));
            setError(errorMessage(err) ?? 'Failed to load semantic policies');
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, group.id]);

    useEffect(() => {
        runBackground(loadPolicies(), 'policies');
    }, [loadPolicies]);

    const filtered = assigned.filter((p) => {
        if (!searchTerm) return true;
        const term = searchTerm.toLowerCase();
        return (
            p.name.toLowerCase().includes(term) ||
            (p.description || '').toLowerCase().includes(term) ||
            p.constraint_text.toLowerCase().includes(term)
        );
    });

    const totalCount = assigned.length;

    if (loading) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-16">
                    <Loader className="w-6 h-6 text-primary-500 animate-spin" />
                    <span className="ml-3 text-secondary">Loading semantic policies...</span>
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
                        <p className="font-medium">Failed to load semantic policies</p>
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
                        <ScrollText className="w-5 h-5 text-primary-600" />
                    </div>
                    <div>
                        <h2 className="text-lg font-semibold text-default">Semantic Policies</h2>
                        <p className="text-sm text-muted">
                            {totalCount} polic{totalCount !== 1 ? 'ies' : 'y'} apply to this group
                        </p>
                    </div>
                </div>
            </div>

            {/* Info Banner */}
            <InfoBanner title="How semantic policies apply to this group">
                <p>
                    Semantic policies are bound directly to this group, or applied tenant-wide when a
                    policy has no group binding. At runtime an <strong>LLM judge</strong> evaluates each
                    governed request against the policy's natural-language constraint and, in{' '}
                    <strong>enforce</strong> mode, blocks or warns on a violation (in{' '}
                    <strong>shadow</strong> mode it only logs a would-block). Bindings are managed from{' '}
                    <strong>Governance → Semantic Policies</strong> on each policy's Access Control tab.
                </p>
            </InfoBanner>

            {/* Filter row */}
            {totalCount > 3 && (
                <div className="flex items-center space-x-3">
                    <div className="relative flex-1 max-w-xs min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search..."
                            className="w-full pl-9 pr-8 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                        />
                        {searchTerm && (
                            <button onClick={() => setSearchTerm('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-secondary">
                                <X className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                </div>
            )}

            {/* Table */}
            {filtered.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <ShieldOff className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {totalCount === 0 ? 'No Semantic Policies Apply' : 'No Matches'}
                    </p>
                    <p className="text-sm text-muted mt-2">
                        {totalCount === 0
                            ? 'Bind a policy from Governance → Semantic Policies → Access Control'
                            : `No policies match "${searchTerm}"`}
                    </p>
                </div>
            ) : (
                <div className="bg-surface rounded-xl border border-default overflow-x-auto">
                    <table className="min-w-full divide-y divide-default">
                        <thead className="bg-surface-alt">
                            <tr>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Policy</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Constraint</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Surfaces</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Mode</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Action</th>
                            </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                            {filtered.map((p) => (
                                <tr key={p.id} className="hover:bg-surface-alt transition-colors">
                                    {/* Policy */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className="p-1.5 rounded-lg bg-primary-100 flex-shrink-0">
                                                <ScrollText className="w-4 h-4 text-primary-600" />
                                            </div>
                                            <div className="min-w-0">
                                                <span className="text-sm font-semibold text-default truncate">{p.name}</span>
                                                {p.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[240px]">{p.description}</p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* Constraint */}
                                    <td className="px-6 py-4">
                                        <p className="text-xs text-muted max-w-[300px] truncate" title={p.constraint_text}>
                                            {p.constraint_text}
                                        </p>
                                    </td>

                                    {/* Surfaces */}
                                    <td className="px-6 py-4">
                                        {p.surfaces?.length ? (
                                            <div className="flex flex-wrap gap-1">
                                                {p.surfaces.map((s) => (
                                                    <span key={s} className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary">
                                                        {SURFACE_LABEL[s as Surface] ?? s}
                                                    </span>
                                                ))}
                                            </div>
                                        ) : (
                                            <span className="text-xs text-muted italic">All surfaces</span>
                                        )}
                                    </td>

                                    {/* Mode */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full ${
                                            p.enabled
                                                ? p.mode === 'enforce'
                                                    ? 'bg-red-100 text-red-700'
                                                    : 'bg-amber-100 text-amber-700'
                                                : 'bg-surface-raised text-muted'
                                        }`}>
                                            {p.enabled ? p.mode : 'inactive'}
                                        </span>
                                    </td>

                                    {/* Action */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs text-secondary">{ACTION_LABEL[p.action] ?? p.action}</span>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
};

export default GroupSemanticPoliciesTab;
