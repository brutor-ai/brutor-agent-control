/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    X,
    Loader,
    AlertCircle,
    ChevronRight,
    ChevronDown,
    FolderSymlink,
    Building2,
    Building,
    Briefcase,
    Package,
    Users,
    FolderKanban,
    FolderTree,
    Target,
    AppWindow,
    Bot,
    Sparkles,
    Server,
    Folder,
    Home,
    Check,
    CornerDownRight
} from 'lucide-react';
import { ResourceGroup, GroupHierarchyTree as GroupTree, formatGroupLabel } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
    onMove: () => void;
    onClose: () => void;
}

const GROUP_TYPE_ICONS: Record<string, any> = {
    organization: Building2,
    division: Building,
    department: Briefcase,
    business_unit: Package,
    team: Users,
    project: FolderKanban,
    program: FolderTree,
    initiative: Target,
    application: AppWindow,
    agent: Bot,
    assistant: Sparkles,
    service: Server,
    custom: Folder
};

/**
 * Collect all descendant IDs of a group within the tree (including itself).
 * These are excluded as valid move targets to prevent circular references.
 */
const collectDescendantIds = (tree: GroupTree[], targetId: string): Set<string> => {
    const ids = new Set<string>();

    const findAndCollect = (nodes: GroupTree[]): boolean => {
        for (const node of nodes) {
            if (node.id === targetId) {
                // Found the target — collect it and all descendants
                const collectAll = (n: GroupTree) => {
                    ids.add(n.id);
                    n.children?.forEach(collectAll);
                };
                collectAll(node);
                return true;
            }
            if (node.children && findAndCollect(node.children)) {
                return true;
            }
        }
        return false;
    };

    findAndCollect(tree);
    return ids;
};

const MoveGroupModal: React.FC<Props> = ({ group, onMove, onClose }) => {
    const { fetchGroupHierarchyTree, updateResourceGroup } = useGateway();

    const [tree, setTree] = useState<GroupTree[]>([]);
    const [loading, setLoading] = useState(true);
    const [moving, setMoving] = useState(false);
    const [error, setError] = useState('');
    const [selectedParentId, setSelectedParentId] = useState<string | null>(
        group.parent_group_id || null
    );
    const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());

    // Load hierarchy tree
    useEffect(() => {
        const load = async () => {
            try {
                setLoading(true);
                const response = await fetchGroupHierarchyTree();
                setTree(response.tree);

                // Auto-expand ancestors so current location is visible
                if (group.ancestors && group.ancestors.length > 0) {
                    const ancestorIds = new Set(group.ancestors.map(a => a.id));
                    setExpandedNodes(ancestorIds);
                } else {
                    // Expand root nodes by default
                    const rootIds = new Set(response.tree.map((n: GroupTree) => n.id));
                    setExpandedNodes(rootIds);
                }
            } catch (err) {
                setError(errorMessage(err, 'Failed to load group hierarchy'));
            } finally {
                setLoading(false);
            }
        };
        runBackground(load(), 'load');
    }, []);

    // IDs that cannot be selected as targets (the group itself + all its descendants)
    const excludedIds = React.useMemo(() => {
        if (tree.length === 0) return new Set<string>();
        return collectDescendantIds(tree, group.id);
    }, [tree, group.id]);

    const isCurrentParent = selectedParentId === (group.parent_group_id || null);

    const handleMove = async () => {
        if (isCurrentParent) return;

        try {
            setMoving(true);
            setError('');

            await updateResourceGroup(group.id, {
                parent_group_id: selectedParentId
            });

            onMove();
        } catch (err) {
            console.error('Failed to move group:', errorKind(err));
            setError(errorMessage(err, 'Failed to move group'));
            setMoving(false);
        }
    };

    const toggleNode = (nodeId: string) => {
        setExpandedNodes(prev => {
            const next = new Set(prev);
            if (next.has(nodeId)) {
                next.delete(nodeId);
            } else {
                next.add(nodeId);
            }
            return next;
        });
    };

    // Get a friendly name for the selected target
    const getSelectedTargetName = useCallback((): string => {
        if (selectedParentId === null) return 'Root (top level)';

        const findName = (nodes: GroupTree[]): string | null => {
            for (const node of nodes) {
                if (node.id === selectedParentId) return formatGroupLabel(node);
                if (node.children) {
                    const found = findName(node.children);
                    if (found) return found;
                }
            }
            return null;
        };

        return findName(tree) || selectedParentId;
    }, [selectedParentId, tree]);

    const renderTreeNode = (node: GroupTree, level: number = 0) => {
        const isExcluded = excludedIds.has(node.id);
        const isSelected = selectedParentId === node.id;
        const isCurrentLoc = node.id === group.parent_group_id;
        const isExpanded = expandedNodes.has(node.id);
        const hasChildren = node.children && node.children.length > 0;
        const Icon = GROUP_TYPE_ICONS[node.group_type || 'custom'] || Folder;

        // Filter out excluded children but still render non-excluded ones
        const visibleChildren = node.children || [];

        return (
            <div key={node.id}>
                <button
                    onClick={() => {
                        if (!isExcluded) {
                            setSelectedParentId(node.id);
                        }
                    }}
                    disabled={isExcluded}
                    className={`
                        w-full flex items-center px-2 py-1.5 rounded-lg text-left transition-all text-sm
                        ${isExcluded
                            ? 'opacity-40 cursor-not-allowed'
                            : isSelected
                                ? 'bg-primary-50 text-primary-900 ring-1 ring-primary-300'
                                : 'hover:bg-surface-alt text-secondary'
                        }
                    `}
                    style={{ paddingLeft: `${(level * 20) + 8}px` }}
                >
                    {/* Expand/collapse */}
                    <div className="flex-shrink-0 w-5 h-5 mr-1">
                        {hasChildren ? (
                            <span
                                onClick={(e) => {
                                    e.stopPropagation();
                                    toggleNode(node.id);
                                }}
                                className="w-5 h-5 flex items-center justify-center rounded hover:bg-surface-inset cursor-pointer"
                            >
                                {isExpanded
                                    ? <ChevronDown className="w-4 h-4 text-muted" />
                                    : <ChevronRight className="w-4 h-4 text-muted" />
                                }
                            </span>
                        ) : (
                            <span className="w-5 h-5" />
                        )}
                    </div>

                    {/* Icon */}
                    <div className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center mr-2 bg-surface-raised text-secondary">
                        <Icon className="w-3.5 h-3.5" />
                    </div>

                    {/* Name */}
                    <span className="flex-1 truncate font-medium min-h-0 min-w-0">
                        {formatGroupLabel(node)}
                    </span>

                    {/* Badges */}
                    {isExcluded && node.id === group.id && (
                        <span className="ml-2 px-1.5 py-0.5 text-xs font-medium text-muted bg-surface-raised rounded">
                            This group
                        </span>
                    )}
                    {isExcluded && node.id !== group.id && (
                        <span className="ml-2 px-1.5 py-0.5 text-xs font-medium text-muted bg-surface-raised rounded">
                            Descendant
                        </span>
                    )}
                    {isCurrentLoc && !isExcluded && (
                        <span className="ml-2 px-1.5 py-0.5 text-xs font-medium text-primary bg-primary-pale rounded">
                            Current
                        </span>
                    )}
                    {isSelected && !isExcluded && (
                        <Check className="ml-2 w-4 h-4 text-primary-600 flex-shrink-0" />
                    )}
                </button>

                {/* Children */}
                {hasChildren && isExpanded && visibleChildren.map(child => renderTreeNode(child, level + 1))}
            </div>
        );
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-surface rounded-xl shadow-xl max-w-lg w-full mx-4 flex flex-col max-h-[80vh]">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center space-x-3">
                        <div className="w-9 h-9 rounded-lg bg-primary-100 flex items-center justify-center">
                            <FolderSymlink className="w-5 h-5 text-primary-700" />
                        </div>
                        <div>
                            <h3 className="text-lg font-semibold text-default">Move Group</h3>
                            <p className="text-sm text-muted">
                                Select a new parent for "<strong>{formatGroupLabel(group)}</strong>"
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Error */}
                {error && (
                    <div className="mx-6 mt-4 flex items-start space-x-3 p-3 bg-surface-alt border border-default rounded-lg">
                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                        <p className="text-sm text-red-700">{error}</p>
                    </div>
                )}

                {/* Tree Picker */}
                <div className="scroll-region px-6 py-4 space-y-1">
                    {loading ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader className="w-6 h-6 text-primary-600 animate-spin" />
                        </div>
                    ) : (
                        <>
                            {/* Root option */}
                            <button
                                onClick={() => setSelectedParentId(null)}
                                className={`
                                    w-full flex items-center px-3 py-2 rounded-lg text-left transition-all text-sm
                                    ${selectedParentId === null
                                        ? 'bg-primary-50 text-primary-900 ring-1 ring-primary-300'
                                        : 'hover:bg-surface-alt text-secondary'
                                    }
                                `}
                            >
                                <div className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center mr-2 bg-surface-raised text-secondary">
                                    <Home className="w-3.5 h-3.5" />
                                </div>
                                <span className="flex-1 font-medium min-h-0 min-w-0">Root (no parent)</span>
                                {!group.parent_group_id && (
                                    <span className="ml-2 px-1.5 py-0.5 text-xs font-medium text-primary bg-primary-pale rounded">
                                        Current
                                    </span>
                                )}
                                {selectedParentId === null && (
                                    <Check className="ml-2 w-4 h-4 text-primary-600 flex-shrink-0" />
                                )}
                            </button>

                            {/* Separator */}
                            <div className="border-t border-default my-2" />

                            {/* Tree nodes */}
                            {tree.map(node => renderTreeNode(node, 0))}
                        </>
                    )}
                </div>

                {/* Footer */}
                <div className="px-6 py-4 border-t border-default bg-surface-alt rounded-b-xl">
                    {/* Move summary */}
                    {!isCurrentParent && (
                        <div className="flex items-center space-x-2 mb-3 text-sm text-secondary">
                            <CornerDownRight className="w-4 h-4 text-muted flex-shrink-0" />
                            <span>
                                Move to: <strong className="text-default">{getSelectedTargetName()}</strong>
                            </span>
                        </div>
                    )}

                    <div className="flex items-center justify-end space-x-3">
                        <button
                            onClick={onClose}
                            disabled={moving}
                            className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={asyncHandler(handleMove, 'click')}
                            disabled={moving || isCurrentParent}
                            className="px-4 py-2 text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 flex items-center space-x-2"
                        >
                            <FolderSymlink className="w-4 h-4" />
                            <span>{moving ? 'Moving...' : 'Move Group'}</span>
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default MoveGroupModal;
