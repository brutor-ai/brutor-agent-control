/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    X,
    AlertCircle,
    Loader
} from 'lucide-react';
import {
    GroupType,
    GROUP_TYPE_LABELS,
    GroupHierarchyTree,
    SystemKind,
    SYSTEM_KIND_LABELS,
    SYSTEM_KIND_DESCRIPTIONS,
} from '../../lib/groupTypes';
import { useGateway } from '../../hooks/useGateway';
import { resolveGroupIcon } from '../../lib/lucideIconMap';
import LucideIconPicker from '../common/LucideIconPicker';
import IconColorPicker from '../common/IconColorPicker';
import ParentGroupSelector from '../common/ParentGroupSelector';
import { errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface Props {
    onClose: () => void;
    onSuccess: () => void;
}

/** The organisational-unit subtypes behind the "Organizational Unit"
 *  category. Order = org-chart depth, roughly. */
const ORGANIZING_TYPES: GroupType[] = [
    GroupType.ORGANIZATION,
    GroupType.DIVISION,
    GroupType.DEPARTMENT,
    GroupType.BUSINESS_UNIT,
    GroupType.TEAM,
    GroupType.PROJECT,
    GroupType.PROGRAM,
    GroupType.INITIATIVE,
];

const CreateGroupModal: React.FC<Props> = ({ onClose, onSuccess }) => {
    const { createResourceGroup, fetchGroupHierarchyTree } = useGateway();

    const [isCreating, setIsCreating] = useState(false);
    const [error, setError] = useState('');
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);
    const [loadingGroups, setLoadingGroups] = useState(true);
    const [readFailure, reads] = useReadFailures<'groups'>();

    const [formData, setFormData] = useState({
        name: '',
        display_name: '',
        description: '',
        group_type: GroupType.CUSTOM,
        // Only sent when group_type is AI_SYSTEM. Seeded to
        // AGENT so the AI-System path has a valid default the moment the
        // operator picks it, matching the control plane's own default.
        system_kind: SystemKind.AGENT,
        parent_group_id: null as string | null,
        is_active: true,
        inherit_resources: true,
        icon: '',
        color: ''
    });

    const isAiSystem = formData.group_type === GroupType.AI_SYSTEM;

    useEffect(() => {
        runBackground(loadGroups(), 'groups');
    }, []);

    const loadGroups = async () => {
        try {
            setLoadingGroups(true);
            const response = await fetchGroupHierarchyTree();
            setGroupTree(response.tree || []);
            reads.loaded('groups');
        } catch (err) {
            setGroupTree([]);
            reads.lost('groups', 'Failed to load the resource groups; no parent can be chosen', err);
        } finally {
            setLoadingGroups(false);
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();

        if (!formData.name.trim()) {
            setError('Name is required');
            return;
        }

        try {
            setIsCreating(true);
            setError('');

            await createResourceGroup({
                name: formData.name.trim(),
                display_name: formData.display_name.trim() || undefined,
                description: formData.description.trim() || undefined,
                group_type: formData.group_type,
                // Sent only for AI Systems — the control plane rejects a kind
                // on any other group type with a 422.
                system_kind: isAiSystem ? formData.system_kind : undefined,
                parent_group_id: formData.parent_group_id || undefined,
                is_active: formData.is_active,
                inherit_resources: formData.inherit_resources,
                icon: formData.icon || undefined,
                color: formData.color || undefined
            });

            onSuccess();
        } catch (err) {
            console.error('Failed to create group:', errorKind(err));
            setError(errorMessage(err, 'Failed to create group'));
            setIsCreating(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
                {/* Header */}
                <div className="flex items-center justify-between p-6 border-b border-default">
                    <h2 className="text-xl font-semibold text-default">
                        Create New Group
                    </h2>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Form */}
                <form onSubmit={asyncHandler(handleSubmit, 'submit')} className="p-6 space-y-6">
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

                    {/* Group Type — a two-step choice. The flat ten-type grid
                        mixed two different questions: "where does this sit in
                        the org chart?" and "what IS this thing?". The category
                        answers the second; the subtype (organizational units
                        only) answers the first. Storage is unchanged — this is
                        purely how group_type gets picked. Nothing requires a
                        tenant to use AI Systems at all: an org tree with teams
                        and bound models is a fully supported way to run. */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Group Type *
                        </label>
                        <div className="grid grid-cols-3 gap-3" data-testid="group-category-picker">
                            {([
                                {
                                    key: 'organizing',
                                    label: 'Organizational Unit',
                                    hint: 'Structure: org, department, team…',
                                    active: ORGANIZING_TYPES.includes(formData.group_type),
                                    pick: () => setFormData({
                                        ...formData,
                                        group_type: ORGANIZING_TYPES.includes(formData.group_type)
                                            ? formData.group_type
                                            : GroupType.TEAM,
                                    }),
                                },
                                {
                                    key: 'ai_system',
                                    label: 'AI System',
                                    hint: 'A governed system with a lifecycle',
                                    active: formData.group_type === GroupType.AI_SYSTEM,
                                    pick: () => setFormData({ ...formData, group_type: GroupType.AI_SYSTEM }),
                                },
                                {
                                    key: 'custom',
                                    label: 'Custom',
                                    hint: 'Anything else',
                                    active: formData.group_type === GroupType.CUSTOM,
                                    pick: () => setFormData({ ...formData, group_type: GroupType.CUSTOM }),
                                },
                            ] as const).map((cat) => (
                                <button
                                    key={cat.key}
                                    type="button"
                                    onClick={cat.pick}
                                    className={`
                                        flex flex-col items-start p-3 rounded-lg border-2 transition-all text-left
                                        ${cat.active
                                        ? 'border-primary-600 bg-primary-50'
                                        : 'border-default hover:border-strong bg-surface'
                                    }
                                    `}
                                >
                                    <span className={`text-sm font-medium ${cat.active ? 'text-primary-900' : 'text-secondary'}`}>
                                        {cat.label}
                                    </span>
                                    <span className="text-xs text-muted mt-0.5">{cat.hint}</span>
                                </button>
                            ))}
                        </div>

                        {/* Organizational subtype — only when organizing. */}
                        {ORGANIZING_TYPES.includes(formData.group_type) && (
                            <div className="mt-3" data-testid="organizing-subtype-picker">
                                <label className="block text-xs font-medium text-muted mb-2">
                                    Organizational level
                                </label>
                                <div className="grid grid-cols-4 gap-2">
                                    {ORGANIZING_TYPES.map((type) => {
                                        const Icon = resolveGroupIcon(undefined, type, undefined);
                                        const isSelected = formData.group_type === type;
                                        return (
                                            <button
                                                key={type}
                                                type="button"
                                                onClick={() => setFormData({ ...formData, group_type: type })}
                                                className={`
                                                    flex items-center space-x-2 p-2 rounded-lg border transition-all
                                                    ${isSelected
                                                    ? 'border-primary-600 bg-primary-50'
                                                    : 'border-default hover:border-strong bg-surface'
                                                }
                                                `}
                                            >
                                                <Icon className={`w-4 h-4 ${isSelected ? 'text-primary-600' : 'text-secondary'}`} />
                                                <span className={`text-xs font-medium ${isSelected ? 'text-primary-900' : 'text-secondary'}`}>
                                                    {GROUP_TYPE_LABELS[type]}
                                                </span>
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        )}
                    </div>

                    {/* AI System kind. Only meaningful for AI
                        Systems, so it appears only when one is selected rather
                        than sitting greyed out for every other group type. */}
                    {isAiSystem && (
                        <div data-testid="system-kind-picker">
                            <label className="block text-sm font-medium text-secondary mb-2">
                                AI System Kind *
                            </label>
                            <div className="grid grid-cols-2 gap-3">
                                {Object.values(SystemKind).map((kind) => {
                                    const Icon = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, kind);
                                    const isSelected = formData.system_kind === kind;

                                    return (
                                        <button
                                            key={kind}
                                            type="button"
                                            onClick={() => setFormData({ ...formData, system_kind: kind })}
                                            title={SYSTEM_KIND_DESCRIPTIONS[kind]}
                                            className={`
                                                flex items-start space-x-3 p-3 rounded-lg border-2 text-left transition-all
                                                ${isSelected
                                                ? 'border-primary-600 bg-primary-50'
                                                : 'border-default hover:border-strong bg-surface'
                                            }
                                            `}
                                        >
                                            <Icon className={`w-5 h-5 mt-0.5 shrink-0 ${isSelected ? 'text-primary-600' : 'text-secondary'}`} />
                                            <span className="min-w-0">
                                                <span className={`block text-sm font-medium ${isSelected ? 'text-primary-900' : 'text-secondary'}`}>
                                                    {SYSTEM_KIND_LABELS[kind]}
                                                </span>
                                                <span className="block text-xs text-muted mt-0.5">
                                                    {SYSTEM_KIND_DESCRIPTIONS[kind]}
                                                </span>
                                            </span>
                                        </button>
                                    );
                                })}
                            </div>
                            <p className="text-sm text-muted mt-2">
                                The kind decides which lifecycle signals are monitored for this
                                system. It can be changed later.
                            </p>
                        </div>
                    )}

                    {/* Custom Icon */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Icon Override
                        </label>
                        <LucideIconPicker
                            value={formData.icon}
                            onChange={(iconName) => setFormData({ ...formData, icon: iconName })}
                            groupType={formData.group_type}
                        />
                        <p className="text-sm text-muted mt-1">
                            Optionally override the default group type icon
                        </p>
                    </div>

                    {/* Icon Color */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Icon Color
                        </label>
                        <IconColorPicker
                            value={formData.color}
                            onChange={(colorKey) => setFormData({ ...formData, color: colorKey })}
                        />
                        <p className="text-sm text-muted mt-1">
                            Choose a color for the group icon badge
                        </p>
                    </div>

                    {/* Name */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Name *
                        </label>
                        <input
                            type="text"
                            value={formData.name}
                            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                            className="w-full px-4 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            placeholder="e.g., Engineering, Frontend Team, Customer Portal"
                            required
                        />
                        <p className="text-sm text-muted mt-1">
                            Internal identifier for the group
                        </p>
                    </div>

                    {/* Display Name */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Display Name
                        </label>
                        <input
                            type="text"
                            value={formData.display_name}
                            onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                            className="w-full px-4 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            placeholder="Optional friendly name"
                        />
                        <p className="text-sm text-muted mt-1">
                            User-friendly name shown in UI (optional)
                        </p>
                    </div>

                    {/* Description */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Description
                        </label>
                        <textarea
                            value={formData.description}
                            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                            rows={3}
                            className="w-full px-4 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            placeholder="Optional description of the group's purpose"
                        />
                    </div>

                    {/* Parent Group - Hierarchical Selector */}
                    <div>
                        <label className="block text-sm font-medium text-secondary mb-2">
                            Parent Group
                        </label>
                        {loadingGroups ? (
                            <div className="flex items-center justify-center p-8 border border-strong rounded-md bg-surface-alt">
                                <Loader className="w-6 h-6 animate-spin text-primary-600 mr-2" />
                                <span className="text-sm text-secondary">Loading groups...</span>
                            </div>
                        ) : readFailure('groups') ? (
                            <ReadFailureNotice compact failure={readFailure('groups')} onRetry={() => runBackground(loadGroups(), 'groups')} />
                        ) : (
                            <ParentGroupSelector
                                groupTree={groupTree}
                                selectedGroupId={formData.parent_group_id}
                                onSelectionChange={(groupId) =>
                                    setFormData({ ...formData, parent_group_id: groupId })
                                }
                            />
                        )}
                        <p className="text-sm text-muted mt-2">
                            🌳 Select a parent to create a nested group, or leave empty for a root-level group
                        </p>
                    </div>

                    {/* Active Status */}
                    <div>
                        <label className="flex items-center space-x-3 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={formData.is_active}
                                onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                                className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                            />
                            <div>
                                <span className="text-sm font-medium text-secondary">
                                    Active
                                </span>
                                <p className="text-sm text-muted">
                                    Inactive groups won't be available for assignment
                                </p>
                            </div>
                        </label>
                    </div>

                    {/* Inherit from Parent */}
                    {formData.parent_group_id && (
                        <div className="bg-primary-pale border border-primary rounded-lg p-4">
                            <label className="flex items-start space-x-3 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={formData.inherit_resources}
                                    onChange={(e) => setFormData({ ...formData, inherit_resources: e.target.checked })}
                                    className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500 mt-0.5"
                                />
                                <div className="flex-1 min-h-0 min-w-0">
                                    <span className="text-sm font-medium text-default">
                                        Inherit resources from parent
                                    </span>
                                    <p className="text-sm text-secondary mt-1">
                                        When enabled, this group inherits LLM models, MCP servers, and agent skills from parent groups.
                                        When disabled, this group will only use its directly assigned resources.
                                    </p>
                                    {!formData.inherit_resources && (
                                        <div className="mt-2 p-2 bg-surface-alt border border-default rounded text-xs text-amber-800">
                                            ⚠️ Warning: Disabling resource inheritance means this group won't have access to parent LLMs, MCPs, or skills.
                                        </div>
                                    )}
                                </div>
                            </label>
                        </div>
                    )}

                    {/* Actions */}
                    <div className="flex items-center justify-end space-x-3 pt-4 border-t border-default">
                        <button
                            type="button"
                            onClick={onClose}
                            disabled={isCreating}
                            className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={isCreating || !formData.name.trim()}
                            className="px-4 py-2 text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 flex items-center space-x-2"
                        >
                            {isCreating && <Loader className="w-4 h-4 animate-spin" />}
                            <span>{isCreating ? 'Creating...' : 'Create Group'}</span>
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
};

export default CreateGroupModal;