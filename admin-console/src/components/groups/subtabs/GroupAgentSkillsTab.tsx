/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    Sparkles,
    Loader,
    AlertCircle,
    Search,
    X,
    ArrowUpRight,
    CheckCircle,
    Info,
    HelpCircle
} from 'lucide-react';
import { ResourceGroup, AgentSkillInfo, EffectiveAgentSkillsResponse, formatGroupLabel } from '../../../lib/groupTypes';
import { formatSkillName } from '../../../lib/agentSkillTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import GroupAgentSkillsTableView from './GroupAgentSkillsTableView';
import SkillLimitsDialog from './SkillLimitsDialog';
import SkillGovernanceDialog from './SkillGovernanceDialog';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

type ViewMode = 'all' | 'direct' | 'inherited';

const GroupAgentSkillsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchEffectiveAgentSkills, removeSkillFromGroup, getSkillGlobalGovernance, updateSkillGlobalGovernance } = useGateway();
    const toast = useToast();

    const [effectiveSkills, setEffectiveSkills] = useState<EffectiveAgentSkillsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [viewMode, setViewMode] = useState<ViewMode>('all');
    const [removingId, setRemovingId] = useState<string | null>(null);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // Limits dialog state
    const [limitsSkill, setLimitsSkill] = useState<AgentSkillInfo | null>(null);

    // Governance dialog state
    const [governanceSkill, setGovernanceSkill] = useState<AgentSkillInfo | null>(null);

    // Skill discovery toggle
    const [updatingDiscovery, setUpdatingDiscovery] = useState(false);

    // Info modal state
    const [showInheritanceInfo, setShowInheritanceInfo] = useState(false);
    const [showAddSkillsInfo, setShowAddSkillsInfo] = useState(false);

    // Delete confirmation state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        skillId: string;
        skillName: string;
    } | null>(null);

    const loadSkills = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchEffectiveAgentSkills(group.id);
            setEffectiveSkills(response);
        } catch (err) {
            console.error('Failed to load agent skills:', errorKind(err));
            setError(errorMessage(err, 'Failed to load agent skills'));
        } finally {
            setLoading(false);
        }
    }, [group.id, fetchEffectiveAgentSkills]);

    useEffect(() => {
        runBackground(loadSkills(), 'skills');
    }, [loadSkills]);

    const handleRemoveSkill = (skillId: string, skillName: string) => {
        setDeleteError(null);
        setDeleteConfirmation({
            skillId,
            skillName
        });
    };

    const confirmRemoveSkill = async () => {
        if (!deleteConfirmation) return;

        const { skillId, skillName } = deleteConfirmation;

        try {
            setRemovingId(skillId);
            setError('');
            await removeSkillFromGroup(group.id, skillId);
            await loadSkills();
            onUpdate();
            toast.success(`Removed ${formatSkillName(skillName)} from ${formatGroupLabel(group)}`);
            setDeleteConfirmation(null);
        } catch (err: any) {
            console.error('Failed to remove skill:', errorKind(err));
            setDeleteError(err.detail || err.message || 'Failed to remove skill');
        } finally {
            setRemovingId(null);
        }
    };

    const handleToggleDiscovery = async (enabled: boolean) => {
        try {
            setUpdatingDiscovery(true);
            // The switch is `discovery_enabled` of the group's own skill
            // governance, and an update states that object whole: what the
            // group declares is read and sent back with the switch changed.
            const { skill_global_governance: own } = await getSkillGlobalGovernance(group.id);
            await updateSkillGlobalGovernance(group.id, { ...(own ?? {}), discovery_enabled: enabled });
            onUpdate();
            toast.success(`Programmatic skill discovery ${enabled ? 'enabled' : 'disabled'} for ${formatGroupLabel(group)}`);
        } catch (err: any) {
            console.error('Failed to update skill discovery:', errorKind(err));
            toast.error(err.detail || err.message || 'Failed to update skill discovery setting');
        } finally {
            setUpdatingDiscovery(false);
        }
    };

    // Get skills based on view mode
    const getDisplaySkills = (): AgentSkillInfo[] => {
        if (!effectiveSkills) return [];

        switch (viewMode) {
            case 'direct':
                return effectiveSkills.direct_skills || [];
            case 'inherited':
                return effectiveSkills.inherited_skills || [];
            case 'all':
            default:
                return effectiveSkills.effective_skills || [];
        }
    };

    // Filter skills by search term
    const displaySkills = getDisplaySkills();

    const filteredSkills = displaySkills.filter(skill => {
        const matches = skill.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (skill.description && skill.description.toLowerCase().includes(searchTerm.toLowerCase())) ||
            (skill.category && skill.category.toLowerCase().includes(searchTerm.toLowerCase())) ||
            skill.id.toLowerCase().includes(searchTerm.toLowerCase());
        return matches;
    });

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">Agent Skills</h2>
                    <p className="text-sm text-secondary mt-1">
                        Agent skills available to this resource group for LLM-driven task execution.
                    </p>
                </div>
                <div className="flex items-center space-x-2">
                    <button
                        onClick={() => setShowInheritanceInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn about skill inheritance"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Inheritance</span>
                    </button>
                    <button
                        onClick={() => setShowAddSkillsInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn how to add skills"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Add Skills</span>
                    </button>
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-red-900">Error</p>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            )}

            {/* Programmatic Skill Discovery Toggle */}
            <div className="flex items-center justify-between p-4 bg-surface-alt border border-default rounded-lg">
                <div className="flex-1 min-h-0 min-w-0">
                    <div className="flex items-center space-x-2">
                        <h3 className="text-sm font-medium text-default">Programmatic Skill Discovery</h3>
                        {updatingDiscovery && <Loader className="w-4 h-4 text-primary-600 animate-spin" />}
                    </div>
                    <p className="text-xs text-muted mt-1">
                        Allow apps and agents to discover available skills via the catalog API (<code className="text-xs bg-surface-inset px-1 rounded">/v1/proxy/skills/catalog</code>).
                    </p>
                </div>
                <div className="flex items-center space-x-2 ml-4">
                    <button
                        onClick={asyncHandler(() => handleToggleDiscovery(true), 'click')}
                        disabled={updatingDiscovery}
                        className={`px-3 py-1.5 text-sm font-medium rounded-l-lg border transition-colors ${
                            group.skill_discovery_enabled !== false
                                ? 'bg-primary-600 text-white border-primary-600'
                                : 'bg-surface text-secondary border-strong hover:bg-surface-alt'
                        }`}
                    >
                        Enabled
                    </button>
                    <button
                        onClick={asyncHandler(() => handleToggleDiscovery(false), 'click')}
                        disabled={updatingDiscovery}
                        className={`px-3 py-1.5 text-sm font-medium rounded-r-lg border transition-colors -ml-2 ${
                            group.skill_discovery_enabled === false
                                ? 'bg-red-600 text-white border-red-600'
                                : 'bg-surface text-secondary border-strong hover:bg-surface-alt'
                        }`}
                    >
                        Disabled
                    </button>
                </div>
            </div>

            {/* Filters & Search */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center space-y-3 sm:space-y-0 sm:space-x-4">
                {/* Search */}
                <div className="flex-1 relative min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                        placeholder="Search skills..."
                        className="w-full pl-10 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
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

                {/* View Mode Tabs */}
                <div className="flex items-center space-x-1 bg-surface-raised rounded-lg p-1 overflow-x-auto">
                    <button
                        onClick={() => setViewMode('all')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'all'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        All ({effectiveSkills?.effective_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('direct')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'direct'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Direct ({effectiveSkills?.direct_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('inherited')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'inherited'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Inherited ({effectiveSkills?.inherited_count || 0})
                    </button>
                </div>
            </div>

            {/* Info Banner for Inherited View */}
            {viewMode === 'inherited' && effectiveSkills && effectiveSkills.inherited_count > 0 && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <Info className="w-5 h-5 text-muted flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-secondary">Inherited Skills</p>
                        <p className="text-sm text-secondary mt-1">
                            These skills are inherited from parent groups in the hierarchy. To modify them, edit the parent group.
                        </p>
                    </div>
                </div>
            )}

            {/* Skills Table */}
            {loading ? (
                <div className="flex items-center justify-center h-48">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                        <p className="text-sm text-secondary">Loading agent skills...</p>
                    </div>
                </div>
            ) : filteredSkills.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <Sparkles className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {searchTerm
                            ? 'No skills found matching your search'
                            : viewMode === 'direct'
                                ? 'No direct skills assigned yet'
                                : viewMode === 'inherited'
                                    ? 'No inherited skills from parent groups'
                                    : 'No agent skills available'}
                    </p>
                    {!searchTerm && (
                        <p className="text-sm text-muted mt-2">
                            {viewMode === 'direct'
                                ? 'Click "Add Skills" to assign agent skills directly to this group'
                                : viewMode === 'inherited'
                                    ? 'Skills will appear here when assigned to parent groups'
                                    : 'Add skills from the Agent Skills section or assign them to parent groups'}
                        </p>
                    )}
                </div>
            ) : (
                <GroupAgentSkillsTableView
                    skills={filteredSkills}
                    onRemove={handleRemoveSkill}
                    onConfigureGovernance={setGovernanceSkill}
                    onConfigureLimits={setLimitsSkill}
                    removingId={removingId}
                />
            )}

            {/* Delete Confirmation Modal */}
            {deleteConfirmation && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        setDeleteConfirmation(null);
                        setDeleteError(null);
                    }}
                    onConfirm={asyncHandler(confirmRemoveSkill, 'confirm')}
                    action="remove"
                    itemType="skill"
                    itemName={formatSkillName(deleteConfirmation.skillName)}
                    itemInfo={{
                        id: deleteConfirmation.skillId,
                        description: 'Agent Skill',
                        enabled: true,
                    }}
                    customWarning={`This will remove ${formatSkillName(deleteConfirmation.skillName)} from ${formatGroupLabel(group)}. Members will no longer have access to this skill.`}
                    customNote="The skill will still be available in the system and can be re-added later."
                    isDeleting={removingId === deleteConfirmation.skillId}
                    errorMessage={deleteError}
                />
            )}

            {/* Inheritance Info Modal */}
            {showInheritanceInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <div className="flex items-center space-x-3">
                                <div className="p-2 bg-surface-alt rounded-lg">
                                    <Info className="w-5 h-5 text-muted" />
                                </div>
                                <h2 className="text-xl font-semibold text-default">
                                    Skill Inheritance
                                </h2>
                            </div>
                            <button
                                onClick={() => setShowInheritanceInfo(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <div>
                                <h3 className="text-base font-semibold text-default mb-2">
                                    What is Skill Inheritance?
                                </h3>
                                <p className="text-sm text-secondary">
                                    Resource groups can inherit agent skills from their parent groups in the hierarchy.
                                    This means skills assigned to parent groups automatically become available to all descendant groups.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="text-sm font-semibold text-secondary mb-2">
                                    Skill Types
                                </h4>
                                <ul className="space-y-2 text-sm text-secondary">
                                    <li className="flex items-start space-x-2">
                                        <CheckCircle className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                        <span><strong>Direct Skills:</strong> Skills explicitly assigned to this group</span>
                                    </li>
                                    <li className="flex items-start space-x-2">
                                        <ArrowUpRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                        <span><strong>Inherited Skills:</strong> Skills from parent groups (shown with light blue background)</span>
                                    </li>
                                </ul>
                            </div>

                            <div>
                                <h4 className="text-sm font-semibold text-default mb-2">
                                    Managing Inherited Skills
                                </h4>
                                <p className="text-sm text-secondary mb-2">
                                    Inherited skills cannot be removed from this group directly. To modify them, edit the parent group that owns the skill.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <p className="text-xs text-secondary">
                                    <strong>Example:</strong> If the "Organization" group has a "query-sales-data" skill, all teams under that organization
                                    (Engineering, Marketing, etc.) will automatically inherit access to that skill.
                                </p>
                            </div>
                        </div>
                        <div className="flex items-center justify-end p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={() => setShowInheritanceInfo(false)}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                            >
                                Got it
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Per-Skill Governance Dialog */}
            {governanceSkill && (
                <SkillGovernanceDialog
                    isOpen={true}
                    onClose={() => setGovernanceSkill(null)}
                    onSave={asyncHandler(loadSkills, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    skill={governanceSkill}
                />
            )}

            {/* Per-Skill Limits Dialog */}
            {limitsSkill && (
                <SkillLimitsDialog
                    isOpen={true}
                    onClose={() => setLimitsSkill(null)}
                    onSave={asyncHandler(loadSkills, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    skill={limitsSkill}
                />
            )}

            {/* Add Skills Info Modal */}
            {showAddSkillsInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <div className="flex items-center space-x-3">
                                <div className="p-2 bg-primary-pale rounded-lg">
                                    <HelpCircle className="w-5 h-5 text-primary" />
                                </div>
                                <h2 className="text-xl font-semibold text-default">
                                    How to Add Skills to a Group
                                </h2>
                            </div>
                            <button
                                onClick={() => setShowAddSkillsInfo(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <p className="text-sm text-secondary">
                                Agent skills are managed centrally in the <strong>Agent Skills</strong> page and then assigned
                                to resource groups to control access.
                            </p>

                            <div className="bg-primary-pale border border-primary rounded-lg p-4">
                                <h3 className="text-sm font-semibold text-primary mb-3">
                                    Step-by-Step Instructions
                                </h3>
                                <ol className="space-y-3 text-sm text-primary">
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            1
                                        </span>
                                        <div>
                                            <p className="font-medium">Navigate to Agent Skills page</p>
                                            <p className="text-primary mt-1">
                                                Go to <strong>Resources | Agent Skills</strong> in the left side navigation
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            2
                                        </span>
                                        <div>
                                            <p className="font-medium">Create or Select a Skill</p>
                                            <p className="text-primary mt-1">
                                                Create a new skill with <strong>+ Create Skill</strong> or select an existing skill to edit
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            3
                                        </span>
                                        <div>
                                            <p className="font-medium">Assign Resource Groups</p>
                                            <p className="text-primary mt-1">
                                                In the skill form, use the <strong>Group Access</strong> tab to assign the
                                                skill to one or more resource groups
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            4
                                        </span>
                                        <div>
                                            <p className="font-medium">Save Changes</p>
                                            <p className="text-primary mt-1">
                                                Click <strong>Save</strong> to apply the group assignments
                                            </p>
                                        </div>
                                    </li>
                                </ol>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <div className="flex items-start space-x-2">
                                    <AlertCircle className="w-4 h-4 text-yellow-600 flex-shrink-0 mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium text-yellow-900">Important Note</p>
                                        <p className="text-sm text-yellow-800 mt-1">
                                            The skill will appear in this group's list after you save the changes.
                                            If the group inherits from parents, the skill will also automatically appear in all descendant groups.
                                            Skills must be in <strong>published</strong> status to be executed by end users.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center justify-end p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={() => setShowAddSkillsInfo(false)}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                            >
                                Got it
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GroupAgentSkillsTab;