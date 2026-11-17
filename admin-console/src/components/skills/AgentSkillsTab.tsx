/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import toast from 'react-hot-toast';
import {
    RefreshCw,
    AlertCircle,
    Plus,
    Upload,
    LayoutGrid,
    List,
    Search,
    X,
    ChevronDown,
    Sparkles,
    Loader2,
    CheckSquare,
    Trash2
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useLocalStorageString } from '../../hooks/useLocalStorage';
import {
    AgentSkillConfig,
    AgentSkillLimits,
    AgentSkillUpdateRequest,
    SkillStatus,
    SKILL_STATUS_CONFIG
} from '../../lib/agentSkillTypes';
import AgentSkillCard from './AgentSkillCard';
import AgentSkillListItem from './AgentSkillListItem';
import AgentSkillDetailPanel from './AgentSkillDetailPanel';
import AgentSkillImportModal from './AgentSkillImportModal';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import { failureSentence, errorKind, errorMessage } from '../../lib/narrow';
import { saveFailure } from '../../lib/validationErrors';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

type SkillFormData = Partial<AgentSkillConfig> & {
    group_ids?: string[];
};

// Empty AgentSkillConfig stub used to open the unified detail panel in
// "Create" mode. The panel detects `!id` to switch off lifecycle buttons
// + post-create-only tabs (Resources, Scripts, Assets, etc.).
const newSkillStub = (): AgentSkillConfig => ({
    id: '',
    tenant_id: '',
    name: '',
    description: '',
    version: '1.0.0',
    status: 'draft',
    enabled: true,
    skill_md_hash: '',
    execution_count: 0,
    is_global: false,
    group_ids: [],
});

const AgentSkillsTab: React.FC = () => {
    const {
        gatewayClient,
        fetchGroupHierarchyTree,
        listSkillResources, getSkillResource, createSkillResource, updateSkillResource, deleteSkillResource,
        listSkillScripts, getSkillScript, createSkillScript, updateSkillScript, deleteSkillScript,
        listSkillAssets, getSkillAsset, createSkillAsset, updateSkillAsset, deleteSkillAsset,
    } = useGateway();

    // Data state
    const [skills, setSkills] = useState<Record<string, AgentSkillConfig>>({});
    const [loading, setLoading] = useState(true);

    // UI state
    const [layout, setLayout] = useLocalStorageString('agent-skills-view-layout', 'card') as ['card' | 'list', (v: 'card' | 'list') => void];
    const [searchTerm, setSearchTerm] = useState('');
    const [statusFilter, setStatusFilter] = useState<SkillStatus | ''>('');
    const [categoryFilter, setCategoryFilter] = useState('');

    // Modal/panel state. The detail panel handles create + edit + view
    // via stub-skill detection.
    const [isImporting, setIsImporting] = useState(false);
    const [detailSkill, setDetailSkill] = useState<AgentSkillConfig | null>(null);
    const [detailTab, setDetailTab] = useState<'overview' | 'groups' | 'content' | 'resources' | 'scripts' | 'assets' | 'executions'>('overview');

    const openDetail = (skill: AgentSkillConfig, tab: 'overview' | 'groups' | 'content' | 'resources' | 'scripts' | 'assets' | 'executions' = 'overview') => {
        setDetailSkill(skill);
        setDetailTab(tab);
    };

    // Delete confirmation
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        skillId: string;
        skillName: string;
        skillInfo: AgentSkillConfig;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // Bulk selection / delete
    const [selectionMode, setSelectionMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
    const [isBulkDeleting, setIsBulkDeleting] = useState(false);
    const [bulkError, setBulkError] = useState<string | null>(null);
    // The size limits an import is held to, as the list of skills states them.
    const [importLimits, setImportLimits] = useState<AgentSkillLimits | null>(null);

    // ========================================================================
    // Data Loading
    // ========================================================================

    const loadSkills = async () => {
        try {
            setLoading(true);
            const response = await gatewayClient.listAgentSkills({ include_content: true });
            setSkills(response.skills || {});
            setImportLimits(response.limits ?? null);
        } catch (error) {
            console.error('Failed to load agent skills:', errorKind(error));
            toast.error(failureSentence('Failed to load agent skills', error));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        runBackground(loadSkills(), 'skills');
    }, []);

    // ========================================================================
    // CRUD Operations
    // ========================================================================

    /**
     * Surface the backend's `auto_bound_server_group_ids` side effect
     * as a toast. Called after every create / update / group-edit
     * mutation. The backend auto-binds the system Agent Skill MCP
     * server to any group that lacks it whenever a skill is assigned;
     * this notice keeps that magic transparent so the admin sees
     * "Agent Skills" appear in those groups' MCP Servers tab and
     * knows why.
     *
     * No-op when nothing was auto-bound (groups already had the
     * server, or the call didn't touch group_ids).
     */
    const showAutoBindNotice = (groupIds?: string[] | null) => {
        if (!groupIds || groupIds.length === 0) return;
        const count = groupIds.length;
        const groupWord = count === 1 ? 'group' : 'groups';
        toast.success(
            `Agent Skills execution server added to ${count} ${groupWord} so the skill can run.`,
            { duration: 6000 },
        );
    };

    const handleCreateSkill = async (data: SkillFormData): Promise<string | undefined> => {
        try {
            if (!data.name || !data.description || !data.skill_md_content) {
                toast.error('Missing required fields');
                return undefined;
            }

            const response = await gatewayClient.createAgentSkill({
                name: data.name,
                description: data.description,
                skill_md_content: data.skill_md_content,
                version: data.version || '1.0.0',
                license: data.license,
                compatibility: data.compatibility,
                allowed_tools: data.allowed_tools,
                disclosure_mode: data.disclosure_mode,
                metadata: data.metadata,
                enabled: data.enabled ?? true,
                group_ids: data.group_ids,
            });

            toast.success(`Skill "${data.name}" created`);
            // Surface the auto-bind side effect (see backend
            // `_ensure_agent_skill_server_bound`) so the admin
            // knows the system MCP server was added to the
            // group(s) automatically. Without this they wonder
            // why the server appears in MCP Servers after a
            // skill create.
            showAutoBindNotice(response.auto_bound_server_group_ids);
            // Don't close or reload here — the panel chains
            // POSTs for any pending Resources/Scripts/Assets
            // using the returned id, then calls onClose+onRefresh.
            return response.skill.id;
        } catch (error) {
            throw saveFailure(error, 'Failed to create skill');
        }
    };

    const handleUpdateSkill = async (skillId: string, data: SkillFormData) => {
        try {
            // Anything not listed here never reaches the server, and the panel
            // still says "saved": Limits sends only its own key, so dropping it
            // sent an empty PATCH with a success toast. A skill's own policy
            // (`tenant_governance`) is not available in this release and is
            // never sent.
            const update: AgentSkillUpdateRequest & Pick<
                SkillFormData,
                'name' | 'version' | 'tenant_limits'
            > = {
                name: data.name,
                version: data.version,
                description: data.description,
                skill_md_content: data.skill_md_content,
                license: data.license,
                compatibility: data.compatibility,
                allowed_tools: data.allowed_tools,
                disclosure_mode: data.disclosure_mode,
                metadata: data.metadata,
                enabled: data.enabled,
                tenant_limits: data.tenant_limits,
                group_ids: data.group_ids,
            };
            const response = await gatewayClient.updateAgentSkill(skillId, update);

            toast.success('Skill updated');
            // Same auto-bind notice as create — see comment there.
            showAutoBindNotice(response.auto_bound_server_group_ids);
            await loadSkills();
        } catch (error) {
            throw saveFailure(error, 'Failed to update skill');
        }
    };

    const handleDeleteSkill = (skillId: string) => {
        const skill = skills[skillId];
        if (skill) {
            setDeleteConfirmation({
                skillId,
                skillName: skill.name,
                skillInfo: skill
            });
        }
    };

    const confirmDeleteSkill = async () => {
        if (!deleteConfirmation) return;
        try {
            setIsDeleting(true);
            setDeleteError(null);
            await gatewayClient.deleteAgentSkill(deleteConfirmation.skillId);
            toast.success(`Skill "${deleteConfirmation.skillName}" deleted`);
            setDeleteConfirmation(null);
            if (detailSkill?.id === deleteConfirmation.skillId) setDetailSkill(null);
            await loadSkills();
        } catch (error) {
            const msg = errorMessage(error, 'Failed to delete skill');
            setDeleteError(msg);
            toast.error(msg);
        } finally {
            setIsDeleting(false);
        }
    };

    // ========================================================================
    // Bulk Selection / Delete
    // ========================================================================

    const toggleSelect = (skillId: string) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(skillId)) next.delete(skillId); else next.add(skillId);
            return next;
        });
    };

    const exitSelectionMode = () => {
        setSelectionMode(false);
        setSelectedIds(new Set());
        setBulkError(null);
    };

    const confirmBulkDelete = async () => {
        const ids = Array.from(selectedIds);
        if (ids.length === 0) return;
        try {
            setIsBulkDeleting(true);
            setBulkError(null);
            const result = await gatewayClient.bulkDeleteAgentSkills(ids);
            if (result.deleted_count > 0) {
                toast.success(
                    `Deleted ${result.deleted_count} skill${result.deleted_count !== 1 ? 's' : ''}`,
                );
            }
            if (result.skipped_count > 0) {
                // Published skills (and any not-found) come back here. Make
                // the partial outcome explicit rather than silently dropping.
                toast.error(
                    `Skipped ${result.skipped_count} skill${result.skipped_count !== 1 ? 's' : ''} `
                    + `(published skills must be deprecated then archived first)`,
                    { duration: 7000 },
                );
            }
            setBulkConfirmOpen(false);
            exitSelectionMode();
            await loadSkills();
        } catch (error) {
            const msg = errorMessage(error, 'Bulk delete failed');
            setBulkError(msg);
            toast.error(msg);
        } finally {
            setIsBulkDeleting(false);
        }
    };

    // ========================================================================
    // Lifecycle Operations
    // ========================================================================

    const handleValidate = async (skillId: string) => {
        try {
            const result = await gatewayClient.validateAgentSkill(skillId);
            if (result.valid) {
                toast.success('Skill validated successfully');
            } else {
                toast.error(`Validation failed: ${result.errors.length} error(s)`);
            }
            await loadSkills();
        } catch (error) {
            toast.error(errorMessage(error, 'Validation failed'));
        }
    };

    const handlePublish = async (skillId: string) => {
        try {
            await gatewayClient.publishAgentSkill(skillId);
            toast.success('Skill published! It is now immutable and available for execution.');
            await loadSkills();
        } catch (error) {
            toast.error(errorMessage(error, 'Failed to publish skill'));
        }
    };

    const handleDeprecate = async (skillId: string, reason: string) => {
        try {
            await gatewayClient.deprecateAgentSkill(skillId, reason);
            toast.success('Skill deprecated');
            await loadSkills();
        } catch (error) {
            toast.error(errorMessage(error, 'Failed to deprecate skill'));
        }
    };

    const handleArchive = async (skillId: string) => {
        try {
            await gatewayClient.archiveAgentSkill(skillId);
            toast.success('Skill archived');
            await loadSkills();
        } catch (error) {
            toast.error(errorMessage(error, 'Failed to archive skill'));
        }
    };

    const handleCopySkill = (source: AgentSkillConfig) => {
        // Open the panel in Create mode pre-filled with the source's
        // editable fields. `id: ''` flips the panel into create mode;
        // status is reset to draft because a copy is a new draft. We
        // strip server-managed fields (audit timestamps, validation
        // state, execution metrics) so they don't accidentally
        // round-trip on save.
        setDetailSkill({
            ...newSkillStub(),
            name: `${source.name}-copy`,
            description: source.description,
            version: source.version,
            license: source.license,
            compatibility: source.compatibility,
            allowed_tools: source.allowed_tools,
            metadata: source.metadata,
            skill_md_content: source.skill_md_content,
        });
        setDetailTab('overview');
    };

    // ========================================================================
    // Filtering
    // ========================================================================

    const allSkills = Object.values(skills);

    const filteredSkills = allSkills.filter(skill => {
        if (searchTerm) {
            const term = searchTerm.toLowerCase();
            const matchesName = skill.name.toLowerCase().includes(term);
            const matchesDesc = skill.description?.toLowerCase().includes(term);
            const matchesTags = skill.metadata?.tags?.some(t => t.toLowerCase().includes(term));
            if (!matchesName && !matchesDesc && !matchesTags) return false;
        }
        if (statusFilter && skill.status !== statusFilter) return false;
        if (categoryFilter) {
            const skillCategory = skill.metadata?.category || 'Other';
            if (skillCategory !== categoryFilter) return false;
        }
        return true;
    });

    // Sort: enabled first, then by name
    const sortedSkills = [...filteredSkills].sort((a, b) => {
        if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
        return a.name.localeCompare(b.name);
    });

    // Status counts for filter badges
    const statusCounts = allSkills.reduce((acc, s) => {
        acc[s.status] = (acc[s.status] || 0) + 1;
        return acc;
    }, {} as Record<string, number>);

    // Unique categories
    const categories = [...new Set(allSkills.map(s => s.metadata?.category || 'Other'))].sort();

    // ========================================================================
    // Render
    // ========================================================================

    return (
        <div className="app-column h-full bg-surface-alt">
            {/* Header */}
            <div className="bg-surface border-b border-default px-6 py-4">
                <div className="flex items-center justify-between mb-4">
                    <div>
                        <h1 className="text-xl font-semibold text-default flex items-center space-x-2">
                            <Sparkles className="w-6 h-6 text-primary-600" />
                            <span>Agent Skills</span>
                        </h1>
                        <p className="text-sm text-muted mt-0.5">
                            {allSkills.length} skill{allSkills.length !== 1 ? 's' : ''} total
                            {statusCounts.published ? ` • ${statusCounts.published} published` : ''}
                            {statusCounts.draft ? ` • ${statusCounts.draft} drafts` : ''}
                        </p>
                    </div>
                    <div className="flex items-center space-x-3">
                        <button
                            onClick={asyncHandler(loadSkills, 'click')}
                            disabled={loading}
                            className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                            title="Refresh"
                        >
                            <RefreshCw className={`w-5 h-5 ${loading ? 'animate-spin' : ''}`} />
                        </button>
                        <button
                            onClick={() => (selectionMode ? exitSelectionMode() : setSelectionMode(true))}
                            disabled={loading || allSkills.length === 0}
                            className={`px-4 py-2 border rounded-lg transition-colors flex items-center space-x-2 disabled:opacity-50 ${
                                selectionMode
                                    ? 'bg-primary-50 text-primary-700 border-primary-300'
                                    : 'bg-surface text-secondary border-strong hover:bg-surface-alt'
                            }`}
                            title={selectionMode ? 'Exit selection mode' : 'Select skills to bulk-delete'}
                        >
                            <CheckSquare className="w-4 h-4" />
                            <span className="text-sm font-medium">{selectionMode ? 'Cancel' : 'Select'}</span>
                        </button>
                        <button
                            onClick={() => setIsImporting(true)}
                            className="px-4 py-2 bg-surface text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors flex items-center space-x-2"
                        >
                            <Upload className="w-4 h-4" />
                            <span className="text-sm font-medium">Import</span>
                        </button>
                        <button
                            onClick={() => { setDetailSkill(newSkillStub()); setDetailTab('overview'); }}
                            className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2"
                        >
                            <Plus className="w-4 h-4" />
                            <span className="text-sm font-medium">New Skill</span>
                        </button>
                    </div>
                </div>

                {/* Search + Filters Bar */}
                <div className="flex items-center space-x-3">
                    {/* Search */}
                    <div className="relative flex-1 max-w-md min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search skills by name, description, tags..."
                            className="w-full pl-10 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent text-sm"
                        />
                        {searchTerm && (
                            <button
                                onClick={() => setSearchTerm('')}
                                className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary"
                            >
                                <X className="w-4 h-4" />
                            </button>
                        )}
                    </div>

                    {/* Status filter */}
                    <div className="relative">
                        <select
                            value={statusFilter}
                            onChange={(e) => setStatusFilter(e.target.value as SkillStatus | '')}
                            className="appearance-none pl-3 pr-8 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 text-sm bg-surface"
                        >
                            <option value="">All Statuses</option>
                            {Object.entries(SKILL_STATUS_CONFIG).map(([key, cfg]) => (
                                <option key={key} value={key}>
                                    {cfg.label} ({statusCounts[key] || 0})
                                </option>
                            ))}
                        </select>
                        <ChevronDown className="absolute right-2 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted pointer-events-none" />
                    </div>

                    {/* Category filter */}
                    {categories.length > 1 && (
                        <div className="relative">
                            <select
                                value={categoryFilter}
                                onChange={(e) => setCategoryFilter(e.target.value)}
                                className="appearance-none pl-3 pr-8 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 text-sm bg-surface"
                            >
                                <option value="">All Categories</option>
                                {categories.map(c => (
                                    <option key={c} value={c}>{c}</option>
                                ))}
                            </select>
                            <ChevronDown className="absolute right-2 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted pointer-events-none" />
                        </div>
                    )}

                    {/* Layout toggle */}
                    <div className="flex items-center bg-surface-raised rounded-lg p-0.5">
                        <button
                            onClick={() => setLayout('card')}
                            className={`p-2 rounded transition-colors ${
                                layout === 'card'
                                    ? 'bg-surface text-primary-600 shadow-sm'
                                    : 'text-secondary hover:text-default'
                            }`}
                            title="Card view"
                        >
                            <LayoutGrid className="w-4 h-4" />
                        </button>
                        <button
                            onClick={() => setLayout('list')}
                            className={`p-2 rounded transition-colors ${
                                layout === 'list'
                                    ? 'bg-surface text-primary-600 shadow-sm'
                                    : 'text-secondary hover:text-default'
                            }`}
                            title="List view"
                        >
                            <List className="w-4 h-4" />
                        </button>
                    </div>
                </div>
            </div>

            {/* Bulk-selection action bar */}
            {selectionMode && (
                <div className="bg-primary-50 border-b border-primary-200 px-6 py-2.5 flex items-center justify-between">
                    <div className="flex items-center space-x-4">
                        <label className="flex items-center space-x-2 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                checked={sortedSkills.length > 0 && sortedSkills.every(s => selectedIds.has(s.id))}
                                ref={(el) => {
                                    if (el) {
                                        const some = sortedSkills.some(s => selectedIds.has(s.id));
                                        const all = sortedSkills.length > 0 && sortedSkills.every(s => selectedIds.has(s.id));
                                        el.indeterminate = some && !all;
                                    }
                                }}
                                onChange={(e) => {
                                    if (e.target.checked) {
                                        setSelectedIds(new Set(sortedSkills.map(s => s.id)));
                                    } else {
                                        setSelectedIds(new Set());
                                    }
                                }}
                                className="h-4 w-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                            />
                            <span className="text-sm font-medium text-primary-800">
                                {selectedIds.size > 0
                                    ? `${selectedIds.size} selected`
                                    : `Select all ${sortedSkills.length}`}
                            </span>
                        </label>
                        {selectedIds.size > 0 && (
                            <button
                                onClick={() => setSelectedIds(new Set())}
                                className="text-sm text-primary-700 hover:text-primary-900 underline"
                            >
                                Clear
                            </button>
                        )}
                    </div>
                    <button
                        onClick={() => { setBulkError(null); setBulkConfirmOpen(true); }}
                        disabled={selectedIds.size === 0}
                        className="px-4 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors flex items-center space-x-2 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                        <Trash2 className="w-4 h-4" />
                        <span className="text-sm font-medium">
                            Delete{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
                        </span>
                    </button>
                </div>
            )}

            {/* Content Area */}
            <div className="scroll-region p-6">
                {loading ? (
                    <div className="flex items-center justify-center h-64">
                        <div className="text-center">
                            <Loader2 className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                            <p className="text-sm text-secondary">Loading agent skills...</p>
                        </div>
                    </div>
                ) : sortedSkills.length === 0 ? (
                    <div className="flex items-center justify-center h-64">
                        <div className="text-center">
                            {allSkills.length === 0 ? (
                                <>
                                    <Sparkles className="w-16 h-16 text-disabled mx-auto mb-4" />
                                    <h3 className="text-lg font-semibold text-default mb-2">No Agent Skills Yet</h3>
                                    <p className="text-secondary mb-4">
                                        Create your first skill to define agent capabilities following the agentskills.io spec.
                                    </p>
                                    <button
                                        onClick={() => { setDetailSkill(newSkillStub()); setDetailTab('overview'); }}
                                        className="inline-flex items-center space-x-2 px-6 py-3 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors shadow-sm"
                                    >
                                        <Plus className="w-5 h-5" />
                                        <span>Create Your First Skill</span>
                                    </button>
                                </>
                            ) : (
                                <>
                                    <AlertCircle className="w-16 h-16 text-disabled mx-auto mb-4" />
                                    <h3 className="text-lg font-semibold text-default mb-2">No Matching Skills</h3>
                                    <p className="text-secondary">
                                        Try adjusting your search or filter criteria.
                                    </p>
                                </>
                            )}
                        </div>
                    </div>
                ) : layout === 'card' ? (
                    /* ============ CARD VIEW ============ */
                    <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-4">
                        {sortedSkills.map(skill => (
                            <AgentSkillCard
                                key={skill.id}
                                skill={skill}
                                onEdit={() => { setDetailSkill(skill); setDetailTab('overview'); }}
                                onDelete={() => handleDeleteSkill(skill.id)}
                                onCopy={() => handleCopySkill(skill)}
                                onOpenTab={(tab) => openDetail(skill, tab)}
                                onPublish={asyncHandler(() => handlePublish(skill.id), 'publish')}
                                onValidate={asyncHandler(() => handleValidate(skill.id), 'validate')}
                                onArchive={asyncHandler(() => handleArchive(skill.id), 'archive')}
                                selectable={selectionMode}
                                selected={selectedIds.has(skill.id)}
                                onToggleSelect={() => toggleSelect(skill.id)}
                            />
                        ))}
                    </div>
                ) : (
                    /* ============ LIST VIEW ============ */
                    <div className="bg-surface rounded-lg border border-default">
                        {/* Table Header */}
                        <div className={`${TABLE_TH_TYPE} hidden lg:flex items-center px-4 py-3 bg-surface-alt border-b border-default`}>
                            <div className="flex-1 min-w-0 min-h-0">Skill</div>
                            <div className="w-32 text-center flex-shrink-0">Category</div>
                            <div className="w-28 text-center flex-shrink-0">Status</div>
                            <div className="w-20 text-center flex-shrink-0">Executions</div>
                            <div className="w-20 text-center flex-shrink-0">Cost Est.</div>
                            <div className="w-16 text-center flex-shrink-0">Approval</div>
                            <div className="min-w-[200px] max-w-[280px] flex-shrink-0 mx-3">Access</div>
                            <div className="w-10 flex-shrink-0" />
                        </div>

                        {/* Table Body */}
                        {sortedSkills.map(skill => (
                            <AgentSkillListItem
                                key={skill.id}
                                skill={skill}
                                onEdit={() => { setDetailSkill(skill); setDetailTab('overview'); }}
                                onDelete={() => handleDeleteSkill(skill.id)}
                                onCopy={() => handleCopySkill(skill)}
                                onOpenTab={(tab) => openDetail(skill, tab)}
                                onPublish={asyncHandler(() => handlePublish(skill.id), 'publish')}
                                onValidate={asyncHandler(() => handleValidate(skill.id), 'validate')}
                                onArchive={asyncHandler(() => handleArchive(skill.id), 'archive')}
                                selectable={selectionMode}
                                selected={selectedIds.has(skill.id)}
                                onToggleSelect={() => toggleSelect(skill.id)}
                            />
                        ))}

                        {/* Footer */}
                        <div className="px-4 py-3 bg-surface-alt border-t border-default text-sm text-secondary">
                            Showing <span className="font-medium">{sortedSkills.length}</span> of{' '}
                            <span className="font-medium">{allSkills.length}</span> skill{allSkills.length !== 1 ? 's' : ''}
                        </div>
                    </div>
                )}
            </div>

            {/* ============ MODALS ============ */}

            {/* Detail Panel (slide-over). Drives create + edit + view
                via the single unified form.
                Create mode is detected by `!detailSkill.id` inside the panel. */}
            {detailSkill && (
                <AgentSkillDetailPanel
                    skill={detailSkill}
                    initialTab={detailTab}
                    onClose={() => { setDetailSkill(null); setDetailTab('overview'); }}
                    onCopy={() => {
                        handleCopySkill(detailSkill);
                    }}
                    onSave={async (data) => {
                        // Create returns the new skill id so the panel
                        // can chain-POST any buffered Resources / Scripts
                        // / Assets against it before closing.
                        if (!detailSkill.id) {
                            return await handleCreateSkill(data);
                        }
                        await handleUpdateSkill(detailSkill.id, data);
                        return undefined;
                    }}
                    onDelete={() => {
                        handleDeleteSkill(detailSkill.id);
                        setDetailSkill(null);
                    }}
                    onPublish={asyncHandler(() => handlePublish(detailSkill.id), 'publish')}
                    onValidate={asyncHandler(() => handleValidate(detailSkill.id), 'validate')}
                    onDeprecate={asyncHandler((reason) => handleDeprecate(detailSkill.id, reason), 'deprecate')}
                    onArchive={asyncHandler(() => handleArchive(detailSkill.id), 'archive')}
                    loadExecutions={async (skillId, offset, limit) => {
                        const result = await gatewayClient.listAgentSkillExecutions(skillId, { offset, limit });
                        return result;
                    }}
                    loadResources={async (skillId) => {
                        const resp = await listSkillResources(skillId);
                        return resp.resources || [];
                    }}
                    loadScripts={async (skillId) => {
                        const resp = await listSkillScripts(skillId);
                        return resp.scripts || [];
                    }}
                    loadAssets={async (skillId) => {
                        const resp = await listSkillAssets(skillId);
                        return resp.assets || [];
                    }}
                    getResource={getSkillResource}
                    createResource={createSkillResource}
                    updateResource={updateSkillResource}
                    deleteResource={deleteSkillResource}
                    getScript={getSkillScript}
                    createScript={createSkillScript}
                    updateScript={updateSkillScript}
                    deleteScript={deleteSkillScript}
                    getAsset={getSkillAsset}
                    createAsset={createSkillAsset}
                    updateAsset={updateSkillAsset}
                    deleteAsset={deleteSkillAsset}
                    fetchGroupTree={async () => {
                        const response = await fetchGroupHierarchyTree();
                        return response.tree;
                    }}
                    onUpdateGroups={async (skillId, groupIds) => {
                        // The group-assignment shortcut on the skill
                        // detail screen — same auto-bind side effect
                        // as full create/update, so surface the same
                        // toast.
                        const response = await gatewayClient.updateAgentSkill(skillId, { group_ids: groupIds });
                        showAutoBindNotice(response.auto_bound_server_group_ids);
                    }}
                    onRefresh={asyncHandler(loadSkills, 'refresh')}
                />
            )}

            {/* Import Skills Modal */}
            {isImporting && (
                <AgentSkillImportModal
                    onClose={() => setIsImporting(false)}
                    onImport={(files) => gatewayClient.importAgentSkills(files)}
                    onImportComplete={asyncHandler(loadSkills, 'import complete')}
                    limits={importLimits}
                />
            )}

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={deleteConfirmation !== null}
                onClose={() => { setDeleteConfirmation(null); setDeleteError(null); }}
                onConfirm={asyncHandler(confirmDeleteSkill, 'confirm')}
                itemType="skill"
                itemName={deleteConfirmation?.skillName || ''}
                itemInfo={deleteConfirmation?.skillInfo ? {
                    id: deleteConfirmation.skillInfo.id,
                    status: deleteConfirmation.skillInfo.status,
                    version: deleteConfirmation.skillInfo.version,
                    description: deleteConfirmation.skillInfo.description,
                    enabled: deleteConfirmation.skillInfo.enabled,
                } : undefined}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />

            {/* Bulk Delete Confirmation Modal */}
            {bulkConfirmOpen && (() => {
                const publishedSelected = Array.from(selectedIds)
                    .map(id => skills[id])
                    .filter(s => s && s.status === 'published');
                const deletableCount = selectedIds.size - publishedSelected.length;
                return (
                    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
                        <div className="bg-surface rounded-lg shadow-xl border border-default w-full max-w-md">
                            <div className="px-6 py-4 border-b border-default flex items-center space-x-3">
                                <div className="p-2 rounded-lg bg-red-100">
                                    <Trash2 className="w-5 h-5 text-red-600" />
                                </div>
                                <h3 className="text-lg font-semibold text-default">
                                    Delete {selectedIds.size} skill{selectedIds.size !== 1 ? 's' : ''}?
                                </h3>
                            </div>
                            <div className="px-6 py-4 space-y-3">
                                <p className="text-sm text-secondary">
                                    This permanently deletes the selected skill{selectedIds.size !== 1 ? 's' : ''} and
                                    all associated resources, scripts, assets, and group assignments. This cannot be undone.
                                </p>
                                {publishedSelected.length > 0 && (
                                    <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-800">
                                        {publishedSelected.length} published skill{publishedSelected.length !== 1 ? 's' : ''} will
                                        be <span className="font-medium">skipped</span> — published skills must be
                                        deprecated then archived before deletion.
                                        {deletableCount > 0
                                            ? ` ${deletableCount} skill${deletableCount !== 1 ? 's' : ''} will be deleted.`
                                            : ' Nothing will be deleted.'}
                                    </div>
                                )}
                                {bulkError && (
                                    <div className="rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-700 flex items-start space-x-2">
                                        <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                        <span>{bulkError}</span>
                                    </div>
                                )}
                            </div>
                            <div className="px-6 py-4 border-t border-default flex items-center justify-end space-x-3">
                                <button
                                    onClick={() => { setBulkConfirmOpen(false); setBulkError(null); }}
                                    disabled={isBulkDeleting}
                                    className="px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={asyncHandler(confirmBulkDelete, 'click')}
                                    disabled={isBulkDeleting}
                                    className="px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700 transition-colors flex items-center space-x-2 disabled:opacity-50"
                                >
                                    {isBulkDeleting && <Loader2 className="w-4 h-4 animate-spin" />}
                                    <span>Delete</span>
                                </button>
                            </div>
                        </div>
                    </div>
                );
            })()}
        </div>
    );
};

export default AgentSkillsTab;