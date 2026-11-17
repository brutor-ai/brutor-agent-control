/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    ShieldCheck,
    ShieldOff,
    Loader,
    AlertCircle,
    Search,
    X,
    ArrowUpRight,
    Star,
} from 'lucide-react';
import { ResourceGroup, GuardrailConfigInfo, EffectiveGuardrailsResponse } from '../../../lib/groupTypes';
import { surfaceGroupLabels } from '../../../lib/guardrailTypes';
import { useGateway } from '../../../hooks/useGateway';
import InfoBanner from '../../common/InfoBanner';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { runBackground } from '../../../lib/asyncSafe';

// ============================================================================
// Types
// ============================================================================

interface Props {
    group: ResourceGroup;
}

type ViewMode = 'all' | 'direct' | 'inherited';

// ============================================================================
// Helpers
// ============================================================================

function getCheckLabels(config: GuardrailConfigInfo): string[] {
    // Append "(+N surface)" when a check has per-surface
    // overrides so the row signals that the scalar action isn't the
    // whole story. The map comes from the effective-guardrails
    // endpoint (per_surface_counts); absent / 0 = scalar applies
    // everywhere.
    const ps = (key: string): string => {
        const n = config.per_surface_counts?.[key] ?? 0;
        return n > 0 ? ` (+${n} surface${n !== 1 ? 's' : ''})` : '';
    };
    const checks: string[] = [];
    if (config.pii_enabled)              checks.push(`PII${ps('pii')}`);
    if (config.prompt_injection_enabled) checks.push(`Injection${ps('prompt_injection')}`);
    if (config.jailbreak_enabled)        checks.push(`Jailbreak${ps('jailbreak')}`);
    if (config.toxic_content_enabled)    checks.push(`Toxic${ps('toxic_content')}`);
    if (config.secrets_enabled)          checks.push(`Secrets${ps('secrets')}`);

    // Banned words / patterns: surface override-only configs too —
    // a per-surface list with an empty global list is still active.
    const bw = config.banned_word_count;
    const bwOverrides = config.per_surface_counts?.banned_words ?? 0;
    if (bw > 0 || bwOverrides > 0) {
        const base = bw > 0 ? `${bw} word${bw !== 1 ? 's' : ''}` : 'words';
        checks.push(`${base}${bwOverrides > 0 ? ` (+${bwOverrides} surface${bwOverrides !== 1 ? 's' : ''})` : ''}`);
    }
    const bp = config.banned_pattern_count;
    const bpOverrides = config.per_surface_counts?.banned_patterns ?? 0;
    if (bp > 0 || bpOverrides > 0) {
        const base = bp > 0 ? `${bp} pattern${bp !== 1 ? 's' : ''}` : 'patterns';
        checks.push(`${base}${bpOverrides > 0 ? ` (+${bpOverrides} surface${bpOverrides !== 1 ? 's' : ''})` : ''}`);
    }
    if (config.provider_count > 0)       checks.push(`${config.provider_count} provider${config.provider_count !== 1 ? 's' : ''}`);
    return checks;
}

// ============================================================================
// Component
// ============================================================================

const GroupGuardrailsTab: React.FC<Props> = ({ group }) => {
    const { fetchEffectiveGuardrails } = useGateway();

    const [data, setData] = useState<EffectiveGuardrailsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [viewMode, setViewMode] = useState<ViewMode>('all');

    // ── Load from endpoint ──────────────────────────────────────────────────
    const loadGuardrails = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchEffectiveGuardrails(group.id);
            setData(response);
        } catch (err) {
            console.error('Failed to load guardrail configs:', errorKind(err));
            setError(errorMessage(err, 'Failed to load guardrail configurations'));
        } finally {
            setLoading(false);
        }
    }, [group.id, fetchEffectiveGuardrails]);

    useEffect(() => {
        runBackground(loadGuardrails(), 'guardrails');
    }, [loadGuardrails]);

    // ── Filtering ───────────────────────────────────────────────────────────
    const allConfigs = data?.effective_configs || [];

    const filtered = allConfigs.filter(config => {
        if (viewMode === 'direct' && config.is_inherited) return false;
        if (viewMode === 'inherited' && !config.is_inherited) return false;
        if (!searchTerm) return true;
        const term = searchTerm.toLowerCase();
        return (
            config.name.toLowerCase().includes(term) ||
            (config.description || '').toLowerCase().includes(term)
        );
    });

    const directCount = data?.direct_count || 0;
    const inheritedCount = data?.inherited_count || 0;
    const totalCount = data?.effective_count || 0;

    // ── Loading state ───────────────────────────────────────────────────────
    if (loading) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-16">
                    <Loader className="w-6 h-6 text-primary-500 animate-spin" />
                    <span className="ml-3 text-secondary">Loading guardrail configurations...</span>
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
                        <p className="font-medium">Failed to load guardrails</p>
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
                        <h2 className="text-lg font-semibold text-default">Guardrails</h2>
                        <p className="text-sm text-muted">
                            {totalCount} configuration{totalCount !== 1 ? 's' : ''} apply to this group
                        </p>
                    </div>
                </div>
            </div>

            {/* Info Banner */}
            <InfoBanner title="How guardrails apply to this group">
                <p>
                    Guardrails are <strong>directly</strong> assigned to this group, or <strong>inherited</strong> from
                    ancestor groups in the hierarchy. When multiple configs match, the most restrictive action wins
                    (Block &gt; Redact &gt; Warn &gt; Allow). Guardrails are managed from the Guardrails section.
                </p>
            </InfoBanner>

            {/* Filter row */}
            <div className="flex items-center space-x-3">
                {/* View mode pills */}
                <div className="flex items-center bg-surface-raised rounded-lg p-0.5">
                    {([
                        { id: 'all', label: `All (${totalCount})` },
                        { id: 'direct', label: `Direct (${directCount})` },
                        { id: 'inherited', label: `Inherited (${inheritedCount})` },
                    ] as const).map(pill => (
                        <button
                            key={pill.id}
                            onClick={() => setViewMode(pill.id)}
                            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                viewMode === pill.id
                                    ? 'bg-surface text-default shadow-sm'
                                    : 'text-secondary hover:text-default'
                            }`}
                        >
                            {pill.label}
                        </button>
                    ))}
                </div>

                {/* Search */}
                {totalCount > 3 && (
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
                            <button onClick={() => setSearchTerm('')}
                                    className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-secondary">
                                <X className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                )}
            </div>

            {/* Table */}
            {filtered.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <ShieldOff className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {totalCount === 0 ? 'No Guardrails Apply' : 'No Matches'}
                    </p>
                    <p className="text-sm text-muted mt-2">
                        {totalCount === 0
                            ? 'Assign guardrails from the Guardrails section'
                            : searchTerm
                                ? `No guardrails match "${searchTerm}"`
                                : `This group has no ${viewMode} guardrails`
                        }
                    </p>
                </div>
            ) : (
                <div className="bg-surface rounded-xl border border-default overflow-x-auto">
                    <table className="min-w-full divide-y divide-default">
                        <thead className="bg-surface-alt">
                        <tr>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Configuration
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Source
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Active Checks
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Surfaces
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Behaviour
                            </th>
                        </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                        {filtered.map(config => {
                            const checks = getCheckLabels(config);
                            const surfaces = surfaceGroupLabels(config.surfaces);

                            return (
                                <tr key={config.id} className="hover:bg-surface-alt transition-colors">
                                    {/* Configuration */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className="p-1.5 rounded-lg bg-primary-100 flex-shrink-0">
                                                <ShieldCheck className="w-4 h-4 text-primary-600" />
                                            </div>
                                            <div className="min-w-0">
                                                <div className="flex items-center space-x-2">
                                                        <span className="text-sm font-semibold text-default truncate">
                                                            {config.name}
                                                        </span>
                                                    {config.is_default && (
                                                        <span className="inline-flex items-center space-x-1 px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-surface-alt text-amber-700">
                                                                <Star className="w-2.5 h-2.5" />
                                                                <span>Default</span>
                                                            </span>
                                                    )}
                                                </div>
                                                {config.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[240px]">
                                                        {config.description}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* Source */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        {config.is_inherited ? (
                                            <div className="flex flex-col">
                                                    <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-xs font-medium rounded-full bg-surface-alt text-secondary w-fit">
                                                        <ArrowUpRight className="w-3 h-3" />
                                                        <span>Inherited</span>
                                                    </span>
                                                {config.source_group_name && (
                                                    <span className="text-[10px] text-muted mt-0.5">
                                                            from {config.source_group_name}
                                                        </span>
                                                )}
                                            </div>
                                        ) : (
                                            <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-primary-100 text-primary-700">
                                                    Direct
                                                </span>
                                        )}
                                    </td>

                                    {/* Active Checks */}
                                    <td className="px-6 py-4">
                                        {checks.length === 0 ? (
                                            <span className="text-xs text-muted italic">None</span>
                                        ) : (
                                            <div className="flex flex-wrap gap-1">
                                                {checks.map((label, i) => (
                                                    <span
                                                        key={i}
                                                        className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary"
                                                    >
                                                            {label}
                                                        </span>
                                                ))}
                                            </div>
                                        )}
                                    </td>

                                    {/* Surfaces */}
                                    <td className="px-6 py-4">
                                        {surfaces.length === 0 ? (
                                            <span className="text-xs text-muted italic">None</span>
                                        ) : (
                                            <div className="flex flex-wrap gap-1">
                                                {surfaces.map(s => (
                                                    <span
                                                        key={s}
                                                        className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary"
                                                    >
                                                            {s}
                                                        </span>
                                                ))}
                                            </div>
                                        )}
                                    </td>

                                    {/* Behaviour */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3 text-xs text-muted">
                                                <span className={config.fail_open ? 'text-amber-600' : 'text-green-600'}>
                                                    {config.fail_open ? 'Fail-open' : 'Fail-closed'}
                                                </span>
                                            <span>{config.max_check_timeout_ms}ms</span>
                                            {config.priority > 0 && (
                                                <span>P{config.priority}</span>
                                            )}
                                        </div>
                                    </td>
                                </tr>
                            );
                        })}
                        </tbody>
                    </table>
                </div>
            )}

            {/* Footer counts */}
            {totalCount > 0 && (
                <div className="flex items-center space-x-6 text-xs text-muted px-1">
                    <span>Total: <strong className="text-secondary">{totalCount}</strong></span>
                    {directCount > 0 && <span>Direct: <strong className="text-primary-700">{directCount}</strong></span>}
                    {inheritedCount > 0 && <span>Inherited: <strong className="text-secondary">{inheritedCount}</strong></span>}
                </div>
            )}
        </div>
    );
};

export default GroupGuardrailsTab;