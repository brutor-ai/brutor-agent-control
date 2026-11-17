/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ParentGroupSelector - Single-select hierarchical group selector
 * Used for selecting a parent group when creating/editing groups
 */

import React, { useState } from 'react';
import { ChevronRight, ChevronDown, Users, Circle, CheckCircle, AlertCircle } from 'lucide-react';
import { GroupHierarchyTree, formatGroupLabel } from '../../lib/groupTypes';

interface ParentGroupSelectorProps {
    groupTree: GroupHierarchyTree[];
    selectedGroupId: string | null;
    onSelectionChange: (selectedId: string | null) => void;
    excludeGroupId?: string; // Exclude this group and its descendants (can't be own parent)
    className?: string;
}

interface TreeNodeProps {
    group: GroupHierarchyTree;
    selectedGroupId: string | null;
    onSelect: (groupId: string) => void;
    level: number;
    isExcluded: boolean;
    isDescendantOfExcluded: boolean;
}

const TreeNode: React.FC<TreeNodeProps> = ({
                                               group,
                                               selectedGroupId,
                                               onSelect,
                                               level,
                                               isExcluded,
                                               isDescendantOfExcluded
                                           }) => {
    const [isExpanded, setIsExpanded] = useState(true);
    const hasChildren = group.children && group.children.length > 0;
    const isSelected = selectedGroupId === group.id;
    const isDisabled = isExcluded || isDescendantOfExcluded;

    const handleSelect = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (!isDisabled) {
            onSelect(group.id);
        }
    };

    const handleExpand = (e: React.MouseEvent) => {
        e.stopPropagation();
        setIsExpanded(!isExpanded);
    };

    return (
        <div className="select-none">
            <div
                className={`
                    flex items-center space-x-2 py-1.5 px-2 rounded transition-colors
                    ${!isDisabled ? 'hover:bg-surface-alt cursor-pointer' : 'opacity-50 cursor-not-allowed'}
                    ${isSelected ? 'bg-primary-50' : ''}
                `}
                style={{ paddingLeft: `${level * 1.5}rem` }}
                onClick={handleSelect}
            >
                {/* Expand/Collapse Button */}
                {hasChildren ? (
                    <button
                        onClick={handleExpand}
                        disabled={isDisabled}
                        className="flex-shrink-0 p-0.5 hover:bg-surface-inset rounded transition-colors disabled:cursor-not-allowed"
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

                {/* Selection Indicator */}
                {isDisabled ? (
                    <AlertCircle className="w-5 h-5 text-muted flex-shrink-0" />
                ) : isSelected ? (
                    <CheckCircle className="w-5 h-5 text-primary-600 flex-shrink-0" />
                ) : (
                    <Circle className="w-5 h-5 text-muted flex-shrink-0" />
                )}

                {/* Group Icon */}
                <Users className={`w-4 h-4 flex-shrink-0 ${
                    isSelected ? 'text-primary-600' : 'text-muted'
                }`} />

                {/* Group Name */}
                <span className={`text-sm flex-1 min-h-0 min-w-0 ${
                    isSelected ? 'text-primary-900 font-medium' : 'text-secondary'
                }`}>
                    {formatGroupLabel(group)}
                </span>

                {/* Children Count Badge */}
                {hasChildren && (
                    <span className="text-xs text-muted bg-surface-raised px-2 py-0.5 rounded">
                        {group.children.length}
                    </span>
                )}

                {/* Excluded Indicator */}
                {isExcluded && (
                    <span className="text-xs text-muted italic">
                        (current group)
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
                            selectedGroupId={selectedGroupId}
                            onSelect={onSelect}
                            level={level + 1}
                            isExcluded={false}
                            isDescendantOfExcluded={isExcluded || isDescendantOfExcluded}
                        />
                    ))}
                </div>
            )}
        </div>
    );
};

const ParentGroupSelector: React.FC<ParentGroupSelectorProps> = ({
                                                                     groupTree,
                                                                     selectedGroupId,
                                                                     onSelectionChange,
                                                                     excludeGroupId,
                                                                     className = ''
                                                                 }) => {
    // Filter active groups and mark excluded group
    const processedTree = groupTree.filter(g => g.is_active);

    const handleSelect = (groupId: string) => {
        if (selectedGroupId === groupId) {
            // Deselect if clicking the same group
            onSelectionChange(null);
        } else {
            onSelectionChange(groupId);
        }
    };

    const handleClear = () => {
        onSelectionChange(null);
    };

    return (
        <div className={`border border-strong rounded-md ${className}`}>
            {/* Header */}
            <div className="px-3 py-2 border-b border-default bg-surface-alt flex items-center justify-between">
                <span className="text-xs font-medium text-secondary">
                    {selectedGroupId ? '1 group selected' : 'No parent selected (root level)'}
                </span>
                {selectedGroupId && (
                    <button
                        type="button"
                        onClick={handleClear}
                        className="text-xs text-secondary hover:text-secondary font-medium"
                    >
                        Clear Selection
                    </button>
                )}
            </div>

            {/* Tree View */}
            <div className="p-2 max-h-64 overflow-y-auto">
                {processedTree.length === 0 ? (
                    <div className="text-center py-8">
                        <Users className="w-10 h-10 text-disabled mx-auto mb-2" />
                        <p className="text-sm text-muted">No groups available</p>
                    </div>
                ) : (
                    <div className="space-y-0.5">
                        {processedTree.map(group => (
                            <TreeNode
                                key={group.id}
                                group={group}
                                selectedGroupId={selectedGroupId}
                                onSelect={handleSelect}
                                level={0}
                                isExcluded={group.id === excludeGroupId}
                                isDescendantOfExcluded={false}
                            />
                        ))}
                    </div>
                )}
            </div>

            {/* Footer Info */}
            <div className="px-3 py-2 border-t border-default bg-surface-alt">
                <p className="text-xs text-muted">
                    💡 Select a parent group or leave empty to create a root-level group
                </p>
            </div>
        </div>
    );
};

export default ParentGroupSelector;