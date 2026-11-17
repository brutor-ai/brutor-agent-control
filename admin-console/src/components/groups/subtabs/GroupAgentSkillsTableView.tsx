/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    Trash2,
    CheckCircle,
    XCircle,
    ArrowUpRight,
    Sparkles,
    Play,
    ShieldCheck,
    Gauge
} from 'lucide-react';
import { AgentSkillInfo } from '../../../lib/groupTypes';
import {
    SKILL_STATUS_CONFIG,
    formatSkillName,
    SkillStatus
} from '../../../lib/agentSkillTypes';
import { resolveGroupIcon } from '../../../lib/lucideIconMap';

interface GroupAgentSkillsTableViewProps {
    skills: AgentSkillInfo[];
    onRemove: (skillId: string, skillName: string) => void;
    onConfigureGovernance?: (skill: AgentSkillInfo) => void;
    onConfigureLimits?: (skill: AgentSkillInfo) => void;
    removingId: string | null;
}

const GroupAgentSkillsTableView: React.FC<GroupAgentSkillsTableViewProps> = ({
                                                                                 skills,
                                                                                 onRemove,
                                                                                 onConfigureGovernance,
                                                                                 onConfigureLimits,
                                                                                 removingId
                                                                             }) => {
    // Render status badge
    const renderStatusBadge = (status: string) => {
        const config = SKILL_STATUS_CONFIG[status as SkillStatus];
        if (!config) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                    <span className="text-xs font-medium text-secondary">{status}</span>
                </div>
            );
        }
        return (
            <div className={`inline-flex items-center space-x-1 px-2 py-1 ${config.bgColor} rounded-full`}>
                <span className={`text-xs font-medium ${config.color}`}>{config.label}</span>
            </div>
        );
    };

    // Render skill icon
    const renderSkillIcon = (skill: AgentSkillInfo) => {
        const iconName = skill.icon_name || 'Sparkles';
        const Icon = resolveGroupIcon(iconName, undefined) || Sparkles;
        return (
            <div className="w-10 h-10 rounded-lg bg-primary-50 flex items-center justify-center border border-primary-200">
                <Icon className="w-5 h-5 text-primary-600" />
            </div>
        );
    };

    // Render type badge (direct/inherited)
    const getTypeBadge = (skill: AgentSkillInfo) => {
        if (skill.is_inherited) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                    <ArrowUpRight className="w-3 h-3 text-muted" />
                    <span className="text-xs font-medium text-secondary">Inherited</span>
                </div>
            );
        }
        return (
            <div className="inline-flex items-center space-x-1 px-2 py-1 bg-primary-pale rounded-full">
                <CheckCircle className="w-3 h-3 text-primary" />
                <span className="text-xs font-medium text-primary">Direct</span>
            </div>
        );
    };

    return (
        <div className="bg-surface rounded-lg border border-default overflow-hidden">
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th scope="col" className={`${TABLE_TH_TYPE} w-16 px-4 py-3 text-left`}>
                            Icon
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Skill
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Description
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Version
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Type
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Status
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Enabled
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Executions
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {skills.map((skill) => {
                        const isRemoving = removingId === skill.id;
                        const isInherited = skill.is_inherited;
                        const isDirect = !isInherited;

                        return (
                            <tr
                                key={skill.id}
                                className={`transition-colors ${
                                    isInherited
                                        ? 'bg-surface-alt'
                                        : 'hover:bg-surface-alt'
                                }`}
                            >
                                {/* Icon */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {renderSkillIcon(skill)}
                                </td>

                                {/* Name & ID */}
                                <td className="px-4 py-4">
                                    <div className="flex flex-col">
                                        <div className="text-sm font-medium text-default">
                                            {formatSkillName(skill.name)}
                                        </div>
                                        <code className="text-xs text-muted font-mono mt-0.5">
                                            {skill.name}
                                        </code>
                                        {skill.category && (
                                            <span className="text-xs text-muted mt-0.5">
                                                    {skill.category}
                                                </span>
                                        )}
                                    </div>
                                </td>

                                {/* Description */}
                                <td className="px-4 py-4">
                                    <div className="text-sm text-secondary max-w-xs">
                                        {skill.description ? (
                                            <span className="line-clamp-2">{skill.description}</span>
                                        ) : (
                                            <span className="text-muted italic">No description</span>
                                        )}
                                    </div>
                                </td>

                                {/* Version */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                        <span className="text-sm text-secondary font-mono">
                                            v{skill.version}
                                        </span>
                                </td>

                                {/* Type (Direct/Inherited) */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {getTypeBadge(skill)}
                                </td>

                                {/* Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {renderStatusBadge(skill.status)}
                                </td>

                                {/* Enabled */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {skill.enabled ? (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full">
                                            <CheckCircle className="w-3 h-3 text-green-600" />
                                            <span className="text-xs font-medium text-green-700">Enabled</span>
                                        </div>
                                    ) : (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-raised rounded-full">
                                            <XCircle className="w-3 h-3 text-muted" />
                                            <span className="text-xs font-medium text-secondary">Disabled</span>
                                        </div>
                                    )}
                                </td>

                                {/* Execution Count */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <div className="flex items-center space-x-1 text-sm text-secondary">
                                        <Play className="w-3 h-3 text-muted" />
                                        <span>{skill.execution_count.toLocaleString()}</span>
                                    </div>
                                </td>

                                {/* Actions */}
                                <td className="px-4 py-4 whitespace-nowrap text-right">
                                    <div className="flex items-center justify-end space-x-1">
                                        <button
                                            onClick={() => onConfigureGovernance?.(skill)}
                                            className={`relative p-1.5 rounded transition-colors ${
                                                skill.has_governance
                                                    ? 'text-primary-600 hover:text-primary-700 hover:bg-primary-50'
                                                    : 'text-muted hover:text-secondary hover:bg-surface-alt'
                                            }`}
                                            title={skill.has_governance ? "Governance active — click to configure" : "Configure governance"}
                                        >
                                            <ShieldCheck className="w-4 h-4" />
                                            {skill.has_governance && (
                                                <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-primary-500 rounded-full ring-2 ring-white" />
                                            )}
                                        </button>
                                        <button
                                            onClick={() => onConfigureLimits?.(skill)}
                                            className={`relative p-1.5 rounded transition-colors ${
                                                skill.has_limits
                                                    ? 'text-amber-600 hover:text-amber-700 hover:bg-surface-alt'
                                                    : 'text-muted hover:text-secondary hover:bg-surface-alt'
                                            }`}
                                            title={skill.has_limits ? "Per-skill limits active — click to configure" : "Configure per-skill limits"}
                                        >
                                            <Gauge className="w-4 h-4" />
                                            {skill.has_limits && (
                                                <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-amber-500 rounded-full ring-2 ring-white" />
                                            )}
                                        </button>
                                        {isDirect && (
                                            <button
                                                onClick={() => onRemove(skill.id, skill.name)}
                                                disabled={isRemoving}
                                                className="p-1.5 text-red-600 hover:text-red-700 hover:bg-surface-alt rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                                title="Remove skill from group"
                                            >
                                                {isRemoving ? (
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-red-600"></div>
                                                ) : (
                                                    <Trash2 className="w-4 h-4" />
                                                )}
                                            </button>
                                        )}
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                    </tbody>
                </table>
            </div>

            {/* Footer with count */}
            <div className="px-4 py-3 bg-surface-alt border-t border-default">
                <div className="text-sm text-secondary">
                    Showing <span className="font-medium">{skills.length}</span> skill{skills.length !== 1 ? 's' : ''}
                </div>
            </div>
        </div>
    );
};

export default GroupAgentSkillsTableView;