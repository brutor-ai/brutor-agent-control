/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Semantic Governance Policies tab — natural-language, LLM-judged policy.
 *
 * Styling mirrors `components/argument-policies/ArgumentPoliciesTab.tsx` so the
 * two intent/governance pages share a single visual vocabulary. The create/edit
 * form is the SemanticPolicyForm modal (modelled on ArgumentPolicyForm).
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    AlertCircle,
    Edit,
    Loader2,
    Plus,
    RefreshCw,
    ScrollText,
    Search,
    ShieldCheck,
    ShieldOff,
    Trash2,
    XCircle,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import InfoBanner from '../common/InfoBanner';
import SemanticPolicyForm from './SemanticPolicyForm';
import { GroupHierarchyTree, formatGroupLabel } from '../../lib/groupTypes';
import type {
    PolicyAction,
    PolicyMode,
    SemanticPolicy,
    Surface,
} from '../../lib/semanticPolicyTypes';
import { ACTION_LABEL, SURFACE_LABEL, SURFACES } from '../../lib/semanticPolicyTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface JudgeModelRow {
    id: string;
    name?: string;
    model_name?: string;
    provider?: string;
}

const flattenGroups = (tree: GroupHierarchyTree[], out: Record<string, string> = {}): Record<string, string> => {
    for (const node of tree) {
        out[node.id] = formatGroupLabel(node) || node.id;
        if (Array.isArray(node.children)) flattenGroups(node.children, out);
    }
    return out;
};

const SemanticPoliciesTab: React.FC = () => {
    const { gatewayClient, fetchGroupHierarchyTree } = useGateway();
    const toast = useToast();

    const [policies, setPolicies] = useState<SemanticPolicy[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const [formOpen, setFormOpen] = useState(false);
    const [editing, setEditing] = useState<SemanticPolicy | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<SemanticPolicy | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const [modeFilter, setModeFilter] = useState<PolicyMode | ''>('');
    const [actionFilter, setActionFilter] = useState<PolicyAction | ''>('');
    const [surfaceFilter, setSurfaceFilter] = useState<Surface | ''>('');
    const [searchTerm, setSearchTerm] = useState('');

    // Lookups for friendly display in the table.
    const [groupNames, setGroupNames] = useState<Record<string, string>>({});
    const [judgeNames, setJudgeNames] = useState<Record<string, string>>({});
    const [readFailure, reads] = useReadFailures<'groups' | 'judges'>();

    const loadPolicies = useCallback(async () => {
        if (!gatewayClient) return;
        setLoading(true);
        setError('');
        try {
            const r = await gatewayClient.listSemanticPolicies();
            setPolicies(r.policies || []);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load semantic policies'));
            toast.error('Failed to Load', errorMessage(err, 'Unable to load semantic policies'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, toast]);

    const loadLookups = useCallback(async () => {
        try {
            const r = await fetchGroupHierarchyTree();
            setGroupNames(flattenGroups(r.tree || []));
            reads.loaded('groups');
        } catch (err) {
            reads.failed('groups', 'Failed to load the names of the resource groups; groups are shown by id', err);
        }
        if (!gatewayClient) return;
        try {
            const resp = await gatewayClient.listLLMs({ mode: 'chat', enabled: true });
            const raw: unknown = resp?.models ?? resp;
            const list: JudgeModelRow[] = Array.isArray(raw)
                ? (raw as JudgeModelRow[])
                : raw && typeof raw === 'object'
                  ? Object.values(raw as Record<string, JudgeModelRow>)
                  : [];
            const map: Record<string, string> = {};
            for (const m of list) map[m.id] = m.name || m.model_name || m.id;
            setJudgeNames(map);
            reads.loaded('judges');
        } catch (err) {
            reads.failed('judges', 'Failed to load the names of the judge models; models are shown by id', err);
        }
    }, [gatewayClient, fetchGroupHierarchyTree, reads]);

    useEffect(() => {
        runBackground(loadPolicies(), 'policies');
        runBackground(loadLookups(), 'lookups');
    }, [loadPolicies, loadLookups]);

    const handleDeleteConfirm = async () => {
        if (!deleteTarget || !gatewayClient) return;
        setIsDeleting(true);
        setDeleteError(null);
        try {
            await gatewayClient.deleteSemanticPolicy(deleteTarget.id);
            setPolicies((prev) => prev.filter((p) => p.id !== deleteTarget.id));
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

    const toggleMode = async (p: SemanticPolicy) => {
        if (!gatewayClient) return;
        const next: PolicyMode = p.mode === 'shadow' ? 'enforce' : 'shadow';
        try {
            await gatewayClient.updateSemanticPolicy(p.id, { mode: next });
            toast.success('Mode updated', `"${p.name}" is now ${next}.`);
            await loadPolicies();
        } catch (err) {
            toast.error('Update failed', errorMessage(err, 'Could not change mode'));
        }
    };

    const handleFormSaved = async () => {
        setFormOpen(false);
        setEditing(null);
        await loadPolicies();
    };

    const filtered = policies.filter((p) => {
        if (modeFilter && p.mode !== modeFilter) return false;
        if (actionFilter && p.action !== actionFilter) return false;
        if (surfaceFilter) {
            const list = p.surfaces?.length ? p.surfaces : SURFACES; // empty = all
            if (!list.includes(surfaceFilter)) return false;
        }
        if (searchTerm) {
            const term = searchTerm.toLowerCase();
            if (
                !p.name.toLowerCase().includes(term) &&
                !(p.description || '').toLowerCase().includes(term) &&
                !p.constraint_text.toLowerCase().includes(term)
            ) {
                return false;
            }
        }
        return true;
    });

    const totalActive = policies.filter((p) => p.enabled).length;

    const renderGroups = (ids: string[]) => {
        if (!ids || ids.length === 0) {
            return <span className="text-xs text-muted italic">Tenant-wide</span>;
        }
        const names = ids.map((id) => groupNames[id] || id);
        const shown = names.slice(0, 2);
        return (
            <div className="flex flex-wrap gap-1">
                {shown.map((n) => (
                    <span key={n} className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-surface-raised text-secondary">
                        {n}
                    </span>
                ))}
                {names.length > 2 && (
                    <span className="text-[11px] text-muted">+{names.length - 2}</span>
                )}
            </div>
        );
    };

    if (loading && policies.length === 0) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <Loader2 className="w-8 h-8 text-primary-500 animate-spin mx-auto mb-3" />
                        <p className="text-secondary">Loading semantic policies...</p>
                    </div>
                </div>
            </div>
        );
    }

    if (error && policies.length === 0) {
        return (
            <div className="p-6">
                <div className="flex items-center justify-center py-20">
                    <div className="text-center">
                        <AlertCircle className="w-12 h-12 text-red-400 mx-auto mb-4" />
                        <h3 className="text-lg font-semibold text-default mb-2">Failed to Load Policies</h3>
                        <p className="text-secondary mb-4">{error}</p>
                        <button onClick={asyncHandler(loadPolicies, 'click')} className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-lg text-sm font-medium transition-colors">
                            Retry
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            <ReadFailureNotice failure={readFailure('groups')} onRetry={() => runBackground(loadLookups(), 'lookups')} />
            <ReadFailureNotice failure={readFailure('judges')} onRetry={() => runBackground(loadLookups(), 'lookups')} />
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
                        <h1 className="text-2xl font-bold text-default">Semantic Policies</h1>
                        <p className="text-sm text-muted">
                            {policies.length === 0
                                ? 'Natural-language, LLM-judged governance — ship in shadow, then enforce'
                                : `${policies.length} polic${policies.length !== 1 ? 'ies' : 'y'}, ${totalActive} active`}
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
                        onClick={() => { setEditing(null); setFormOpen(true); }}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                    >
                        <Plus className="w-4 h-4" />
                        <span>New Policy</span>
                    </button>
                </div>
            </div>

            {/* ============ HOW IT WORKS ============ */}
            <InfoBanner title="How semantic policies work">
                <p>
                    Where guardrails and argument policies match patterns or parse structure, a semantic
                    policy governs by <em>intent</em>: you write a constraint in plain English ("the
                    request must not exfiltrate customer PII") and an <strong>LLM judge</strong> evaluates
                    each governed request against it at runtime, returning a violated / not-violated
                    verdict. The judge call is itself a governed, logged, billed gateway call.
                </p>
                <p className="mt-2">
                    Each policy ships <strong>shadow → enforce</strong>: in <em>shadow</em> mode a
                    violation is logged as a would-block (visible in Mission Control → Governance) but
                    never blocks — so you measure judge accuracy before flipping to <em>enforce</em>,
                    where it <em>blocks</em> (403) or <em>warns</em>. Scope each policy to specific
                    surfaces (chat / MCP / A2A / skill) and bind it to Resource Groups (additive down the
                    tree; unbound = tenant-wide). Evaluation is fail-open — a judge error never blocks.
                </p>
            </InfoBanner>

            {/* ============ FILTERS + SEARCH ============ */}
            <div className="flex flex-wrap items-center gap-3">
                <div className="relative flex-1 min-w-[200px] min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                        placeholder="Search by name, description, or constraint..."
                        className="w-full pl-10 pr-4 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                    {searchTerm && (
                        <button onClick={() => setSearchTerm('')} className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary">
                            <XCircle className="w-4 h-4" />
                        </button>
                    )}
                </div>
                <select
                    value={modeFilter}
                    onChange={(e) => setModeFilter(e.target.value as PolicyMode | '')}
                    className="px-3 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                >
                    <option value="">All modes</option>
                    <option value="shadow">Shadow</option>
                    <option value="enforce">Enforce</option>
                </select>
                <select
                    value={actionFilter}
                    onChange={(e) => setActionFilter(e.target.value as PolicyAction | '')}
                    className="px-3 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                >
                    <option value="">All actions</option>
                    <option value="block">Block</option>
                    <option value="warn">Warn</option>
                </select>
                <select
                    value={surfaceFilter}
                    onChange={(e) => setSurfaceFilter(e.target.value as Surface | '')}
                    className="px-3 py-2.5 text-sm border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                >
                    <option value="">All surfaces</option>
                    {SURFACES.map((s) => (
                        <option key={s} value={s}>{SURFACE_LABEL[s]}</option>
                    ))}
                </select>
            </div>

            {/* ============ TABLE / EMPTY STATE ============ */}
            {filtered.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    {policies.length === 0 ? (
                        <>
                            <ShieldOff className="w-14 h-14 text-disabled mx-auto mb-4" />
                            <h3 className="text-lg font-semibold text-secondary mb-2">No Semantic Policies</h3>
                            <p className="text-sm text-muted max-w-md mx-auto mb-6">
                                Create your first semantic policy to govern by intent — block PII
                                exfiltration, prompt injection, or off-policy requests with a plain-English rule.
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
                            <p className="text-sm text-muted">No policies match the current filters.</p>
                        </>
                    )}
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
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Judge</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Groups</th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>Actions</th>
                            </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                            {filtered.map((p) => (
                                <tr key={p.id} className="hover:bg-surface-alt transition-colors">
                                    {/* Policy */}
                                    <td className="px-6 py-4">
                                        <div className="flex items-center space-x-3">
                                            <div className={`p-1.5 rounded-lg flex-shrink-0 ${p.enabled ? 'bg-primary-100' : 'bg-surface-raised'}`}>
                                                {p.enabled ? (
                                                    <ScrollText className="w-4 h-4 text-primary-600" />
                                                ) : (
                                                    <ShieldOff className="w-4 h-4 text-muted" />
                                                )}
                                            </div>
                                            <div className="min-w-0">
                                                <div className="text-sm font-semibold text-default truncate">{p.name}</div>
                                                {p.description && (
                                                    <p className="text-xs text-muted mt-0.5 truncate max-w-[260px]">{p.description}</p>
                                                )}
                                            </div>
                                        </div>
                                    </td>

                                    {/* Constraint */}
                                    <td className="px-6 py-4">
                                        <p className="text-xs text-muted max-w-[320px] truncate" title={p.constraint_text}>
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

                                    {/* Mode (click to toggle) */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <button
                                            onClick={asyncHandler(() => toggleMode(p), 'click')}
                                            title="Click to toggle shadow / enforce"
                                            className={`inline-flex items-center px-2 py-0.5 text-xs font-semibold rounded-full transition-colors ${
                                                p.mode === 'enforce'
                                                    ? 'bg-red-100 text-red-700 hover:bg-red-200'
                                                    : 'bg-amber-100 text-amber-700 hover:bg-amber-200'
                                            }`}
                                        >
                                            {p.mode}
                                        </button>
                                    </td>

                                    {/* Action */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs text-secondary">{ACTION_LABEL[p.action] ?? p.action}</span>
                                    </td>

                                    {/* Judge */}
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <span className="text-xs text-secondary">
                                            {p.judge_model_id ? (judgeNames[p.judge_model_id] || p.judge_model_id) : (
                                                <span className="text-muted italic">Tenant default</span>
                                            )}
                                        </span>
                                    </td>

                                    {/* Groups */}
                                    <td className="px-6 py-4">{renderGroups(p.resource_group_ids)}</td>

                                    {/* Actions */}
                                    <td className="px-6 py-4 whitespace-nowrap text-right">
                                        <div className="flex items-center justify-end space-x-1">
                                            <button
                                                onClick={() => { setEditing(p); setFormOpen(true); }}
                                                className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                title="Edit policy"
                                            >
                                                <Edit className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => { setDeleteTarget(p); setDeleteError(null); }}
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
                <SemanticPolicyForm
                    policy={editing}
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
                    itemType="semantic-policy"
                    isDeleting={isDeleting}
                    errorMessage={deleteError}
                />
            )}
        </div>
    );
};

export default SemanticPoliciesTab;
