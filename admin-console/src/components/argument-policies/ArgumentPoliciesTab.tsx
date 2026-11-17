/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Argument Policies Governance tab — AST-aware checks.
 *
 * Styling mirrors `components/guardrails/GuardrailsTab.tsx` so the
 * two Governance pages have a single visual vocabulary.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    AlertCircle,
    Code2,
    Download,
    Edit,
    FileUp,
    Loader2,
    Plus,
    RefreshCw,
    Search,
    ShieldCheck,
    ShieldOff,
    Trash2,
    XCircle,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import GroupTreeDisplay from '../common/GroupTreeDisplay';
import InfoBanner from '../common/InfoBanner';
import ArgumentPolicyForm from './ArgumentPolicyForm';
import type { Analyzer, ArgumentPolicy, SeveritiesBySurface, Surface } from '../../lib/argumentPolicyTypes';
import {
    ANALYZER_LABEL,
    SURFACE_LABEL,
} from '../../lib/argumentPolicyTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';


const ArgumentPoliciesTab: React.FC = () => {
    const {
        listArgumentPolicies,
        deleteArgumentPolicy,
        exportArgumentPoliciesYaml,
        importArgumentPoliciesYaml,
    } = useGateway();
    const toast = useToast();

    const [policies, setPolicies] = useState<ArgumentPolicy[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [formOpen, setFormOpen] = useState(false);
    const [editing, setEditing] = useState<ArgumentPolicy | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<ArgumentPolicy | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const [surfaceFilter, setSurfaceFilter] = useState<Surface | ''>('');
    const [analyzerFilter, setAnalyzerFilter] = useState<Analyzer | ''>('');
    const [searchTerm, setSearchTerm] = useState('');
    // The severities each surface takes, as the list answers them: what the form offers.
    const [severitiesBySurface, setSeveritiesBySurface] = useState<SeveritiesBySurface>({});

    const loadPolicies = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const result = await listArgumentPolicies({
                surface: surfaceFilter || undefined,
                analyzer: analyzerFilter || undefined,
            });
            setPolicies(result.policies);
            setSeveritiesBySurface(result.severities_by_surface ?? {});
        } catch (err) {
            setError(errorMessage(err, 'Failed to load argument policies'));
            toast.error('Failed to Load', errorMessage(err, 'Unable to load argument policies'));
        } finally {
            setLoading(false);
        }
    }, [listArgumentPolicies, surfaceFilter, analyzerFilter, toast]);

    useEffect(() => {
        runBackground(loadPolicies(), 'policies');
    }, [loadPolicies]);

    const handleDeleteConfirm = async () => {
        if (!deleteTarget) return;
        setIsDeleting(true);
        setDeleteError(null);
        try {
            await deleteArgumentPolicy(deleteTarget.id);
            setPolicies(prev => prev.filter(p => p.id !== deleteTarget.id));
            toast.success('Policy Deleted', `"${deleteTarget.name}" has been deleted.`);
            setDeleteTarget(null);
        } catch (err) {
            const msg = errorMessage(err, 'Failed to delete policy');
            setDeleteError(msg);
            toast.error('Delete Failed', msg);
        } finally {
            setIsDeleting(false);
        }
    };

    const handleExport = async () => {
        try {
            const yaml = await exportArgumentPoliciesYaml();
            const blob = new Blob([yaml], { type: 'application/x-yaml' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `argument-policies-${new Date().toISOString().slice(0, 10)}.yaml`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast.success('Export Complete', 'Argument policies downloaded as YAML.');
        } catch (err) {
            toast.error('Export Failed', errorMessage(err, 'Unable to export policies'));
        }
    };

    const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        try {
            const yaml = await file.text();
            const result = await importArgumentPoliciesYaml(yaml);
            const summary =
                `${result.created.length} created, ` +
                `${result.updated.length} updated, ${result.unchanged.length} unchanged`;
            if (result.errors.length || result.unresolved_groups.length) {
                toast.warning(
                    'Import Complete (with warnings)',
                    `${summary} — ${result.errors.length} error(s), ` +
                    `${result.unresolved_groups.length} unresolved group(s)`,
                );
            } else {
                toast.success('Import Complete', summary);
            }
            await loadPolicies();
        } catch (err) {
            toast.error('Import Failed', errorMessage(err, 'Unable to import YAML'));
        } finally {
            e.target.value = '';
        }
    };

    const handleFormSaved = async () => {
        setFormOpen(false);
        setEditing(null);
        await loadPolicies();
    };

    const filteredPolicies = policies.filter(p => {
        if (!searchTerm) return true;
        const term = searchTerm.toLowerCase();
        return (
            p.name.toLowerCase().includes(term) ||
            (p.description || '').toLowerCase().includes(term) ||
            p.target.toLowerCase().includes(term) ||
            p.argument_key.toLowerCase().includes(term)
        );
    });

    const totalActive = policies.filter(p => p.enabled).length;

    // ========================================================================
    // Loading State
    // ========================================================================

    if (loading && policies.length === 0) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <Loader2 className="w-8 h-8 text-primary-500 animate-spin mx-auto mb-3" />
                        <p className="text-secondary">Loading argument policies...</p>
                    </div>
                </div>
            </div>
        );
    }

    // ========================================================================
    // Error State
    // ========================================================================

    if (error && policies.length === 0) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <AlertCircle className="w-12 h-12 text-red-400 mx-auto mb-4" />
                        <h3 className="text-lg font-semibold text-default mb-2">Failed to Load Policies</h3>
                        <p className="text-secondary mb-4">{error}</p>
                        <button
                            onClick={asyncHandler(loadPolicies, 'click')}
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
                        <h1 className="text-2xl font-bold text-default">Argument Policies</h1>
                        <p className="text-sm text-muted">
                            {policies.length === 0
                                ? 'AST-aware checks on tool/skill arguments — SQL, URL, shell, path, JSON'
                                : `${policies.length} polic${policies.length !== 1 ? 'ies' : 'y'}, ${totalActive} active`
                            }
                        </p>
                    </div>
                </div>

                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(loadPolicies, 'click')}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-5 h-5 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                        onClick={asyncHandler(handleExport, 'click')}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors"
                        title="Export every policy as YAML"
                    >
                        <Download className="w-4 h-4" />
                        <span>Export YAML</span>
                    </button>
                    <label
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-primary-700 bg-primary-50 border border-primary-200 rounded-lg hover:bg-primary-100 transition-colors cursor-pointer"
                        title="Apply a YAML bundle (idempotent upsert by name)"
                    >
                        <FileUp className="w-4 h-4" />
                        <span>Import YAML</span>
                        <input
                            type="file"
                            accept=".yaml,.yml,application/x-yaml"
                            onChange={asyncHandler(handleImport, 'change')}
                            className="hidden"
                        />
                    </label>
                    <button
                        onClick={() => { setEditing(null); setFormOpen(true); }}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        <Plus className="w-4 h-4" />
                        <span>New Policy</span>
                    </button>
                </div>
            </div>

            {/* ============ HOW IT WORKS ============ */}
            <InfoBanner title="How argument policies work">
                <p>
                    AST-aware argument checks for LLM tool_calls, MCP tool/call inbound, and skill
                    execution. Where guardrails ask <em>"does this text contain something bad,"</em> an
                    argument policy parses the structured argument and decides <em>"does this SQL write,
                    is this URL a private IP, is this shell command outside the allowlist."</em> Each
                    policy is bound to one or more Resource Groups (same model as Guardrails).
                </p>
                <p className="mt-2">
                    Abstract Syntax Tree (AST) policy checks parse code into a hierarchical data
                    structure of nodes (representing functions, variables, and loops). In contrast,
                    RegEx (Regular Expression) checks treat code as a flat string, looking only for
                    text patterns.
                </p>
            </InfoBanner>

            {/* ============ FILTERS + SEARCH ============ */}
            <div className="flex flex-wrap items-center gap-3">
                <div className="relative flex-1 min-w-[200px] min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={e => setSearchTerm(e.target.value)}
                        placeholder="Search by name, description, target, or argument..."
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
                <select
                    value={surfaceFilter}
                    onChange={(e) => setSurfaceFilter(e.target.value as Surface | '')}
                    className="px-3 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                >
                    <option value="">All surfaces</option>
                    {(Object.keys(SURFACE_LABEL) as Surface[]).map(s => (
                        <option key={s} value={s}>{SURFACE_LABEL[s]}</option>
                    ))}
                </select>
                <select
                    value={analyzerFilter}
                    onChange={(e) => setAnalyzerFilter(e.target.value as Analyzer | '')}
                    className="px-3 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                >
                    <option value="">All analyzers</option>
                    {(Object.keys(ANALYZER_LABEL) as Analyzer[]).map(a => (
                        <option key={a} value={a}>{ANALYZER_LABEL[a]}</option>
                    ))}
                </select>
            </div>

            {/* ============ TABLE / EMPTY STATE ============ */}
            {filteredPolicies.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    {policies.length === 0 ? (
                        <>
                            <ShieldOff className="w-14 h-14 text-disabled mx-auto mb-4" />
                            <h3 className="text-lg font-semibold text-secondary mb-2">No Argument Policies</h3>
                            <p className="text-sm text-muted max-w-md mx-auto mb-6">
                                Create your first argument policy to apply AST-aware checks on tool
                                and skill arguments — block writes, prevent SSRF, restrict shell commands.
                            </p>
                            <button
                                onClick={() => { setEditing(null); setFormOpen(true); }}
                                className="inline-flex items-center space-x-2 px-5 py-2.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Create First Policy</span>
                            </button>
                        </>
                    ) : (
                        <>
                            <Search className="w-12 h-12 text-disabled mx-auto mb-4" />
                            <h3 className="text-lg font-semibold text-secondary mb-1">No matches</h3>
                            <p className="text-sm text-muted">
                                No policies match the current filters.
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
                                    Policy
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Status
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
                                    Groups
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                    Actions
                                </th>
                            </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                            {filteredPolicies.map(policy => (
                                <tr key={policy.id} className="hover:bg-surface-alt transition-colors">
                                    {/* ── Policy ──── */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className={`p-1.5 rounded-lg flex-shrink-0 ${policy.enabled ? 'bg-primary-100' : 'bg-surface-raised'}`}>
                                                {policy.enabled ? (
                                                    <ShieldCheck className="w-4 h-4 text-primary-600" />
                                                ) : (
                                                    <ShieldOff className="w-4 h-4 text-muted" />
                                                )}
                                            </div>
                                            <div className="min-w-0">
                                                <div className="text-sm font-semibold text-default truncate">
                                                    {policy.name}
                                                </div>
                                                <div className="text-xs text-muted font-mono truncate" title={policy.id}>
                                                    {policy.id}
                                                </div>
                                                {policy.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[280px]">
                                                        {policy.description}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* ── Status ──── */}
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

                                    {/* ── Surface ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary">
                                            {SURFACE_LABEL[policy.surface]}
                                        </span>
                                    </td>

                                    {/* ── Target ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs font-mono text-secondary">
                                            {policy.target}
                                        </span>
                                    </td>

                                    {/* ── Argument ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs font-mono text-secondary">
                                            {policy.argument_key}
                                        </span>
                                    </td>

                                    {/* ── Analyzer ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-[11px] font-medium rounded-full bg-primary-100 text-primary-700">
                                            <Code2 className="w-3 h-3" />
                                            <span>{ANALYZER_LABEL[policy.analyzer]}</span>
                                        </span>
                                    </td>

                                    {/* ── Rules ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-sm text-secondary">{policy.rules.length}</span>
                                    </td>

                                    {/* ── Groups ──── */}
                                    <td className="px-6 py-4">
                                        <GroupTreeDisplay
                                            groups={policy.groups || []}
                                            variant="compact"
                                            maxVisible={2}
                                        />
                                    </td>

                                    {/* ── Actions ──── */}
                                    <td className="px-6 py-4 whitespace-nowrap text-right">
                                        <div className="flex items-center justify-end space-x-1">
                                            <button
                                                onClick={() => { setEditing(policy); setFormOpen(true); }}
                                                className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                title="Edit policy"
                                            >
                                                <Edit className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => { setDeleteTarget(policy); setDeleteError(null); }}
                                                className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                title="Delete policy"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {/* ============ FORM MODAL ============ */}
            {formOpen && (
                <ArgumentPolicyForm
                    policy={editing}
                    severitiesBySurface={severitiesBySurface}
                    onClose={(saved) => {
                        if (saved) {
                            runBackground(handleFormSaved(), 'form saved');
                        } else {
                            setFormOpen(false);
                            setEditing(null);
                        }
                    }}
                />
            )}

            {/* ============ DELETE MODAL ============ */}
            {deleteTarget && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => { setDeleteTarget(null); setDeleteError(null); }}
                    onConfirm={asyncHandler(handleDeleteConfirm, 'confirm')}
                    itemName={deleteTarget.name}
                    itemType="argument-policy"
                    isDeleting={isDeleting}
                    errorMessage={deleteError}
                />
            )}
        </div>
    );
};

export default ArgumentPoliciesTab;
