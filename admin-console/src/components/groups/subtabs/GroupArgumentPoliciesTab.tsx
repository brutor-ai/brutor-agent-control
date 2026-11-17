/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resource Group view of assigned Argument Policies — READ-ONLY.
 *
 * Policy bindings are owned by the Argument Policy itself (set on
 * the form's Access Control tab under Governance → Argument
 * Policies). This view shows what's currently bound and is styled
 * to match GroupGuardrailsTab exactly.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    AlertCircle,
    Code2,
    Loader,
    Search,
    ShieldCheck,
    ShieldOff,
    X,
} from 'lucide-react';
import { ResourceGroup } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import type { ArgumentPolicy } from '../../../lib/argumentPolicyTypes';
import {
    ANALYZER_LABEL,
    SURFACE_LABEL,
} from '../../../lib/argumentPolicyTypes';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { runBackground } from '../../../lib/asyncSafe';


interface Props {
    group: ResourceGroup;
}

const GroupArgumentPoliciesTab: React.FC<Props> = ({ group }) => {
    const { listArgumentPolicies } = useGateway();

    const [assigned, setAssigned] = useState<ArgumentPolicy[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');

    const loadPolicies = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const result = await listArgumentPolicies();
            setAssigned(result.policies.filter(p => p.group_ids.includes(group.id)));
        } catch (err) {
            console.error('Failed to load argument policies:', errorKind(err));
            setError(errorMessage(err) ?? 'Failed to load argument policies');
        } finally {
            setLoading(false);
        }
    }, [listArgumentPolicies, group.id]);

    useEffect(() => {
        runBackground(loadPolicies(), 'policies');
    }, [loadPolicies]);

    // ── Filtering ───────────────────────────────────────────────────────────
    const filtered = assigned.filter(p => {
        if (!searchTerm) return true;
        const term = searchTerm.toLowerCase();
        return (
            p.name.toLowerCase().includes(term) ||
            (p.description || '').toLowerCase().includes(term) ||
            p.target.toLowerCase().includes(term) ||
            p.argument_key.toLowerCase().includes(term)
        );
    });

    const totalCount = assigned.length;

    // ── Loading state ───────────────────────────────────────────────────────
    if (loading) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-16">
                    <Loader className="w-6 h-6 text-primary-500 animate-spin" />
                    <span className="ml-3 text-secondary">Loading argument policies...</span>
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
                        <p className="font-medium">Failed to load argument policies</p>
                        <p className="text-sm mt-1">{error}</p>
                    </div>
                </div>
            </div>
        );
    }

    // ── Render ──────────────────────────────────────────────────────────────
    return (
        <div className="p-6 space-y-5">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="p-2 rounded-lg bg-primary-100">
                        <ShieldCheck className="w-5 h-5 text-primary-600" />
                    </div>
                    <div>
                        <h2 className="text-lg font-semibold text-default">Argument Policies</h2>
                        <p className="text-sm text-muted">
                            {totalCount} polic{totalCount !== 1 ? 'ies' : 'y'} apply to this group
                        </p>
                    </div>
                </div>
            </div>

            {/* Info Banner */}
            <InfoBanner title="How argument policies apply to this group">
                <p>
                    Argument policies are bound directly to this group, or inherited additively
                    from ancestor groups. They run AST-aware checks that parse the structured
                    argument (SQL, URL, shell, path, JSON) and refuse it on a rule match. Bindings are
                    managed from <strong>Governance → Argument Policies</strong> on each policy's
                    Access Control tab.
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
                            onChange={e => setSearchTerm(e.target.value)}
                            placeholder="Search..."
                            className="w-full pl-9 pr-8 py-1.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                        />
                        {searchTerm && (
                            <button
                                onClick={() => setSearchTerm('')}
                                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-secondary"
                            >
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
                        {totalCount === 0 ? 'No Argument Policies Apply' : 'No Matches'}
                    </p>
                    <p className="text-sm text-muted mt-2">
                        {totalCount === 0
                            ? 'Bind a policy from Governance → Argument Policies → Access Control'
                            : `No policies match "${searchTerm}"`
                        }
                    </p>
                </div>
            ) : (
                <div className="bg-surface rounded-xl border border-default overflow-x-auto">
                    <table className="min-w-full divide-y divide-default">
                        <thead className="bg-surface-alt">
                            <tr>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Policy
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Surface
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Target
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Argument
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Analyzer
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Rules
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Behaviour
                                </th>
                            </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                            {filtered.map(policy => (
                                <tr key={policy.id} className="hover:bg-surface-alt transition-colors">
                                    {/* Policy */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className="p-1.5 rounded-lg bg-primary-100 flex-shrink-0">
                                                <ShieldCheck className="w-4 h-4 text-primary-600" />
                                            </div>
                                            <div className="min-w-0">
                                                <span className="text-sm font-semibold text-default truncate">
                                                    {policy.name}
                                                </span>
                                                {policy.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[240px]">
                                                        {policy.description}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* Surface */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary">
                                            {SURFACE_LABEL[policy.surface]}
                                        </span>
                                    </td>

                                    {/* Target */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs font-mono text-secondary">{policy.target}</span>
                                    </td>

                                    {/* Argument */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs font-mono text-secondary">{policy.argument_key}</span>
                                    </td>

                                    {/* Analyzer */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-[11px] font-medium rounded-full bg-primary-100 text-primary-700">
                                            <Code2 className="w-3 h-3" />
                                            <span>{ANALYZER_LABEL[policy.analyzer]}</span>
                                        </span>
                                    </td>

                                    {/* Rules count */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-sm text-secondary">{policy.rules.length}</span>
                                    </td>

                                    {/* Behaviour (severity / state) */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full ${
                                            policy.enabled
                                                ? policy.severity === 'deny'
                                                    ? 'bg-green-100 text-green-700'
                                                    : 'bg-amber-100 text-amber-700'
                                                : 'bg-surface-raised text-muted'
                                        }`}>
                                            {policy.enabled ? policy.severity : 'inactive'}
                                        </span>
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

export default GroupArgumentPoliciesTab;
