/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import {
    Edit,
    Trash2,
    MoreVertical,
    Sparkles,
    Copy,
    Globe,
    CheckCircle,
    ShieldCheck,
    Activity,
    Workflow,
    Eye,
    Archive,
} from 'lucide-react';
import {
    AgentSkillConfig,
    getSkillCategory,
    SKILL_STATUS_CONFIG
} from '../../lib/agentSkillTypes';
import { AgentSkillStatusBadge } from './AgentSkillStatusBadge';
import GroupTreeDisplay from '../common/GroupTreeDisplay';

interface AgentSkillListItemProps {
    skill: AgentSkillConfig;
    onEdit: () => void;
    onDelete: () => void;
    onCopy: () => void;
    onOpenTab: (tab: 'overview' | 'groups' | 'content' | 'resources' | 'scripts' | 'assets' | 'executions') => void;
    onPublish?: () => void;
    onValidate?: () => void;
    onArchive?: () => void;
    /** When true, render a leading selection checkbox (bulk-delete mode). */
    selectable?: boolean;
    selected?: boolean;
    onToggleSelect?: () => void;
}

const AgentSkillListItem: React.FC<AgentSkillListItemProps> = ({
    skill,
    onEdit,
    onDelete,
    onCopy,
    onOpenTab,
    onPublish,
    onValidate,
    onArchive,
    selectable = false,
    selected = false,
    onToggleSelect,
}) => {
    const [showMenu, setShowMenu] = useState(false);
    const [menuPosition, setMenuPosition] = useState<'bottom' | 'top'>('bottom');
    const menuButtonRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (showMenu && menuButtonRef.current) {
            const rect = menuButtonRef.current.getBoundingClientRect();
            const spaceBelow = window.innerHeight - rect.bottom;
            const spaceAbove = rect.top;
            if (spaceBelow < 200 && spaceAbove > spaceBelow) {
                setMenuPosition('top');
            } else {
                setMenuPosition('bottom');
            }
        }
    }, [showMenu]);

    const category = getSkillCategory(skill);
    const isPublished = skill.status === 'published';
    const approvalRequired = skill.metadata?.approval_required ?? false;
    const costEstimate = skill.metadata?.cost_estimate_usd;
    const isComposite = skill.metadata?.skill_type === 'composite';

    return (
        <div className={`relative border-b transition-all ${
            skill.enabled
                ? 'hover:bg-surface-alt border-default'
                : 'bg-surface-alt/50 border-default opacity-60'
        }`}>
            {/* Desktop layout */}
            <div className="flex items-center px-4 py-3">
                {/* Selection checkbox (bulk-delete mode) */}
                {selectable && (
                    <input
                        type="checkbox"
                        checked={selected}
                        onChange={onToggleSelect}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Select ${skill.name}`}
                        className="mr-3 h-4 w-4 flex-shrink-0 rounded border-strong text-primary-600 focus:ring-primary-500 cursor-pointer"
                    />
                )}
                {/* Skill Name + Description */}
                <div className="flex-1 min-w-0 flex items-center space-x-3 min-h-0">
                    <div className={`p-1.5 rounded-lg flex-shrink-0 ${SKILL_STATUS_CONFIG[skill.status]?.bgColor || 'bg-surface-raised'}`}>
                        <Sparkles className={`w-5 h-5 ${SKILL_STATUS_CONFIG[skill.status]?.color || 'text-secondary'}`} />
                    </div>
                    <div className="flex-1 min-w-0 min-h-0">
                        <div className="flex items-center space-x-2">
                            <button
                                onClick={() => onOpenTab('overview')}
                                className="font-medium text-default truncate hover:text-primary-600 transition-colors text-left"
                                title={skill.name}
                            >
                                {skill.name}
                            </button>
                            <span className="text-xs font-mono text-muted flex-shrink-0">
                                v{skill.version}
                            </span>
                            {isComposite && (
                                <span className="inline-flex items-center space-x-0.5 px-1.5 py-0.5 text-xs font-medium bg-primary-100 text-primary-700 rounded-full flex-shrink-0">
                                    <Workflow className="w-2.5 h-2.5" />
                                    <span>Composite</span>
                                </span>
                            )}
                        </div>
                        <div className="text-xs text-muted font-mono truncate" title={skill.id}>{skill.id}</div>
                        {skill.description && (
                            <p className="text-xs text-muted truncate mt-0.5 max-w-md">
                                {skill.description}
                            </p>
                        )}
                    </div>
                </div>

                {/* Category */}
                <div className="w-32 text-center flex-shrink-0 hidden lg:block">
                    <span className="text-xs text-secondary">{category}</span>
                </div>

                {/* Status */}
                <div className="w-28 text-center flex-shrink-0">
                    <AgentSkillStatusBadge status={skill.status} />
                </div>

                {/* Executions */}
                <div className="w-20 text-center flex-shrink-0 hidden lg:flex items-center justify-center space-x-1">
                    <Activity className="w-3 h-3 text-muted" />
                    <span className="text-xs text-secondary">{skill.execution_count.toLocaleString()}</span>
                </div>

                {/* Cost Estimate */}
                <div className="w-20 text-center flex-shrink-0 hidden xl:block">
                    {costEstimate != null ? (
                        <span className="text-xs text-secondary">${costEstimate.toFixed(2)}</span>
                    ) : (
                        <span className="text-xs text-muted">—</span>
                    )}
                </div>

                {/* Approval indicator */}
                <div className="w-16 text-center flex-shrink-0 hidden xl:block">
                    {approvalRequired ? (
                        <span title="Requires approval">
                            <ShieldCheck className="w-4 h-4 text-amber-500 mx-auto" />
                        </span>
                    ) : (
                        <span className="text-xs text-muted">Auto</span>
                    )}
                </div>

                {/* Group Access */}
                <div className="min-w-[200px] max-w-[280px] flex-shrink-0 mx-3 hidden lg:block">
                    <GroupTreeDisplay
                        groups={skill.groups || []}
                        variant="compact"
                        maxVisible={3}
                    />
                </div>

                {/* Actions Menu */}
                <div className="w-10 flex-shrink-0 text-right" ref={menuButtonRef}>
                    <div className="relative inline-block">
                        <button
                            onClick={() => setShowMenu(!showMenu)}
                            className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        >
                            <MoreVertical className="w-4 h-4" />
                        </button>

                        {showMenu && (
                            <>
                                <div
                                    className="fixed inset-0 z-10"
                                    onClick={() => setShowMenu(false)}
                                />
                                <div className={`absolute right-0 w-44 bg-surface rounded-lg shadow-lg border border-default py-1 z-20 ${
                                    menuPosition === 'top' ? 'bottom-full mb-1' : 'top-full mt-1'
                                }`}>
                                    {onValidate && (
                                        <button onClick={() => { onValidate(); setShowMenu(false); }}
                                            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <CheckCircle className="w-4 h-4" /><span>Validate</span>
                                        </button>
                                    )}
                                    {onPublish && (
                                        <button onClick={() => { onPublish(); setShowMenu(false); }}
                                            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 text-green-700">
                                            <Globe className="w-4 h-4" /><span>Publish</span>
                                        </button>
                                    )}
                                    {onArchive && (
                                        <button onClick={() => { onArchive(); setShowMenu(false); }}
                                            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Archive className="w-4 h-4" /><span>Archive</span>
                                        </button>
                                    )}

                                    <div className="border-t border-default my-1" />

                                    {/* View — only for published skills; drafts use Edit to open
                                        the same panel and get write access. */}
                                    {isPublished && (
                                        <button onClick={() => { onOpenTab('overview'); setShowMenu(false); }}
                                            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Eye className="w-4 h-4" /><span>View</span>
                                        </button>
                                    )}

                                    <div className="border-t border-default my-1" />

                                    {!isPublished && (
                                        <button onClick={() => { onEdit(); setShowMenu(false); }}
                                            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Edit className="w-4 h-4" /><span>Edit</span>
                                        </button>
                                    )}
                                    <button onClick={() => { onCopy(); setShowMenu(false); }}
                                        className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                        <Copy className="w-4 h-4" /><span>Duplicate</span>
                                    </button>

                                    <div className="border-t border-default my-1" />

                                    <button onClick={() => { onDelete(); setShowMenu(false); }}
                                        className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 text-red-600">
                                        <Trash2 className="w-4 h-4" /><span>Delete</span>
                                    </button>
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default AgentSkillListItem;
