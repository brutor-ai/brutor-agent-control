/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    ShieldCheck,
    ShieldOff,
    RefreshCw,
    TestTube,
    AlertCircle,
    XCircle,
    Edit,
    Loader2,
    ToggleLeft,
    ToggleRight,
    Plus,
    Trash2,
    Star,
    Search,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import GuardrailForm from './GuardrailForm';
import BreakGlassPanel from './BreakGlassPanel';
import GuardrailTestPanel from './GuardrailTestPanel';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import GroupTreeDisplay from '../common/GroupTreeDisplay';
import InfoBanner from '../common/InfoBanner';
import type {
    GuardrailConfigResponse,
    GuardrailProviderInfo,
} from '../../lib/guardrailTypes';
import {
    getActionConfig,
    surfaceGroupLabels,
} from '../../lib/guardrailTypes';
import { errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';


// ============================================================================
// Helpers
// ============================================================================

/**
 * Count operator-set per-surface overrides on a check.
 *
 * Returns the number of surfaces in the map (e.g. `{chat_output:{...},
 * mcp_input:{...}}` → 2). NULL / empty / malformed all collapse to 0
 * so the row never claims overrides that aren't there. Used to
 * decorate the Active Checks column so the operator sees at a glance
 * that the scalar action isn't the whole story.
 */
function countPerSurfaceOverrides(per_surface: unknown): number {
    if (!per_surface || typeof per_surface !== 'object' || Array.isArray(per_surface)) {
        return 0;
    }
    return Object.keys(per_surface).length;
}

/** Render a "(+N surface)" suffix when at least one override exists. */
function perSurfaceSuffix(per_surface: unknown): string {
    const n = countPerSurfaceOverrides(per_surface);
    if (n === 0) return '';
    return ` (+${n} surface${n !== 1 ? 's' : ''})`;
}

/** Summarise active checks as short labels for the table */
function getCheckSummary(config: GuardrailConfigResponse): string[] {
    const checks: string[] = [];
    const b = config.builtin;

    if (b.pii.enabled) {
        checks.push(`PII → ${getActionConfig(b.pii.action).label}${perSurfaceSuffix(b.pii.per_surface)}`);
    }
    if (b.prompt_injection.enabled) {
        checks.push(`Injection → ${getActionConfig(b.prompt_injection.action).label}${perSurfaceSuffix(b.prompt_injection.per_surface)}`);
    }
    if (b.jailbreak.enabled) {
        checks.push(`Jailbreak → ${getActionConfig(b.jailbreak.action).label}${perSurfaceSuffix(b.jailbreak.per_surface)}`);
    }
    if (b.toxic_content.enabled) {
        checks.push(`Toxic → ${getActionConfig(b.toxic_content.action).label}${perSurfaceSuffix(b.toxic_content.per_surface)}`);
    }
    if (b.secrets?.enabled) {
        checks.push(`Secrets → ${getActionConfig(b.secrets.action).label}${perSurfaceSuffix(b.secrets.per_surface)}`);
    }

    // Banned words / patterns: surface the per-surface count even when
    // the global list is empty — an override of the form
    // `{mcp_input: {words: [...]}}` counts as active config the
    // operator should see, otherwise the row shows nothing for a check
    // that's clearly doing work on a request.
    const bw = b.banned_words?.length || 0;
    const bwOverrides = countPerSurfaceOverrides(b.banned_words_per_surface);
    if (bw > 0 || bwOverrides > 0) {
        const base = bw > 0 ? `${bw} word${bw !== 1 ? 's' : ''}` : 'words';
        const suffix = bwOverrides > 0 ? ` (+${bwOverrides} surface${bwOverrides !== 1 ? 's' : ''})` : '';
        checks.push(`${base}${suffix}`);
    }
    const bp = b.banned_patterns?.length || 0;
    const bpOverrides = countPerSurfaceOverrides(b.banned_patterns_per_surface);
    if (bp > 0 || bpOverrides > 0) {
        const base = bp > 0 ? `${bp} pattern${bp !== 1 ? 's' : ''}` : 'patterns';
        const suffix = bpOverrides > 0 ? ` (+${bpOverrides} surface${bpOverrides !== 1 ? 's' : ''})` : '';
        checks.push(`${base}${suffix}`);
    }

    const ap = config.providers.filter(p => p.enabled).length;
    if (ap > 0) checks.push(`${ap} provider${ap !== 1 ? 's' : ''}`);

    return checks;
}



// ============================================================================
// Main Component
// ============================================================================

const GuardrailsTab: React.FC = () => {
    const {
        listGuardrailConfigs,
        updateGuardrailConfigById,
        deleteGuardrailConfig,
        fetchGuardrailProviders,
    } = useGateway();

    const toast = useToast();

    // Data state
    const [configs, setConfigs] = useState<GuardrailConfigResponse[]>([]);
    const [providers, setProviders] = useState<GuardrailProviderInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    // UI state
    const [showCreateForm, setShowCreateForm] = useState(false);
    const [editingConfig, setEditingConfig] = useState<GuardrailConfigResponse | null>(null);
    const [showTestPanel, setShowTestPanel] = useState(false);
    const [testConfigId, setTestConfigId] = useState<string | undefined>(undefined);
    const [togglingId, setTogglingId] = useState<string | null>(null);
    const [searchTerm, setSearchTerm] = useState('');

    // Delete confirmation
    const [deleteTarget, setDeleteTarget] = useState<GuardrailConfigResponse | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // ========================================================================
    // Data Loading
    // ========================================================================

    const loadConfigs = async () => {
        try {
            setLoading(true);
            setError('');
            const [configsResp, providerResp] = await Promise.all([
                listGuardrailConfigs(),
                fetchGuardrailProviders(),
            ]);
            setConfigs(configsResp.configs);
            setProviders(providerResp.providers);
        } catch (err) {
            console.error('Failed to load guardrail configs:', errorKind(err));
            setError(errorMessage(err, 'Failed to load guardrail configurations'));
            toast.error('Failed to Load', errorMessage(err, 'Unable to load guardrail configurations'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        runBackground(loadConfigs(), 'configs');
    }, []);

    // ========================================================================
    // Quick Toggle
    // ========================================================================

    const handleToggleEnabled = async (config: GuardrailConfigResponse) => {
        setTogglingId(config.id);
        const b = config.builtin;
        try {
            await updateGuardrailConfigById(config.id, {
                enabled: !config.enabled,
                surfaces: config.surfaces,
                // `builtin` is a full replacement on the server: a field left
                // out here is written back as its default, which would turn
                // secrets detection off and reset providers. Send all of it.
                builtin: {
                    pii_enabled: b.pii.enabled,
                    pii_action: b.pii.action,
                    pii_provider: b.pii.provider,
                    pii_categories: b.pii.categories,
                    pii_threshold: b.pii.threshold,
                    pii_surfaces: b.pii.surfaces,
                    pii_per_surface: b.pii.per_surface,
                    prompt_injection_enabled: b.prompt_injection.enabled,
                    prompt_injection_action: b.prompt_injection.action,
                    prompt_injection_sensitivity: b.prompt_injection.sensitivity,
                    prompt_injection_provider: b.prompt_injection.provider,
                    prompt_injection_surfaces: b.prompt_injection.surfaces,
                    prompt_injection_per_surface: b.prompt_injection.per_surface,
                    jailbreak_enabled: b.jailbreak.enabled,
                    jailbreak_action: b.jailbreak.action,
                    jailbreak_provider: b.jailbreak.provider,
                    jailbreak_surfaces: b.jailbreak.surfaces,
                    jailbreak_per_surface: b.jailbreak.per_surface,
                    toxic_content_enabled: b.toxic_content.enabled,
                    toxic_content_action: b.toxic_content.action,
                    toxic_content_threshold: b.toxic_content.threshold,
                    toxic_content_provider: b.toxic_content.provider,
                    toxic_content_category_thresholds: b.toxic_content.category_thresholds,
                    toxic_content_surfaces: b.toxic_content.surfaces,
                    toxic_content_per_surface: b.toxic_content.per_surface,
                    banned_words: b.banned_words,
                    banned_patterns: b.banned_patterns,
                    banned_words_match_mode: b.banned_words_match_mode,
                    banned_words_surfaces: b.banned_words_surfaces,
                    banned_patterns_surfaces: b.banned_patterns_surfaces,
                    banned_words_per_surface: b.banned_words_per_surface,
                    banned_patterns_per_surface: b.banned_patterns_per_surface,
                    secrets_enabled: b.secrets?.enabled,
                    secrets_action: b.secrets?.action,
                    secrets_categories: b.secrets?.categories,
                    secrets_surfaces: b.secrets?.surfaces,
                    secrets_threshold: b.secrets?.threshold,
                    secrets_per_surface: b.secrets?.per_surface,
                },
                providers: config.providers.map(p => ({
                    provider: p.provider,
                    enabled: p.enabled,
                    endpoint: p.endpoint,
                    config: p.config,
                })),
                max_check_timeout_ms: config.max_check_timeout_ms,
                fail_open: config.fail_open,
                fail_open_overrides: config.fail_open_overrides,
                log_all_checks: config.log_all_checks,
            });

            setConfigs(prev => prev.map(c =>
                c.id === config.id ? { ...c, enabled: !c.enabled } : c
            ));

            toast.success(
                !config.enabled ? 'Guardrails Enabled' : 'Guardrails Disabled',
                !config.enabled
                    ? `"${config.name}" is now active.`
                    : `"${config.name}" has been disabled.`
            );
        } catch (err) {
            toast.error('Toggle Failed', errorMessage(err, 'Unable to toggle guardrails'));
        } finally {
            setTogglingId(null);
        }
    };

    // ========================================================================
    // Delete
    // ========================================================================

    const handleDeleteConfirm = async () => {
        if (!deleteTarget) return;
        setIsDeleting(true);
        setDeleteError(null);

        try {
            await deleteGuardrailConfig(deleteTarget.id);
            setConfigs(prev => prev.filter(c => c.id !== deleteTarget.id));
            toast.success('Config Deleted', `"${deleteTarget.name}" has been deleted.`);
            setDeleteTarget(null);
        } catch (err) {
            const msg = errorMessage(err, 'Failed to delete guardrail configuration');
            setDeleteError(msg);
            toast.error('Delete Failed', msg);
        } finally {
            setIsDeleting(false);
        }
    };

    // ========================================================================
    // After form save
    // ========================================================================

    const handleFormSaved = async () => {
        setShowCreateForm(false);
        setEditingConfig(null);
        await loadConfigs();
    };

    // ========================================================================
    // Filtering
    // ========================================================================

    const filteredConfigs = configs.filter(c => {
        if (!searchTerm) return true;
        const term = searchTerm.toLowerCase();
        return (
            c.name.toLowerCase().includes(term) ||
            (c.description || '').toLowerCase().includes(term)
        );
    });

    const totalActive = configs.filter(c => c.enabled).length;

    // ========================================================================
    // Loading State
    // ========================================================================

    if (loading) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <Loader2 className="w-8 h-8 text-primary-500 animate-spin mx-auto mb-3" />
                        <p className="text-secondary">Loading guardrail configurations...</p>
                    </div>
                </div>
            </div>
        );
    }

    // ========================================================================
    // Error State
    // ========================================================================

    if (error && configs.length === 0) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <AlertCircle className="w-12 h-12 text-red-400 mx-auto mb-4" />
                        <h3 className="text-lg font-semibold text-default mb-2">Failed to Load Configurations</h3>
                        <p className="text-secondary mb-4">{error}</p>
                        <button
                            onClick={asyncHandler(loadConfigs, 'click')}
                            className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-lg text-sm font-medium transition-colors"
                        >
                            Retry
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    // ========================================================================
    // Render
    // ========================================================================

    return (
        <div className="p-6 space-y-6">
            {/* Break-glass state sits above everything else on this
                page. If checks are degraded to log-and-allow, that is the
                single most important fact about the tenant's guardrails and
                it must not be something you scroll to find. */}
            <BreakGlassPanel />

            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className={`p-2.5 rounded-xl ${totalActive > 0 ? 'bg-primary-100' : 'bg-surface-raised'}`}>
                        {totalActive > 0 ? (
                            <ShieldCheck className="w-7 h-7 text-primary-600" />
                        ) : (
                            <ShieldOff className="w-7 h-7 text-muted" />
                        )}
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">Guardrails</h1>
                        <p className="text-sm text-muted">
                            {configs.length === 0
                                ? 'Content filtering and safety policies across LLM, MCP, and Agent Skill surfaces'
                                : `${configs.length} configuration${configs.length !== 1 ? 's' : ''}, ${totalActive} active`
                            }
                        </p>
                    </div>
                </div>

                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(loadConfigs, 'click')}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className="w-5 h-5" />
                    </button>
                    <button
                        onClick={() => { setTestConfigId(undefined); setShowTestPanel(true); }}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors"
                    >
                        <TestTube className="w-4 h-4" />
                        <span>Test Content</span>
                    </button>
                    <button
                        onClick={() => setShowCreateForm(true)}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        <Plus className="w-4 h-4" />
                        <span>New Config</span>
                    </button>
                </div>
            </div>

            {/* ============ HOW GUARDRAILS WORK — before the table ============ */}
            <InfoBanner title="How guardrail configurations work">
                <p>
                    A config with no resource group associations acts as the tenant default.
                    Configs associated with specific groups only apply to users in those groups.
                    When multiple configs match, the most restrictive action wins
                    (Block &gt; Redact &gt; Warn &gt; Allow).
                </p>
            </InfoBanner>

            {/* ============ SEARCH BAR ============ */}
            {configs.length > 3 && (
                <div className="relative">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={e => setSearchTerm(e.target.value)}
                        placeholder="Search guardrail configurations..."
                        className="w-full pl-10 pr-4 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                    {searchTerm && (
                        <button
                            onClick={() => setSearchTerm('')}
                            className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary"
                        >
                            <XCircle className="w-4 h-4" />
                        </button>
                    )}
                </div>
            )}

            {/* ============ CONFIG TABLE ============ */}
            {filteredConfigs.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    {configs.length === 0 ? (
                        <>
                            <ShieldOff className="w-14 h-14 text-disabled mx-auto mb-4" />
                            <h3 className="text-lg font-semibold text-secondary mb-2">No Guardrail Configurations</h3>
                            <p className="text-sm text-muted max-w-md mx-auto mb-6">
                                Create your first guardrail configuration to enable content filtering
                                and safety policies across LLM, MCP, Agent Skill and A2A v1.0 surfaces.
                            </p>
                            <button
                                onClick={() => setShowCreateForm(true)}
                                className="inline-flex items-center space-x-2 px-5 py-2.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Create First Config</span>
                            </button>
                        </>
                    ) : (
                        <>
                            <Search className="w-12 h-12 text-disabled mx-auto mb-4" />
                            <h3 className="text-lg font-semibold text-secondary mb-1">No matches</h3>
                            <p className="text-sm text-muted">
                                No configurations match &ldquo;{searchTerm}&rdquo;
                            </p>
                        </>
                    )}
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
                                Status
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Active Checks
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Surfaces
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                Groups
                            </th>
                            <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                Actions
                            </th>
                        </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                        {filteredConfigs.map(config => {
                            const checks = getCheckSummary(config);
                            const surfaces = surfaceGroupLabels(config.surfaces);
                            const toggling = togglingId === config.id;

                            return (
                                <tr key={config.id} className="hover:bg-surface-alt transition-colors">
                                    {/* ── Configuration ──── */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className={`p-1.5 rounded-lg flex-shrink-0 ${config.enabled ? 'bg-primary-100' : 'bg-surface-raised'}`}>
                                                {config.enabled ? (
                                                    <ShieldCheck className="w-4 h-4 text-primary-600" />
                                                ) : (
                                                    <ShieldOff className="w-4 h-4 text-muted" />
                                                )}
                                            </div>
                                            <div className="min-w-0">
                                                <div className="flex items-center space-x-2">
                                                        <span className="text-sm font-semibold text-default truncate">
                                                            {config.name}
                                                        </span>
                                                    {config.is_default && (
                                                        <span className="inline-flex items-center space-x-1 px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-amber-100 text-amber-700">
                                                                <Star className="w-2.5 h-2.5" />
                                                                <span>Default</span>
                                                            </span>
                                                    )}
                                                </div>
                                                <div className="text-xs text-muted font-mono truncate" title={config.id}>{config.id}</div>
                                                {config.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[240px]">
                                                        {config.description}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* ── Status ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                            <span className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full ${
                                                config.enabled
                                                    ? 'bg-green-100 text-green-700'
                                                    : 'bg-surface-raised text-muted'
                                            }`}>
                                                {config.enabled ? 'Active' : 'Inactive'}
                                            </span>
                                    </td>

                                    {/* ── Active Checks ──── */}
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

                                    {/* ── Surfaces ──── */}
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

                                    {/* ── Groups ──── */}
                                    <td className="px-6 py-4">
                                        <GroupTreeDisplay
                                            groups={config.groups || []}
                                            variant="compact"
                                            maxVisible={2}
                                        />
                                    </td>

                                    {/* ── Actions ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap text-right">
                                        <div className="flex items-center justify-end space-x-1">
                                            <button
                                                onClick={asyncHandler(() => handleToggleEnabled(config), 'click')}
                                                disabled={toggling}
                                                className={`p-1.5 rounded-lg transition-colors disabled:opacity-50 ${
                                                    config.enabled
                                                        ? 'text-green-600 hover:bg-green-50'
                                                        : 'text-muted hover:bg-surface-raised'
                                                }`}
                                                title={config.enabled ? 'Disable' : 'Enable'}
                                            >
                                                {toggling ? (
                                                    <Loader2 className="w-4 h-4 animate-spin" />
                                                ) : config.enabled ? (
                                                    <ToggleRight className="w-5 h-5" />
                                                ) : (
                                                    <ToggleLeft className="w-5 h-5" />
                                                )}
                                            </button>
                                            <button
                                                onClick={() => {
                                                    setTestConfigId(config.id);
                                                    setShowTestPanel(true);
                                                }}
                                                className="p-1.5 text-muted hover:text-primary-600 hover:bg-primary-50 rounded-lg transition-colors"
                                                title="Test this configuration"
                                            >
                                                <TestTube className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => setEditingConfig(config)}
                                                className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                title="Edit configuration"
                                            >
                                                <Edit className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => {
                                                    setDeleteTarget(config);
                                                    setDeleteError(null);
                                                }}
                                                className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                title="Delete configuration"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            );
                        })}
                        </tbody>
                    </table>
                </div>
            )}

            {/* ============ MODALS ============ */}

            {showCreateForm && (
                <GuardrailForm
                    config={null}
                    providers={providers}
                    onClose={() => setShowCreateForm(false)}
                    onSave={handleFormSaved}
                />
            )}

            {editingConfig && (
                <GuardrailForm
                    config={editingConfig}
                    providers={providers}
                    onClose={() => setEditingConfig(null)}
                    onSave={handleFormSaved}
                />
            )}

            {showTestPanel && (
                <GuardrailTestPanel
                    onClose={() => setShowTestPanel(false)}
                    initialConfigId={testConfigId}
                />
            )}

            {/* Delete Confirmation */}
            <GenericDeleteConfirmationModal
                isOpen={!!deleteTarget}
                onClose={() => {
                    setDeleteTarget(null);
                    setDeleteError(null);
                }}
                onConfirm={asyncHandler(handleDeleteConfirm, 'confirm')}
                itemType="guardrail"
                itemName={deleteTarget?.name || ''}
                itemInfo={deleteTarget ? {
                    id: deleteTarget.id,
                    description: deleteTarget.description,
                    enabled: deleteTarget.enabled,
                } : undefined}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />
        </div>
    );
};

export default GuardrailsTab;