/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Semantic Policy create / edit form. 5xl × 90vh modal with a left
 * sidebar nav (General / Access Control / Surfaces), mirroring
 * ArgumentPolicyForm / GuardrailForm so the governance forms share one
 * visual vocabulary.
 *
 * Group binding lives on the Access Control tab via the shared
 * HierarchicalGroupSelector — the Resource Group view is read-only.
 */

import React, { useEffect, useState } from 'react';
import {
    AlertCircle,
    FileText,
    Info,
    Layers,
    Loader2,
    Save,
    ScrollText,
    Users,
    X,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import HierarchicalGroupSelector from '../common/HierarchicalGroupSelector';
import { GroupHierarchyTree } from '../../lib/groupTypes';
import type {
    PolicyAction,
    PolicyMode,
    SemanticPolicy,
} from '../../lib/semanticPolicyTypes';
import { SURFACE_LABEL, SURFACES } from '../../lib/semanticPolicyTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

type TabId = 'general' | 'access' | 'surfaces';

interface Props {
    policy: SemanticPolicy | null;
    onClose: (saved: boolean) => void;
}

// Minimal shape of an /v1/admin/llms row used to build the judge picker.
interface JudgeModelRow {
    id: string;
    name?: string;
    model_name?: string;
    provider?: string;
    enabled?: boolean;
}

const SemanticPolicyForm: React.FC<Props> = ({ policy, onClose }) => {
    const gateway = useGateway();
    const toast = useToast();

    const isCreate = policy === null;

    const [activeTab, setActiveTab] = useState<TabId>('general');
    const [saving, setSaving] = useState(false);
    const [serverError, setServerError] = useState<string | null>(null);

    // ── General state ────────────────────────────────────────────────
    const [name, setName] = useState(policy?.name ?? '');
    const [description, setDescription] = useState(policy?.description ?? '');
    const [constraintText, setConstraintText] = useState(policy?.constraint_text ?? '');
    const [mode, setMode] = useState<PolicyMode>(policy?.mode ?? 'shadow');
    const [action, setAction] = useState<PolicyAction>(policy?.action ?? 'block');
    const [judgeModelId, setJudgeModelId] = useState(policy?.judge_model_id ?? '');
    const [enabled, setEnabled] = useState(policy?.enabled ?? true);

    // ── Surfaces state ───────────────────────────────────────────────
    const [surfaces, setSurfaces] = useState<string[]>(policy?.surfaces ?? []);

    // ── Access Control state ─────────────────────────────────────────
    const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>(
        policy?.resource_group_ids ?? [],
    );
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [groupsLoading, setGroupsLoading] = useState(false);

    // ── Judge model picker ───────────────────────────────────────────
    const [judgeModels, setJudgeModels] = useState<{ id: string; label: string }[]>([]);
    const [readFailure, reads] = useReadFailures<'groups' | 'judges'>();
    const [groupsAsked, setGroupsAsked] = useState(0);
    const [judgesAsked, setJudgesAsked] = useState(0);

    // Load the Resource Group tree (mirrors ArgumentPolicyForm). Run-once.
    useEffect(() => {
        const fetchGroups = async () => {
            if (!gateway?.gatewayClient) return;
            setGroupsLoading(true);
            try {
                const response = await gateway.gatewayClient.fetchGroupHierarchyTree();
                setGroupTree(response.tree || []);
                reads.loaded('groups');
            } catch (err) {
                reads.failed('groups', 'Failed to load the resource groups', err);
            } finally {
                setGroupsLoading(false);
            }
        };
        runBackground(fetchGroups(), 'groups');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [groupsAsked]);

    // Judge candidates = enabled chat models (empty → tenant default only).
    useEffect(() => {
        const fetchJudges = async () => {
            if (!gateway?.gatewayClient) return;
            try {
                const resp: { models?: Record<string, JudgeModelRow> | JudgeModelRow[] } =
                    await gateway.gatewayClient.listLLMs({ mode: 'chat', enabled: true });
                const raw: unknown = resp?.models ?? resp;
                const list: JudgeModelRow[] = Array.isArray(raw)
                    ? (raw as JudgeModelRow[])
                    : raw && typeof raw === 'object'
                      ? Object.values(raw as Record<string, JudgeModelRow>)
                      : [];
                setJudgeModels(
                    list
                        .filter((m) => m && m.enabled !== false)
                        .map((m) => ({
                            id: m.id,
                            label: `${m.name || m.model_name}${m.provider ? ` (${m.provider})` : ''}`,
                        })),
                );
                reads.loaded('judges');
            } catch (err) {
                // The picker offers the tenant default only, and says why.
                reads.failed('judges', 'Failed to load the judge models; only the tenant default can be chosen', err);
            }
        };
        runBackground(fetchJudges(), 'judges');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [judgesAsked]);

    const toggleSurface = (s: string) =>
        setSurfaces((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setServerError(null);

        if (!name.trim()) {
            setActiveTab('general');
            toast.error('Validation', 'Name is required');
            return;
        }
        if (!constraintText.trim()) {
            setActiveTab('general');
            toast.error('Validation', 'A natural-language constraint is required');
            return;
        }

        const body = {
            name: name.trim(),
            description: description.trim() || null,
            constraint_text: constraintText.trim(),
            action,
            mode,
            surfaces: surfaces.length ? surfaces : null,
            judge_model_id: judgeModelId || null,
            resource_group_ids: selectedGroupIds,
        };

        setSaving(true);
        try {
            if (isCreate) {
                await gateway.gatewayClient.createSemanticPolicy(body);
                toast.success('Policy Created', `"${name.trim()}" has been created.`);
            } else {
                await gateway.gatewayClient.updateSemanticPolicy(policy.id, { ...body, enabled });
                toast.success('Policy Updated', `"${name.trim()}" has been saved.`);
            }
            onClose(true);
        } catch (err) {
            const msg = errorMessage(err, 'Failed to save semantic policy');
            setServerError(msg);
            toast.error('Save Failed', msg);
        } finally {
            setSaving(false);
        }
    };

    const NavItem: React.FC<{ id: TabId; label: string; icon: React.ReactNode }> = ({
        id, label, icon,
    }) => (
        <button
            type="button"
            onClick={() => setActiveTab(id)}
            className={`flex items-center space-x-2.5 w-full px-3 py-2.5 text-sm font-medium rounded-lg transition-colors text-left ${
                activeTab === id
                    ? 'bg-primary-50 text-primary-700 border-l-3 border-primary-500'
                    : 'text-secondary hover:text-default hover:bg-surface-alt'
            }`}
        >
            {icon}
            <span>{label}</span>
        </button>
    );

    const inputCls =
        'w-full px-3 py-2 text-sm border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary-500';

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-5xl h-[90vh] flex flex-col">

                {/* ─── Header ─── */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default flex-shrink-0">
                    <div className="flex items-center space-x-3">
                        <ScrollText className="w-6 h-6 text-primary-600" />
                        <h2 className="text-xl font-semibold text-default">
                            {isCreate ? 'New Semantic Policy' : `Edit Semantic Policy — ${policy.name}`}
                        </h2>
                    </div>
                    <button
                        onClick={() => onClose(false)}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* ─── Sidebar + Content ─── */}
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="flex-1 flex overflow-hidden min-h-0 min-w-0">

                    {/* Left Sidebar */}
                    <nav className="w-48 flex-shrink-0 border-r border-default bg-surface-alt py-3 px-2 space-y-0.5 overflow-y-auto">
                        <NavItem id="general"  label="General"        icon={<FileText className="w-4 h-4" />} />
                        <NavItem id="access"   label="Access Control" icon={<Users className="w-4 h-4" />} />
                        <NavItem id="surfaces" label="Surfaces"       icon={<Layers className="w-4 h-4" />} />
                    </nav>

                    {/* Right Content */}
                    <div className="scroll-region">
                        <div className="p-6">

                            {serverError && (
                                <div className="mb-6 bg-red-50 border border-red-300 rounded-lg p-4 flex items-start space-x-3">
                                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                                    <div className="flex-1 min-h-0 min-w-0">
                                        <p className="text-sm font-medium text-red-800 mb-1">
                                            {isCreate ? 'Failed to create policy' : 'Failed to update policy'}
                                        </p>
                                        <pre className="text-sm text-red-700 whitespace-pre-wrap font-mono">{serverError}</pre>
                                    </div>
                                    <button type="button" onClick={() => setServerError(null)} className="text-red-400 hover:text-red-600">
                                        <X className="w-4 h-4" />
                                    </button>
                                </div>
                            )}

                            {/* ─── GENERAL TAB ─── */}
                            {activeTab === 'general' && (
                                <div className="space-y-4">
                                    {/* Name */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Name <span className="text-red-500">*</span>
                                        </label>
                                        <input
                                            type="text"
                                            value={name}
                                            onChange={(e) => setName(e.target.value)}
                                            placeholder="e.g. no-pii-egress"
                                            maxLength={255}
                                            required
                                            className={inputCls}
                                        />
                                    </div>

                                    {/* Description */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Description
                                        </label>
                                        <input
                                            type="text"
                                            value={description ?? ''}
                                            onChange={(e) => setDescription(e.target.value)}
                                            placeholder="Stops requests that exfiltrate customer PII to external peers"
                                            maxLength={1024}
                                            className={inputCls}
                                        />
                                    </div>

                                    {/* Constraint */}
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Constraint (natural language) <span className="text-red-500">*</span>
                                        </label>
                                        <textarea
                                            value={constraintText}
                                            onChange={(e) => setConstraintText(e.target.value)}
                                            rows={4}
                                            required
                                            placeholder="The request must NOT ask the model to extract or send customers' personal data to an external destination."
                                            className={inputCls}
                                        />
                                        <p className="mt-1 text-xs text-muted">
                                            An LLM judge evaluates each governed request against this constraint and
                                            returns a violated / not-violated verdict. Write it as a clear,
                                            single-intent rule.
                                        </p>
                                    </div>

                                    {/* Mode + Action */}
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Mode
                                            </label>
                                            <select
                                                value={mode}
                                                onChange={(e) => setMode(e.target.value as PolicyMode)}
                                                className={`${inputCls} bg-surface`}
                                            >
                                                <option value="shadow">shadow (log would-blocks)</option>
                                                <option value="enforce">enforce</option>
                                            </select>
                                            <p className="mt-1 text-xs text-muted">
                                                Ship in <strong>shadow</strong> to measure judge accuracy, then flip to{' '}
                                                <strong>enforce</strong>.
                                            </p>
                                        </div>
                                        <div>
                                            <label className="block text-sm font-medium text-secondary mb-1">
                                                Action (when enforced)
                                            </label>
                                            <select
                                                value={action}
                                                onChange={(e) => setAction(e.target.value as PolicyAction)}
                                                className={`${inputCls} bg-surface`}
                                            >
                                                <option value="block">block (reject with 403)</option>
                                                <option value="warn">warn (log-only)</option>
                                            </select>
                                        </div>
                                    </div>

                                    {/* Judge model */}
                                    <div>
                                        <ReadFailureNotice compact failure={readFailure('judges')} onRetry={() => setJudgesAsked((n) => n + 1)} />
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Judge model
                                        </label>
                                        <select
                                            value={judgeModelId ?? ''}
                                            onChange={(e) => setJudgeModelId(e.target.value)}
                                            className={`${inputCls} bg-surface`}
                                        >
                                            <option value="">— Tenant default judge model —</option>
                                            {judgeModels.map((m) => (
                                                <option key={m.id} value={m.id}>{m.label}</option>
                                            ))}
                                            {judgeModelId && !judgeModels.some((m) => m.id === judgeModelId) && (
                                                <option value={judgeModelId}>{judgeModelId} (unavailable)</option>
                                            )}
                                        </select>
                                        <p className="mt-1 text-xs text-muted">
                                            Optional. Pin a cheaper/faster chat model for the judge call; defaults to the
                                            tenant model. The judge call is itself governed, logged, and billed.
                                        </p>
                                    </div>

                                    {/* Enabled */}
                                    <div className="pt-2">
                                        {/* The create endpoint always starts a policy enabled. */}
                                        {!isCreate && (
                                            <label className="flex items-center space-x-2 text-sm">
                                                <input
                                                    type="checkbox"
                                                    checked={enabled}
                                                    onChange={(e) => setEnabled(e.target.checked)}
                                                    className="w-4 h-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                                                />
                                                <span className="font-medium text-secondary">Enabled</span>
                                            </label>
                                        )}
                                    </div>
                                </div>
                            )}

                            {/* ─── ACCESS CONTROL TAB ─── */}
                            {activeTab === 'access' && (
                                <div className="space-y-4">
                                    <div className="bg-primary-50 border border-primary-200 rounded-lg p-4 flex items-start space-x-3">
                                        <Info className="w-5 h-5 text-primary-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 min-h-0 min-w-0">
                                            <p className="text-sm text-primary-900">
                                                <strong>Control which Resource Groups this semantic policy applies to.</strong>{' '}
                                                A policy bound to a group runs on every request that group serves; bindings
                                                are additive down the tree. With <em>no</em> bindings the policy is{' '}
                                                <strong>tenant-wide</strong> (applies to every group).
                                            </p>
                                        </div>
                                    </div>

                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-2">
                                            Resource Groups
                                        </label>
                                        {groupsLoading ? (
                                            <div className="text-sm text-muted py-4 text-center">Loading groups…</div>
                                        ) : readFailure('groups') && groupTree.length === 0 ? (
                                            <ReadFailureNotice compact failure={readFailure('groups')} onRetry={() => setGroupsAsked((n) => n + 1)} />
                                        ) : groupTree.length === 0 ? (
                                            <div className="text-sm text-muted py-4 px-3 bg-surface-alt rounded-lg border border-default">
                                                <Info className="w-4 h-4 inline mr-1" />
                                                No Resource Groups defined yet — the policy will apply tenant-wide.
                                            </div>
                                        ) : (
                                            <>
                                                <HierarchicalGroupSelector
                                                    groupTree={groupTree}
                                                    selectedGroupIds={selectedGroupIds}
                                                    onSelectionChange={setSelectedGroupIds}
                                                />
                                                <div className="mt-2 text-sm text-secondary flex items-center">
                                                    <Users className="w-4 h-4 mr-1" />
                                                    <span>
                                                        {selectedGroupIds.length === 0
                                                            ? 'No groups selected — applies tenant-wide'
                                                            : `${selectedGroupIds.length} group${selectedGroupIds.length !== 1 ? 's' : ''} selected`}
                                                    </span>
                                                </div>
                                            </>
                                        )}
                                    </div>
                                </div>
                            )}

                            {/* ─── SURFACES TAB ─── */}
                            {activeTab === 'surfaces' && (
                                <div className="space-y-4">
                                    <div className="bg-primary-50 border border-primary-200 rounded-lg p-4 flex items-start space-x-3">
                                        <Info className="w-5 h-5 text-primary-600 flex-shrink-0 mt-0.5" />
                                        <div className="flex-1 text-sm text-primary-900 min-h-0 min-w-0">
                                            <strong>Surfaces are the agent-action points where the judge runs.</strong>{' '}
                                            Select none to apply on <strong>every</strong> surface, or scope to specific
                                            ones to bound cost/latency (each applicable policy is one judge call per
                                            request).
                                        </div>
                                    </div>

                                    <div className="space-y-2">
                                        {SURFACES.map((s) => (
                                            <label
                                                key={s}
                                                className="flex items-center space-x-3 p-3 border border-default rounded-lg bg-surface-alt cursor-pointer hover:bg-surface-raised transition-colors"
                                            >
                                                <input
                                                    type="checkbox"
                                                    checked={surfaces.includes(s)}
                                                    onChange={() => toggleSurface(s)}
                                                    className="w-4 h-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                                                />
                                                <span className="text-sm font-medium text-default">{SURFACE_LABEL[s]}</span>
                                                <span className="text-xs text-muted font-mono">{s}</span>
                                            </label>
                                        ))}
                                    </div>
                                    <p className="text-xs text-muted">
                                        {surfaces.length === 0
                                            ? 'No surfaces selected — the policy applies to all surfaces.'
                                            : `${surfaces.length} surface${surfaces.length !== 1 ? 's' : ''} selected.`}
                                    </p>
                                </div>
                            )}

                        </div>
                    </div>
                </form>

                {/* ─── Footer ─── */}
                <div className="px-6 py-4 border-t border-default flex items-center justify-end space-x-3 flex-shrink-0">
                    <button
                        type="button"
                        onClick={() => onClose(false)}
                        disabled={saving}
                        className="px-4 py-2 text-sm font-medium text-secondary bg-surface-raised hover:bg-surface-alt rounded-lg transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={saving}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                    >
                        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                        <span>{saving ? 'Saving…' : isCreate ? 'Create Policy' : 'Save Changes'}</span>
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SemanticPolicyForm;
