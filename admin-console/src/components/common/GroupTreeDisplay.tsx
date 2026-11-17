/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { ChevronRight } from 'lucide-react';
import { formatGroupLabel } from '../../lib/groupTypes';

interface GroupInfo {
    id: string;
    name: string;
    // API responses sometimes return `null` for these — accept both
    // so callers don't have to coerce. The render path already
    // tolerates nullish values.
    display_name?: string | null;
    group_type?: string | null;
    parent_group_id?: string | null;
}

interface ResourceGroupTreeDisplayProps {
    groups: GroupInfo[];
    variant?: 'compact' | 'expanded' | 'inline';
    maxVisible?: number;
    className?: string;
}

interface TreeNode {
    group: GroupInfo;
    children: TreeNode[];
}

/**
 * ResourceGroupTreeDisplay - Simple tree with indentation
 */
const GroupTreeDisplay: React.FC<ResourceGroupTreeDisplayProps> = ({
                                                                       groups,
                                                                       variant = 'compact',
                                                                       maxVisible = 3,
                                                                       className = ''
                                                                   }) => {
    const [showAll, setShowAll] = useState(false);

    // Build tree structure (hook must be called unconditionally)
    const tree = useMemo(() => {
        if (!groups || groups.length === 0) return [];

        const nodeMap = new Map<string, TreeNode>();
        const roots: TreeNode[] = [];

        // Create all nodes
        groups.forEach(group => {
            nodeMap.set(group.id, {
                group,
                children: []
            });
        });

        // Build hierarchy
        groups.forEach(group => {
            const node = nodeMap.get(group.id)!;

            if (group.parent_group_id && nodeMap.has(group.parent_group_id)) {
                const parent = nodeMap.get(group.parent_group_id)!;
                parent.children.push(node);
            } else {
                roots.push(node);
            }
        });

        return roots;
    }, [groups]);

    // Render tree node with indentation (like GroupHierarchyTree)
    const renderTree = (node: TreeNode, level: number = 0): React.ReactNode => {
        const displayName = formatGroupLabel(node.group);
        const hasChildren = node.children.length > 0;

        return (
            <div key={node.group.id}>
                {/* Node with indentation */}
                <div
                    className="flex items-center gap-1 py-0.5"
                    style={{ paddingLeft: `${level * 12}px` }}
                >
                    {/* Child indicator for non-root nodes */}
                    {level > 0 && (
                        <ChevronRight className="w-2.5 h-2.5 text-muted flex-shrink-0" />
                    )}

                    {/* Badge */}
                    <span
                        className={`inline-flex items-center px-1.5 py-0 rounded text-[10px] leading-tight whitespace-nowrap ${
                            level === 0
                                ? 'bg-sky-100 text-sky-800 font-medium'
                                : level === 1
                                    ? 'bg-sky-50 text-sky-700'
                                    : 'bg-surface-alt text-secondary'
                        }`}
                    >
                        {displayName}
                    </span>

                    {/* Child count */}
                    {hasChildren && (
                        <span className="text-[9px] text-muted">
                            ({node.children.length})
                        </span>
                    )}
                </div>

                {/* Recursively render children */}
                {hasChildren && (
                    <div>
                        {node.children.map(child => renderTree(child, level + 1))}
                    </div>
                )}
            </div>
        );
    };

    // Flatten for counting
    const flattenTree = (nodes: TreeNode[]): TreeNode[] => {
        const result: TreeNode[] = [];
        const visit = (node: TreeNode) => {
            result.push(node);
            node.children.forEach(visit);
        };
        nodes.forEach(visit);
        return result;
    };

    const allNodes = flattenTree(tree);

    // No groups = not assigned to any group
    if (!groups || groups.length === 0) {
        return (
            <span className="flex items-center text-xs text-red-500">
                No groups assigned
            </span>
        );
    }

    // Compact/Expanded variants
    if (variant === 'compact' || variant === 'expanded') {
        const visibleRoots = showAll ? tree : tree.slice(0, maxVisible);
        const visibleCount = flattenTree(visibleRoots).length;
        const remainingCount = allNodes.length - visibleCount;

        return (
            <div className={className}>
                <div className="space-y-0">
                    {visibleRoots.map(node => renderTree(node, 0))}
                </div>

                {remainingCount > 0 && !showAll && (
                    <button
                        type="button"
                        onClick={() => setShowAll(true)}
                        className="text-[10px] text-sky-600 hover:text-sky-700 mt-1"
                    >
                        +{remainingCount} more
                    </button>
                )}

                {showAll && tree.length > maxVisible && (
                    <button
                        type="button"
                        onClick={() => setShowAll(false)}
                        className="text-[10px] text-secondary hover:text-secondary mt-1"
                    >
                        Show less
                    </button>
                )}
            </div>
        );
    }

    // Inline variant - just badges
    if (variant === 'inline') {
        return (
            <div className={`flex flex-wrap gap-1 ${className}`}>
                {allNodes.slice(0, maxVisible).map(node => (
                    <span
                        key={node.group.id}
                        className="inline-flex items-center px-1.5 py-0 rounded text-[10px] leading-tight bg-sky-100 text-sky-800 font-medium"
                    >
                        {formatGroupLabel(node.group)}
                    </span>
                ))}
                {allNodes.length > maxVisible && (
                    <span className="text-[10px] text-muted">
                        +{allNodes.length - maxVisible} more
                    </span>
                )}
            </div>
        );
    }

    return null;
};

export default GroupTreeDisplay;