/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import {
    Plus,
    Search,
    CheckCircle2,
    XCircle,
    Edit2,
    Trash2,
    AlertCircle,
    AlertTriangle,
    X,
    HelpCircle,
    Shield,
    Gauge,
    Share2,
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import {
    RoutingGroup,
    RoutingGroupCreate,
    RoutingGroupListItem,
    RoutingGroupTierAssignment,
    RoutingStrategy,
} from '../../../lib/llmTypes';
import RoutingGroupForm from './RoutingGroupForm';
import RoutingGroupHelpPanel from './RoutingGroupHelpPanel';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { ProviderLogo } from '@/components/llm/config/providerLogos';
import toast from 'react-hot-toast';
import { TABLE_TH, TABLE_TH_TYPE, TABLE_THEAD } from '../../ui/tableStyles';
import { failureSentence, errorKind, errorMessage } from '../../../lib/narrow';
import { modelSaveRefusalText } from '../config/providerConnection';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

// Helper function to get health status icon and color
const getHealthStatusDisplay = (model: any) => {
    if (!model.enabled) {
        return {
            icon: XCircle,
            color: 'text-muted',
            bgColor: 'bg-surface-raised',
            label: 'Disabled',
            textColor: 'text-secondary'
        };
    }

    const status = model.health_status;

    if (status === 'healthy') {
        return {
            icon: CheckCircle2,
            color: 'text-green-600',
            bgColor: 'bg-green-100',
            label: 'Healthy',
            textColor: 'text-green-700'
        };
    } else if (status === 'degraded') {
        return {
            icon: AlertTriangle,
            color: 'text-yellow-600',
            bgColor: 'bg-yellow-100',
            label: 'Degraded',
            textColor: 'text-yellow-700'
        };
    } else if (status === 'unhealthy') {
        return {
            icon: XCircle,
            color: 'text-red-600',
            bgColor: 'bg-red-100',
            label: 'Unhealthy',
            textColor: 'text-red-700'
        };
    } else {
        return {
            icon: AlertCircle,
            color: 'text-muted',
            bgColor: 'bg-surface-raised',
            label: 'Unknown',
            textColor: 'text-secondary'
        };
    }
};

interface RoutingGroupsPanelProps {
    onRefresh?: () => void;
    onFormStateChange?: (isActive: boolean) => void;
}

const RoutingGroupsSubTab: React.FC<RoutingGroupsPanelProps> = ({ onRefresh, onFormStateChange }) => {
    const { gatewayClient } = useGateway();
    const [routingGroups, setRoutingGroups] = useState<RoutingGroupListItem[]>([]);
    const [selectedGroup, setSelectedGroup] = useState<RoutingGroup | null>(null);
    const [showForm, setShowForm] = useState(false);
    // Complexity-routing tier map: loaded when the form opens for an existing
    // group, held here while editing, persisted after the group saves.
    const [pendingTiers, setPendingTiers] = useState<RoutingGroupTierAssignment[]>([]);
    const [initialTiers, setInitialTiers] = useState<RoutingGroupTierAssignment[]>([]);
    const [formGroupModels, setFormGroupModels] = useState<{ id: string; name: string }[]>([]);
    const [isEditing, setIsEditing] = useState(false);
    const [loading, setLoading] = useState(true);
    const hasLoadedRef = useRef(false);
    const [searchTerm, setSearchTerm] = useState('');
    const [filterEnabled, setFilterEnabled] = useState<'all' | 'enabled' | 'disabled'>('all');
    const [expandedGroupId, setExpandedGroupId] = useState<string | null>(null);
    const [groupModels, setGroupModels] = useState<Record<string, any[]>>({});
    const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string; strategy: string; numModels: number } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [showHelp, setShowHelp] = useState(false);

    const loadRoutingGroups = async () => {
        try {
            if (!hasLoadedRef.current) setLoading(true);
            const response = await gatewayClient.listRoutingGroups({
                enabled_only: false,
            });
            setRoutingGroups(response.routing_groups);

            // Auto-load models for all groups to show preview
            response.routing_groups.forEach((group: any) => {
                if (group.num_models > 0) {
                    runBackground(loadGroupModels(group.id), 'group models');
                }
            });
        } catch (error) {
            console.error('Failed to load routing groups:', errorKind(error));
            toast.error(failureSentence('Failed to load routing groups', error));
        } finally {
            setLoading(false);
            hasLoadedRef.current = true;
        }
    };

    const loadGroupModels = async (groupId: string) => {
        if (groupModels[groupId]) {
            return; // Already loaded
        }

        try {
            const response = await gatewayClient.listDeploymentsByRoutingGroup({
                routing_group_id: groupId,
                enabled_only: false,
            });
            setGroupModels(prev => ({
                ...prev,
                [groupId]: response.deployments || [],
            }));
        } catch (error) {
            console.error('Failed to load group models:', errorKind(error));
            toast.error(failureSentence('Failed to load models for routing group', error));
        }
    };

    const handleCreate = () => {
        setSelectedGroup(null);
        setIsEditing(false);
        setFormGroupModels([]);
        setInitialTiers([]);
        setPendingTiers([]);
        setShowForm(true);
    };

    const handleEdit = async (group: RoutingGroupListItem) => {
        try {
            // Load full group details
            const fullGroup = await gatewayClient.getRoutingGroup(group.id);
            setSelectedGroup(fullGroup);
            setIsEditing(true);

            // Members and tier map for the complexity-routing editor. Both are
            // best-effort: a failure here must not block editing the group.
            try {
                const deployments = await gatewayClient.listDeploymentsByRoutingGroup({
                    routing_group_id: group.id,
                    enabled_only: false,
                });
                setFormGroupModels(
                    // A deployment is keyed by `model_id`. The virtual model that
                    // represents a routing group is not a legal tier target.
                    (deployments.deployments || [])
                        .filter((d) => d.provider !== 'routing_group')
                        .map((d) => ({
                            id: d.model_id,
                            name: d.model_name || d.model_id,
                        }))
                );
            } catch (modelsError) {
                // The form opens without them, and says why it has none to offer.
                setFormGroupModels([]);
                toast.error(failureSentence('Failed to load the models of the routing group', modelsError));
            }

            try {
                const tierMap = await gatewayClient.getRoutingGroupTiers(group.id);
                const loaded = (tierMap.tiers || []).map((t) => ({
                    tier: t.tier,
                    llm_model_id: t.llm_model_id,
                    position: t.position,
                }));
                setInitialTiers(loaded);
                setPendingTiers(loaded);
            } catch (tiersError) {
                // The form opens with no tier, and says the stored ones were not read.
                setInitialTiers([]);
                setPendingTiers([]);
                toast.error(failureSentence('Failed to load the tiers of the routing group', tiersError));
            }

            setShowForm(true);
        } catch (error) {
            toast.error(failureSentence('Failed to load routing group details', error));
        }
    };

    const handleDelete = (group: RoutingGroupListItem) => {
        setDeleteTarget({
            id: group.id,
            name: group.name,
            strategy: group.routing_strategy,
            numModels: group.num_models,
        });
        setDeleteError(null);
        onFormStateChange?.(true);  // Pause auto-refresh while modal is open
    };

    const handleConfirmDelete = async () => {
        if (!deleteTarget) return;

        setIsDeleting(true);
        try {
            const result = await gatewayClient.deleteRoutingGroup(deleteTarget.id);
            if (result.models_disconnected > 0) {
                toast.success(`Routing group deleted — ${result.models_disconnected} model(s) disconnected`);
            } else {
                toast.success('Routing group deleted successfully');
            }
            setDeleteTarget(null);
            onFormStateChange?.(false);  // Resume auto-refresh
            await loadRoutingGroups();
            onRefresh?.();
        } catch (error) {
            setDeleteError(errorMessage(error, 'Failed to delete routing group'));
        } finally {
            setIsDeleting(false);
        }
    };

    const handleFormSubmit = async (data: Partial<RoutingGroupCreate>) => {
        try {
            let groupId = selectedGroup?.id;
            // An update names no id: the group is the one in the path.
            const { id, ...fields } = data;
            if (isEditing && selectedGroup) {
                await gatewayClient.updateRoutingGroup(selectedGroup.id, fields);
                toast.success('Routing group updated successfully');
            } else {
                const { name, mode } = fields;
                if (!id || !name || !mode) throw new Error('A routing group needs an ID, a name and a mode.');
                const created = await gatewayClient.createRoutingGroup({ ...fields, id, name, mode });
                groupId = created.id ?? groupId;
                toast.success('Routing group created successfully');
            }

            // The tier map lives behind its own endpoint. Persist it after the
            // group itself, and only for the strategy that reads it — a failure
            // here must not read as "the group did not save", because it did.
            if (groupId && data.routing_strategy === 'complexity-based-routing') {
                try {
                    await gatewayClient.replaceRoutingGroupTiers(groupId, pendingTiers);
                } catch (tierError) {
                    toast.error(
                        `Group saved, but the tier map did not: ${errorMessage(tierError)}`
                    );
                }
            }

            setShowForm(false);
            await loadRoutingGroups();
            onRefresh?.();
        } catch (error) {
            toast.error(`Failed to ${isEditing ? 'update' : 'create'} routing group: ${modelSaveRefusalText(error) ?? errorMessage(error)}`);
            throw error;
        }
    };

    const toggleGroupExpansion = async (groupId: string) => {
        if (expandedGroupId === groupId) {
            setExpandedGroupId(null);
        } else {
            setExpandedGroupId(groupId);
            await loadGroupModels(groupId);
        }
    };

    useEffect(() => {
        runBackground(loadRoutingGroups(), 'routing groups');
    }, []);

    // Notify parent when form state changes (for auto-refresh pause)
    useEffect(() => {
        onFormStateChange?.(showForm || showHelp);
    }, [showForm, showHelp, onFormStateChange]);

    // Unmounting with the guide open (switching to another sub-tab) must not
    // leave the parent's auto-refresh paused with nothing left to resume it.
    useEffect(() => () => onFormStateChange?.(false), [onFormStateChange]);

    // Handle ESC key to close modal
    useEffect(() => {
        const handleEscape = (e: KeyboardEvent) => {
            if (e.key === 'Escape' && showForm) {
                setShowForm(false);
            }
        };

        document.addEventListener('keydown', handleEscape);
        return () => document.removeEventListener('keydown', handleEscape);
    }, [showForm]);

    // Prevent body scroll when modal is open
    useEffect(() => {
        if (showForm) {
            document.body.style.overflow = 'hidden';
        } else {
            document.body.style.overflow = 'unset';
        }

        return () => {
            document.body.style.overflow = 'unset';
        };
    }, [showForm]);

    // Filter routing groups
    const filteredGroups = routingGroups.filter(group => {
        const matchesSearch = group.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            group.id.toLowerCase().includes(searchTerm.toLowerCase());
        const matchesEnabled = filterEnabled === 'all' ||
            (filterEnabled === 'enabled' && group.enabled) ||
            (filterEnabled === 'disabled' && !group.enabled);
        return matchesSearch && matchesEnabled;
    });

    const getStrategyColor = (strategy: RoutingStrategy) => {
        const colors: Record<RoutingStrategy, string> = {
            'simple-shuffle': 'bg-surface-raised text-default border-default',
            'least-busy': 'bg-blue-100 text-blue-800 border-blue-200',
            'usage-based-routing': 'bg-green-100 text-green-800 border-green-200',
            'usage-based-routing-v2': 'bg-green-100 text-green-800 border-green-200',
            'latency-based-routing': 'bg-yellow-100 text-yellow-800 border-yellow-200',
            'cost-based-routing': 'bg-purple-100 text-purple-800 border-purple-200',
            'complexity-based-routing': 'bg-indigo-100 text-indigo-800 border-indigo-200',
        };
        return colors[strategy] || 'bg-surface-raised text-default border-default';
    };

    /**
     * Color/title lookup for mode pills. Family-grouped so the
     * eye can spot at-a-glance whether a row is text (chat /
     * responses / completion), vector (embedding / rerank),
     * media (image / audio / video), or policy (moderation).
     * A row without a mode reads as chat.
     */
    const getModeColor = (mode?: string) => {
        const colors: Record<string, string> = {
            chat:                  'bg-blue-100 text-blue-800 border-blue-200',
            responses:             'bg-blue-100 text-blue-800 border-blue-200',
            completion:            'bg-blue-100 text-blue-800 border-blue-200',
            embedding:             'bg-indigo-100 text-indigo-800 border-indigo-200',
            rerank:                'bg-indigo-100 text-indigo-800 border-indigo-200',
            image_generation:      'bg-pink-100 text-pink-800 border-pink-200',
            audio_speech:          'bg-amber-100 text-amber-800 border-amber-200',
            audio_transcription:   'bg-amber-100 text-amber-800 border-amber-200',
            video_generation:      'bg-fuchsia-100 text-fuchsia-800 border-fuchsia-200',
            moderation:            'bg-red-100 text-red-800 border-red-200',
        };
        return colors[mode || 'chat'] || 'bg-surface-raised text-default border-default';
    };

    const getModeTitle = (mode?: string): string => {
        const titles: Record<string, string> = {
            chat:                  '/v1/chat/completions — multi-turn conversations',
            responses:             '/v1/responses — OpenAI Responses API',
            completion:            '/v1/completions — single-prompt completion (legacy)',
            embedding:             '/v1/embeddings — vector embeddings',
            rerank:                '/v1/rerank — re-rank documents by relevance',
            image_generation:      '/v1/images/generations — text-to-image',
            audio_speech:          '/v1/audio/speech — text-to-speech',
            audio_transcription:  '/v1/audio/transcriptions — speech-to-text',
            video_generation:      '/v1/videos/generations — text-to-video',
            moderation:            '/v1/moderations — content moderation',
        };
        return titles[mode || 'chat'] || `Mode: ${mode}`;
    };

    return (
        <>
            {/* Modal for Create/Edit Routing Group */}
            {showForm && (
                <div className="fixed inset-0 z-50 flex items-center justify-center">
                    {/* Backdrop */}
                    <div
                        className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
                        onClick={() => setShowForm(false)}
                    />

                    {/* Modal */}
                    <div className="relative bg-surface rounded-lg shadow-xl w-full max-w-4xl max-h-[90vh] flex flex-col">
                        {/* Header */}
                        <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                            <h2 className="text-xl font-semibold text-default">
                                {isEditing ? `Edit Routing Group — ${selectedGroup?.name || selectedGroup?.id}` : 'Create Routing Group'}
                            </h2>
                            <button
                                onClick={() => setShowForm(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>

                        {/* Content */}
                        <div className="scroll-region px-6 py-4">
                            <RoutingGroupForm
                                routingGroup={selectedGroup}
                                isEditing={isEditing}
                                onSubmit={handleFormSubmit}
                                onCancel={() => setShowForm(false)}
                                availableGroups={routingGroups.map(g => ({ id: g.id, name: g.name }))}
                                groupModels={formGroupModels}
                                initialTiers={initialTiers}
                                onTiersChange={setPendingTiers}
                            />
                        </div>
                    </div>
                </div>
            )}

            <div className="space-y-4">
                {/* Header */}
                <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-4">
                        {/* Search */}
                        <div className="relative">
                            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                            <input
                                type="text"
                                placeholder="Search routing groups..."
                                value={searchTerm}
                                onChange={(e) => setSearchTerm(e.target.value)}
                                className="pl-10 pr-4 py-2 border border-strong rounded-lg w-64 focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            />
                        </div>

                        {/* Filter */}
                        <select
                            value={filterEnabled}
                            onChange={(e) => setFilterEnabled(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                        >
                            <option value="all">All Groups</option>
                            <option value="enabled">Enabled Only</option>
                            <option value="disabled">Disabled Only</option>
                        </select>
                    </div>

                    <div className="flex items-center space-x-2">
                        <button
                            onClick={() => setShowHelp(!showHelp)}
                            className={`flex items-center space-x-2 px-4 py-2 border rounded-lg transition-colors ${
                                showHelp
                                    ? 'bg-primary-50 border-primary-300 text-primary-700'
                                    : 'border-strong text-secondary hover:bg-surface-alt'
                            }`}
                        >
                            <HelpCircle className="w-4 h-4" />
                            <span>Guide</span>
                        </button>

                        <button
                            onClick={handleCreate}
                            className="flex items-center space-x-2 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                        >
                            <Plus className="w-4 h-4" />
                            <span>Create Routing Group</span>
                        </button>
                    </div>
                </div>

                {/* Help Panel (collapsible) */}
                {showHelp && (
                    <RoutingGroupHelpPanel onClose={() => setShowHelp(false)} />
                )}

                {/* Info Banner (shown when help is closed) */}
                {!showHelp && (
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                        <div className="flex items-start space-x-3">
                            <Share2 className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <h4 className="text-sm font-semibold text-blue-900 mb-1">
                                    Routing Groups
                                </h4>
                                <p className="text-sm text-blue-800">
                                    Routing groups enable intelligent load balancing across multiple models with automatic failover,
                                    cost optimization, and performance-based routing strategies. Assign models to routing groups
                                    in the <strong>AI Models | Configuration</strong> page.
                                </p>
                            </div>
                        </div>
                    </div>
                )}

                {/* Loading State */}
                {loading ? (
                    <div className="flex items-center justify-center py-12">
                        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
                    </div>
                ) : filteredGroups.length === 0 ? (
                    /* Empty State */
                    <div className="text-center py-12 bg-surface-alt rounded-lg border border-default">
                        <Share2 className="w-16 h-16 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-semibold text-default mb-2">
                            {searchTerm || filterEnabled !== 'all' ? 'No Matching Groups' : 'No Routing Groups Yet'}
                        </h3>
                        <p className="text-secondary mb-4">
                            {searchTerm || filterEnabled !== 'all'
                                ? 'Try adjusting your search or filters'
                                : 'Create your first routing group to enable load balancing and intelligent routing'
                            }
                        </p>
                        {!searchTerm && filterEnabled === 'all' && (
                            <button
                                onClick={handleCreate}
                                className="inline-flex items-center space-x-2 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Create Routing Group</span>
                            </button>
                        )}
                    </div>
                ) : (
                    /* Routing Groups Table */
                    <div className="bg-surface border border-default rounded-lg overflow-hidden">
                        <table className="min-w-full divide-y divide-default">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>Status</th>
                                    <th className={TABLE_TH}>Name</th>
                                    <th className={TABLE_TH}>Mode</th>
                                    <th className={TABLE_TH}>Strategy</th>
                                    <th className={`px-4 py-3 text-center ${TABLE_TH_TYPE}`}>Models</th>
                                    <th className={`px-4 py-3 text-center ${TABLE_TH_TYPE}`}>Features</th>
                                    <th className={`px-4 py-3 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-default">
                                {filteredGroups.map((group) => (
                                    <React.Fragment key={group.id}>
                                        <tr
                                            className="hover:bg-surface-alt transition-colors cursor-pointer"
                                            onClick={asyncHandler(() => toggleGroupExpansion(group.id), 'click')}
                                        >
                                            {/* Status */}
                                            <td className="px-4 py-3 whitespace-nowrap">
                                                {group.enabled ? (
                                                    <CheckCircle2 className="w-4 h-4 text-green-600" />
                                                ) : (
                                                    <XCircle className="w-4 h-4 text-muted" />
                                                )}
                                            </td>

                                            {/* Name + Description */}
                                            <td className="px-4 py-3">
                                                <div className="text-sm font-semibold text-default">{group.name}</div>
                                                {group.description && (
                                                    <div className="text-xs text-muted truncate max-w-xs">{group.description}</div>
                                                )}
                                            </td>

                                            {/* Mode — the inbound surface this group serves */}
                                            <td className="px-4 py-3 whitespace-nowrap">
                                                <span
                                                    className={`px-2 py-0.5 text-xs font-medium border rounded ${getModeColor(group.mode)}`}
                                                    title={getModeTitle(group.mode)}
                                                >
                                                    {group.mode || 'chat'}
                                                </span>
                                            </td>

                                            {/* Strategy */}
                                            <td className="px-4 py-3 whitespace-nowrap">
                                                <span className={`px-2 py-0.5 text-xs font-medium border rounded ${getStrategyColor(group.routing_strategy)}`}>
                                                    {group.routing_strategy}
                                                </span>
                                            </td>

                                            {/* Models count + provider logos */}
                                            <td className="px-4 py-3 text-center whitespace-nowrap">
                                                <div className="flex items-center justify-center space-x-1.5">
                                                    <span className="text-sm font-semibold text-default">{group.num_models}</span>
                                                    {groupModels[group.id] && (
                                                        <div className="flex -space-x-1">
                                                            {[...new Set(groupModels[group.id]
                                                                .filter((m: any) => m.provider !== 'routing_group')
                                                                .map((m: any) => m.provider)
                                                            )].slice(0, 4).map((provider: string) => (
                                                                <div key={provider} className="bg-surface border border-default rounded-full p-0.5">
                                                                    <ProviderLogo provider={provider} size="xs" />
                                                                </div>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>
                                            </td>

                                            {/* Feature badges */}
                                            <td className="px-4 py-3">
                                                <div className="flex items-center justify-center flex-wrap gap-1">
                                                    {group.has_fallbacks && (
                                                        <span className="inline-flex items-center px-1.5 py-0.5 bg-green-50 border border-green-200 rounded text-[10px] text-green-700" title={`${group.fallback_count} fallback group(s)`}>
                                                            <Shield className="w-3 h-3 mr-0.5" />
                                                            {group.fallback_count}
                                                        </span>
                                                    )}
                                                    {(group.rpm_limit || group.tpm_limit) && (
                                                        <span className="inline-flex items-center px-1.5 py-0.5 bg-orange-50 border border-orange-200 rounded text-[10px] text-orange-700" title={[group.rpm_limit ? `${group.rpm_limit} RPM` : null, group.tpm_limit ? `${group.tpm_limit.toLocaleString()} TPM` : null].filter(Boolean).join(', ')}>
                                                            <Gauge className="w-3 h-3" />
                                                        </span>
                                                    )}
                                                </div>
                                            </td>

                                            {/* Actions */}
                                            <td className="px-4 py-3 text-right whitespace-nowrap">
                                                <div className="flex items-center justify-end space-x-1">
                                                    <button
                                                        onClick={(e) => { e.stopPropagation(); runBackground(handleEdit(group), 'edit group'); }}
                                                        className="p-1.5 text-muted hover:text-primary-600 hover:bg-surface-raised rounded transition-colors"
                                                        title="Edit routing group"
                                                    >
                                                        <Edit2 className="w-3.5 h-3.5" />
                                                    </button>
                                                    <button
                                                        onClick={(e) => { e.stopPropagation(); handleDelete(group); }}
                                                        className="p-1.5 text-muted hover:text-red-600 hover:bg-red-50 rounded transition-colors"
                                                        title="Delete routing group"
                                                    >
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>

                                        {/* Expanded row with model details */}
                                        {expandedGroupId === group.id && (
                                            <tr>
                                                <td colSpan={8} className="px-4 py-3 bg-surface-alt">
                                                    <div className="grid grid-cols-2 gap-4">
                                                        {/* Left: Config details */}
                                                        <div className="space-y-2">
                                                            <div className="text-xs font-semibold text-secondary uppercase tracking-wider">Configuration</div>
                                                            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                                                                <div className="text-muted">Retries</div>
                                                                <div className="text-default font-medium">{group.num_retries ?? 3}x</div>
                                                                <div className="text-muted">Cooldown</div>
                                                                <div className="text-default font-medium">{group.cooldown_time ?? 60}s</div>
                                                                {group.max_parallel_requests && (
                                                                    <>
                                                                        <div className="text-muted">Max Parallel</div>
                                                                        <div className="text-default font-medium">{group.max_parallel_requests}</div>
                                                                    </>
                                                                )}
                                                                {group.rpm_limit && (
                                                                    <>
                                                                        <div className="text-muted">RPM Limit</div>
                                                                        <div className="text-default font-medium">{group.rpm_limit}</div>
                                                                    </>
                                                                )}
                                                                {group.tpm_limit && (
                                                                    <>
                                                                        <div className="text-muted">TPM Limit</div>
                                                                        <div className="text-default font-medium">{group.tpm_limit.toLocaleString()}</div>
                                                                    </>
                                                                )}
                                                            </div>

                                                            {/* Fallbacks */}
                                                            {group.has_fallbacks && (
                                                                <div className="mt-2">
                                                                    <div className="text-xs font-semibold text-secondary uppercase tracking-wider mb-1">Fallbacks ({group.fallback_count})</div>
                                                                    <div className="flex flex-wrap gap-1">
                                                                        {group.fallback_models?.map(id => (
                                                                            <code key={id} className="px-1.5 py-0.5 bg-green-50 border border-green-200 rounded text-[11px] text-green-800">{id}</code>
                                                                        ))}
                                                                        {group.context_window_fallbacks?.map(id => (
                                                                            <code key={`cw-${id}`} className="px-1.5 py-0.5 bg-blue-50 border border-blue-200 rounded text-[11px] text-blue-800" title="Context window fallback">{id}</code>
                                                                        ))}
                                                                        {group.content_policy_fallbacks?.map(id => (
                                                                            <code key={`cp-${id}`} className="px-1.5 py-0.5 bg-orange-50 border border-orange-200 rounded text-[11px] text-orange-800" title="Content policy fallback">{id}</code>
                                                                        ))}
                                                                        {group.default_fallbacks?.map(id => (
                                                                            <code key={`df-${id}`} className="px-1.5 py-0.5 bg-surface-raised border border-default rounded text-[11px] text-secondary" title="Default fallback">{id}</code>
                                                                        ))}
                                                                    </div>
                                                                </div>
                                                            )}
                                                        </div>

                                                        {/* Right: Assigned models */}
                                                        <div>
                                                            <div className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">
                                                                Assigned Models ({group.num_models})
                                                            </div>
                                                            {group.num_models === 0 ? (
                                                                <div className="flex items-center space-x-1.5 text-xs text-yellow-700">
                                                                    <AlertCircle className="w-3.5 h-3.5" />
                                                                    <span>No models assigned</span>
                                                                </div>
                                                            ) : !groupModels[group.id] ? (
                                                                <div className="text-xs text-muted">Loading...</div>
                                                            ) : (
                                                                <div className="space-y-1">
                                                                    {groupModels[group.id]
                                                                        .filter((model: any) => model.provider !== 'routing_group')
                                                                        .map((model: any) => {
                                                                            const healthStatus = getHealthStatusDisplay(model);
                                                                            const HealthIcon = healthStatus.icon;
                                                                            return (
                                                                                <div
                                                                                    key={model.model_id}
                                                                                    className="flex items-center justify-between bg-surface px-2.5 py-1.5 rounded border border-default text-xs"
                                                                                >
                                                                                    <div className="flex items-center space-x-2">
                                                                                        <HealthIcon className={`w-3.5 h-3.5 ${healthStatus.color} flex-shrink-0`} />
                                                                                        <ProviderLogo provider={model.provider} size="xs" />
                                                                                        <span className="font-medium text-default">{model.model_name}</span>
                                                                                        <span className="text-muted">w:{model.routing_weight}</span>
                                                                                    </div>
                                                                                    <span className="text-muted">{model.total_requests.toLocaleString()} req</span>
                                                                                </div>
                                                                            );
                                                                        })}
                                                                </div>
                                                            )}
                                                        </div>
                                                    </div>
                                                </td>
                                            </tr>
                                        )}
                                    </React.Fragment>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={!!deleteTarget}
                onClose={() => { setDeleteTarget(null); setDeleteError(null); onFormStateChange?.(false); }}
                onConfirm={asyncHandler(handleConfirmDelete, 'confirm')}
                itemType="routing-group"
                itemName={deleteTarget?.name || ''}
                itemInfo={deleteTarget ? {
                    id: deleteTarget.id,
                    description: `Strategy: ${deleteTarget.strategy} · ${deleteTarget.numModels} model(s)`,
                } : undefined}
                isDeleting={isDeleting}
                errorMessage={deleteError}
                customWarning={
                    deleteTarget && deleteTarget.numModels > 0
                        ? `This routing group has ${deleteTarget.numModels} model(s) assigned. They will be automatically disconnected and become standalone models.`
                        : undefined
                }
            />
        </>
    );
};

export default RoutingGroupsSubTab;