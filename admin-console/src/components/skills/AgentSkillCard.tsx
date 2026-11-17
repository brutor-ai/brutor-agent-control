/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
    Edit,
    Trash2,
    MoreVertical,
    Sparkles,
    Tag,
    Activity,
    Copy,
    CheckCircle,
    Globe,
    DollarSign,
    ShieldCheck,
    FileCode,
    Layers,
    Workflow,
    Eye,
    Archive,
} from 'lucide-react';
import { AgentSkillConfig, getSkillCategory, getSkillTags, SKILL_STATUS_CONFIG } from '../../lib/agentSkillTypes';
import { AgentSkillStatusBadge } from './AgentSkillStatusBadge';
import GroupTreeDisplay from '../common/GroupTreeDisplay';

interface AgentSkillCardProps {
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

const AgentSkillCard: React.FC<AgentSkillCardProps> = ({
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

    const category = getSkillCategory(skill);
    const tags = getSkillTags(skill);
    const statusConfig = SKILL_STATUS_CONFIG[skill.status] || SKILL_STATUS_CONFIG.draft;
    const isPublished = skill.status === 'published';
    const approvalRequired = skill.metadata?.approval_required ?? false;
    const costEstimate = skill.metadata?.cost_estimate_usd;
    const isComposite = skill.metadata?.skill_type === 'composite';

    return (
        <div className={`relative rounded-lg border-2 transition-all hover:shadow-md ${
            selected
                ? 'bg-surface border-primary-500 ring-2 ring-primary-500/30'
                : skill.enabled ? 'bg-surface border-default' : 'bg-surface border-default opacity-60'
        }`}>
            {/* Header */}
            <div className="p-4 border-b border-default">
                <div className="flex items-start justify-between mb-3">
                    <div className="flex items-start space-x-3 flex-1 min-w-0 min-h-0">
                        {/* Selection checkbox (bulk-delete mode) */}
                        {selectable && (
                            <input
                                type="checkbox"
                                checked={selected}
                                onChange={onToggleSelect}
                                onClick={(e) => e.stopPropagation()}
                                aria-label={`Select ${skill.name}`}
                                className="mt-2 h-4 w-4 flex-shrink-0 rounded border-strong text-primary-600 focus:ring-primary-500 cursor-pointer"
                            />
                        )}
                        {/* Icon */}
                        <div className={`p-2 rounded-lg flex-shrink-0 ${statusConfig.bgColor}`}>
                            <Sparkles className={`w-6 h-6 ${statusConfig.color}`} />
                        </div>
                        <div className="flex-1 min-w-0 min-h-0">
                            <h3 className="font-semibold text-default truncate" title={skill.name}>
                                {skill.name}
                            </h3>
                            {skill.description && (
                                <p className="text-sm text-secondary line-clamp-2 mt-1">
                                    {skill.description}
                                </p>
                            )}
                        </div>
                    </div>

                    {/* Menu */}
                    <div className="relative ml-2 flex-shrink-0">
                        <button
                            onClick={() => setShowMenu(!showMenu)}
                            className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        >
                            <MoreVertical className="w-5 h-5" />
                        </button>

                        {showMenu && (
                            <>
                                <div
                                    className="fixed inset-0 z-10"
                                    onClick={() => setShowMenu(false)}
                                />
                                <div className="absolute right-0 top-full mt-1 w-48 bg-surface rounded-lg shadow-lg border border-default py-1 z-20">
                                    {/* Lifecycle */}
                                    {onValidate && (
                                        <button onClick={() => { onValidate(); setShowMenu(false); }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <CheckCircle className="w-4 h-4" /><span>Validate</span>
                                        </button>
                                    )}
                                    {onPublish && (
                                        <button onClick={() => { onPublish(); setShowMenu(false); }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 text-green-700">
                                            <Globe className="w-4 h-4" /><span>Publish</span>
                                        </button>
                                    )}
                                    {onArchive && (
                                        <button onClick={() => { onArchive(); setShowMenu(false); }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Archive className="w-4 h-4" /><span>Archive</span>
                                        </button>
                                    )}

                                    <div className="border-t border-default my-1" />

                                    {/* View — only for published skills; drafts use Edit to open
                                        the same panel and get write access. */}
                                    {isPublished && (
                                        <button onClick={() => { onOpenTab('overview'); setShowMenu(false); }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Eye className="w-4 h-4" /><span>View</span>
                                        </button>
                                    )}

                                    <div className="border-t border-default my-1" />

                                    {/* Actions */}
                                    {!isPublished && (
                                        <button onClick={() => { onEdit(); setShowMenu(false); }}
                                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                            <Edit className="w-4 h-4" /><span>Edit</span>
                                        </button>
                                    )}
                                    <button onClick={() => { onCopy(); setShowMenu(false); }}
                                        className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2">
                                        <Copy className="w-4 h-4" /><span>Duplicate</span>
                                    </button>

                                    <div className="border-t border-default my-1" />

                                    <button onClick={() => { onDelete(); setShowMenu(false); }}
                                        className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 text-red-600">
                                        <Trash2 className="w-4 h-4" /><span>Delete</span>
                                    </button>
                                </div>
                            </>
                        )}
                    </div>
                </div>

                {/* Status + Version row */}
                <div className="flex items-center space-x-2 flex-wrap gap-y-1">
                    <AgentSkillStatusBadge status={skill.status} />
                    {isComposite && (
                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-xs font-medium bg-primary-100 text-primary-700 rounded-full">
                            <Workflow className="w-3 h-3" />
                            <span>Composite</span>
                        </span>
                    )}
                    <span className="px-2 py-0.5 text-xs font-mono bg-surface-raised text-secondary rounded">
                        v{skill.version}
                    </span>
                    {!skill.enabled && (
                        <span className="px-2 py-0.5 text-xs font-medium text-muted bg-surface-raised rounded">
                            Disabled
                        </span>
                    )}
                </div>
            </div>

            {/* Body */}
            <div className="p-4 space-y-3">
                {/* Category */}
                <div className="flex items-center space-x-2">
                    <Layers className="w-3.5 h-3.5 text-muted flex-shrink-0" />
                    <span className="text-xs font-medium text-secondary">{category}</span>
                </div>

                {/* Tags */}
                {tags.length > 0 && (
                    <div className="flex items-start space-x-2">
                        <Tag className="w-3.5 h-3.5 text-muted flex-shrink-0 mt-0.5" />
                        <div className="flex flex-wrap gap-1">
                            {tags.slice(0, 5).map((tag) => (
                                <span
                                    key={tag}
                                    className="px-2 py-0.5 text-xs bg-primary-50 text-primary-700 rounded-full"
                                >
                                    {tag}
                                </span>
                            ))}
                            {tags.length > 5 && (
                                <span className="px-2 py-0.5 text-xs text-muted">
                                    +{tags.length - 5} more
                                </span>
                            )}
                        </div>
                    </div>
                )}

                {/* Metrics row */}
                <div className="flex items-center space-x-4 text-xs text-secondary">
                    <div className="flex items-center space-x-1" title="Total executions">
                        <Activity className="w-3.5 h-3.5" />
                        <span>{skill.execution_count.toLocaleString()}</span>
                    </div>
                    {costEstimate != null && (
                        <div className="flex items-center space-x-1" title="Estimated cost per execution">
                            <DollarSign className="w-3.5 h-3.5" />
                            <span>${costEstimate.toFixed(2)}</span>
                        </div>
                    )}
                    {approvalRequired && (
                        <div className="flex items-center space-x-1 text-amber-600" title="Requires approval">
                            <ShieldCheck className="w-3.5 h-3.5" />
                            <span>Approval</span>
                        </div>
                    )}
                </div>

                {/* SKILL.md indicator */}
                <div className="flex items-center space-x-2 text-xs text-muted">
                    <FileCode className="w-3.5 h-3.5" />
                    <span>SKILL.md</span>
                    <code className="px-1.5 py-0.5 bg-surface-raised rounded font-mono text-secondary">
                        {skill.skill_md_hash.substring(0, 8)}
                    </code>
                </div>

                {/* Allowed tools */}
                {skill.allowed_tools && skill.allowed_tools.length > 0 && (
                    <div className="text-xs text-muted">
                        <span className="font-medium">Tools: </span>
                        {skill.allowed_tools.slice(0, 3).join(', ')}
                        {skill.allowed_tools.length > 3 && ` +${skill.allowed_tools.length - 3} more`}
                    </div>
                )}

                {/* Group Access */}
                <div className="pt-3 border-t border-default">
                    <span className="text-xs text-muted block mb-1">Access</span>
                    <GroupTreeDisplay
                        groups={skill.groups || []}
                        variant="expanded"
                        maxVisible={5}
                    />
                </div>
            </div>

            {/* Footer */}
            <div className="px-4 py-3 border-t border-default bg-surface-alt">
                <div className="flex items-center justify-between text-xs text-muted">
                    <span>
                        {skill.license || 'No license'}
                    </span>
                    {skill.last_executed_at && (
                        <span title="Last executed">
                            Last run: {new Date(skill.last_executed_at).toLocaleDateString()}
                        </span>
                    )}
                </div>
            </div>

            {/* Disabled Overlay */}
            {!skill.enabled && (
                <div className="absolute inset-0 bg-gray-900/5 rounded-lg pointer-events-none" />
            )}
        </div>
    );
};

export default AgentSkillCard;
