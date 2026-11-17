/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { ChevronRight, ChevronDown, Users, CheckSquare, Square, MinusSquare } from 'lucide-react';
import { GroupHierarchyTree, formatGroupLabel } from '../../lib/groupTypes';

interface HierarchicalGroupSelectorProps {
    groupTree: GroupHierarchyTree[];
    selectedGroupIds: string[];
    onSelectionChange: (selectedIds: string[]) => void;
    className?: string;
}

interface TreeNodeProps {
    group: GroupHierarchyTree;
    selectedGroupIds: string[];
    onToggle: (groupId: string) => void;
    level: number;
    getSelectionState: (group: GroupHierarchyTree) => 'all' | 'some' | 'none';
    autoSelectDescendants: boolean;
}

const TreeNode: React.FC<TreeNodeProps> = ({
                                               group,
                                               selectedGroupIds,
                                               onToggle,
                                               level,
                                               getSelectionState,
                                               autoSelectDescendants
                                           }) => {
    const [isExpanded, setIsExpanded] = useState(true);
    const hasChildren = group.children && group.children.length > 0;
    const selectionState = getSelectionState(group);

    const handleToggle = (e: React.MouseEvent) => {
        e.stopPropagation();
        onToggle(group.id);
    };

    const handleExpand = (e: React.MouseEvent) => {
        e.stopPropagation();
        setIsExpanded(!isExpanded);
    };

    // Determine checkbox icon based on selection state
    const CheckboxIcon = selectionState === 'all'
        ? CheckSquare
        : selectionState === 'some'
            ? MinusSquare
            : Square;

    const checkboxColor = selectionState === 'all'
        ? 'text-primary-600'
        : selectionState === 'some'
            ? 'text-primary-400'
            : 'text-muted';

    return (
        <div className="select-none">
            <div
                className={`flex items-center space-x-2 py-1.5 px-2 rounded hover:bg-surface-alt cursor-pointer transition-colors`}
                style={{ paddingLeft: `${level * 1.5}rem` }}
            >
                {/* Expand/Collapse Button */}
                {hasChildren ? (
                    <button
                        type="button"
                        onClick={handleExpand}
                        className="flex-shrink-0 p-0.5 hover:bg-surface-inset rounded transition-colors"
                    >
                        {isExpanded ? (
                            <ChevronDown className="w-4 h-4 text-secondary" />
                        ) : (
                            <ChevronRight className="w-4 h-4 text-secondary" />
                        )}
                    </button>
                ) : (
                    <div className="w-5" />
                )}

                {/* Checkbox */}
                <button
                    type="button"
                    onClick={handleToggle}
                    className={`flex-shrink-0 ${checkboxColor} hover:opacity-80 transition-opacity`}
                >
                    <CheckboxIcon className="w-5 h-5" />
                </button>

                {/* Group Icon */}
                <Users className={`w-4 h-4 flex-shrink-0 ${
                    selectionState === 'all' || selectionState === 'some'
                        ? 'text-primary-600'
                        : 'text-muted'
                }`} />

                {/* Group Name */}
                <span
                    onClick={handleToggle}
                    className={`text-sm flex-1 min-h-0 min-w-0 ${
                        selectionState === 'all' || selectionState === 'some'
                            ? 'text-default font-medium'
                            : 'text-secondary'
                    }`}
                >
                    {formatGroupLabel(group)}
                </span>

                {/* Children Count Badge */}
                {hasChildren && (
                    <span className="text-xs text-muted bg-surface-raised px-2 py-0.5 rounded">
                        {group.children.length}
                    </span>
                )}
            </div>

            {/* Children */}
            {hasChildren && isExpanded && (
                <div>
                    {group.children.map((child) => (
                        <TreeNode
                            key={child.id}
                            group={child}
                            selectedGroupIds={selectedGroupIds}
                            onToggle={onToggle}
                            level={level + 1}
                            getSelectionState={getSelectionState}
                            autoSelectDescendants={autoSelectDescendants}
                        />
                    ))}
                </div>
            )}
        </div>
    );
};

const HierarchicalGroupSelector: React.FC<HierarchicalGroupSelectorProps> = ({
                                                                                 groupTree,
                                                                                 selectedGroupIds,
                                                                                 onSelectionChange,
                                                                                 className = ''
                                                                             }) => {
    // Auto-select descendants option (disabled by default)
    const [autoSelectDescendants, setAutoSelectDescendants] = useState(false);

    // Flatten tree to get all groups for selection operations
    const allGroups = useMemo(() => {
        const flatten = (nodes: GroupHierarchyTree[]): GroupHierarchyTree[] => {
            const result: GroupHierarchyTree[] = [];
            for (const node of nodes) {
                result.push(node);
                if (node.children && node.children.length > 0) {
                    result.push(...flatten(node.children));
                }
            }
            return result;
        };
        return flatten(groupTree || []);
    }, [groupTree]);

    // Filter only active groups for the tree display
    const activeTree = useMemo(() => {
        const filterActive = (nodes: GroupHierarchyTree[]): GroupHierarchyTree[] => {
            return nodes
                .filter(node => node.is_active)
                .map(node => ({
                    ...node,
                    children: node.children ? filterActive(node.children) : []
                }));
        };
        return filterActive(groupTree || []);
    }, [groupTree]);

    // Get all descendant IDs for a group
    const getDescendantIds = (group: GroupHierarchyTree): string[] => {
        let ids: string[] = [group.id];
        if (group.children) {
            group.children.forEach(child => {
                ids = ids.concat(getDescendantIds(child));
            });
        }
        return ids;
    };

    // Determine selection state (all selected, some selected, none selected)
    const getSelectionState = (group: GroupHierarchyTree): 'all' | 'some' | 'none' => {
        if (autoSelectDescendants) {
            // When auto-select is enabled, check if all descendants are selected
            const descendantIds = getDescendantIds(group);
            const selectedCount = descendantIds.filter(id => selectedGroupIds.includes(id)).length;

            if (selectedCount === descendantIds.length) return 'all';
            if (selectedCount > 0) return 'some';
            return 'none';
        } else {
            // When auto-select is disabled, only check this specific group
            const isSelected = selectedGroupIds.includes(group.id);

            // Check if any children are selected (for "some" state)
            if (group.children && group.children.length > 0) {
                const anyChildSelected = group.children.some(child => {
                    return getSelectionState(child) !== 'none';
                });

                if (isSelected && anyChildSelected) return 'all';
                if (isSelected || anyChildSelected) return 'some';
            }

            return isSelected ? 'all' : 'none';
        }
    };

    // Handle group toggle
    const handleToggle = (groupId: string) => {
        const group = allGroups.find(g => g.id === groupId);
        if (!group) return;

        let newSelectedIds: string[];

        if (autoSelectDescendants) {
            // Select/deselect the group AND all its descendants
            const descendantIds = getDescendantIds(group);
            const allSelected = descendantIds.every(id => selectedGroupIds.includes(id));

            if (allSelected) {
                // Deselect this group and all descendants
                newSelectedIds = selectedGroupIds.filter(id => !descendantIds.includes(id));
            } else {
                // Select this group and all descendants
                const idsToAdd = descendantIds.filter(id => !selectedGroupIds.includes(id));
                newSelectedIds = [...selectedGroupIds, ...idsToAdd];
            }
        } else {
            // Select/deselect ONLY this specific group
            const isSelected = selectedGroupIds.includes(groupId);

            if (isSelected) {
                // Deselect only this group
                newSelectedIds = selectedGroupIds.filter(id => id !== groupId);
            } else {
                // Select only this group
                newSelectedIds = [...selectedGroupIds, groupId];
            }
        }

        onSelectionChange(newSelectedIds);
    };

    // Handle select all / deselect all
    const handleSelectAll = () => {
        const allIds = allGroups.filter(g => g.is_active).map(g => g.id);
        onSelectionChange(allIds);
    };

    const handleDeselectAll = () => {
        onSelectionChange([]);
    };

    const allSelected = allGroups.length > 0 &&
        allGroups.filter(g => g.is_active).every(g => selectedGroupIds.includes(g.id));

    return (
        <div className={`border border-strong rounded-md ${className}`}>
            {/* Header with Select All / Deselect All */}
            <div className="px-3 py-2 border-b border-default bg-surface-alt flex items-center justify-between">
                <span className="text-xs font-medium text-secondary">
                    {selectedGroupIds.length} group{selectedGroupIds.length !== 1 ? 's' : ''} selected
                </span>
                <div className="flex items-center space-x-2">
                    <button
                        type="button"
                        onClick={handleSelectAll}
                        className="text-xs text-primary-600 hover:text-primary-700 font-medium"
                        disabled={allSelected}
                    >
                        Select All
                    </button>
                    <span className="text-disabled">|</span>
                    <button
                        type="button"
                        onClick={handleDeselectAll}
                        className="text-xs text-secondary hover:text-secondary font-medium"
                        disabled={selectedGroupIds.length === 0}
                    >
                        Clear
                    </button>
                </div>
            </div>

            {/* Auto-select descendants option */}
            <div className="px-3 py-2 border-b border-default bg-blue-50">
                <label className="flex items-center space-x-2 cursor-pointer">
                    <input
                        type="checkbox"
                        checked={autoSelectDescendants}
                        onChange={(e) => setAutoSelectDescendants(e.target.checked)}
                        className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                    />
                    <div className="flex-1 min-h-0 min-w-0">
                        <span className="text-xs font-medium text-secondary">
                            Auto-select descendants
                        </span>
                        <p className="text-xs text-muted mt-0.5">
                            When enabled, selecting a parent automatically selects all child groups
                        </p>
                    </div>
                </label>
            </div>

            {/* Tree View */}
            <div className="p-2 max-h-64 overflow-y-auto">
                {activeTree.length === 0 ? (
                    <div className="text-center py-8">
                        <Users className="w-10 h-10 text-disabled mx-auto mb-2" />
                        <p className="text-sm text-muted">No active user groups available</p>
                    </div>
                ) : (
                    <div className="space-y-0.5">
                        {activeTree.map(group => (
                            <TreeNode
                                key={group.id}
                                group={group}
                                selectedGroupIds={selectedGroupIds}
                                onToggle={handleToggle}
                                level={0}
                                getSelectionState={getSelectionState}
                                autoSelectDescendants={autoSelectDescendants}
                            />
                        ))}
                    </div>
                )}
            </div>

            {/* Footer Info */}
            <div className="px-3 py-2 border-t border-default bg-surface-alt">
                <p className="text-xs text-muted">
                    {autoSelectDescendants
                        ? '💡 Selecting a parent group automatically includes all child groups'
                        : '💡 Select individual groups as needed. Enable "Auto-select descendants" to select entire hierarchies.'
                    }
                </p>
            </div>
        </div>
    );
};

export default HierarchicalGroupSelector;