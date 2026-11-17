/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    FileEdit,
    CheckCircle,
    Globe,
    AlertTriangle,
    Archive,
    Clock,
    Play,
    XCircle,
    Ban,
    Timer
} from 'lucide-react';
import {
    SkillStatus,
    ExecutionStatus,
    SKILL_STATUS_CONFIG,
    EXECUTION_STATUS_CONFIG
} from '../../lib/agentSkillTypes';

// ============================================================================
// Skill Status Badge
// ============================================================================

interface SkillStatusBadgeProps {
    status: SkillStatus;
    size?: 'sm' | 'md';
}

const SKILL_STATUS_ICONS: Record<SkillStatus, React.FC<{ className?: string }>> = {
    draft: FileEdit,
    validated: CheckCircle,
    published: Globe,
    deprecated: AlertTriangle,
    archived: Archive
};

const AgentSkillStatusBadge: React.FC<SkillStatusBadgeProps> = ({ status, size = 'sm' }) => {
    const config = SKILL_STATUS_CONFIG[status] || SKILL_STATUS_CONFIG.draft;
    const Icon = SKILL_STATUS_ICONS[status] || FileEdit;

    const sizeClasses = size === 'md'
        ? 'px-2.5 py-1 text-sm'
        : 'px-2 py-0.5 text-xs';

    const iconSize = size === 'md' ? 'w-4 h-4' : 'w-3 h-3';

    return (
        <span
            className={`inline-flex items-center space-x-1 font-medium rounded-full ${config.bgColor} ${config.color} ${sizeClasses}`}
            title={config.description}
        >
            <Icon className={iconSize} />
            <span>{config.label}</span>
        </span>
    );
};

// ============================================================================
// Execution Status Badge
// ============================================================================

interface ExecutionStatusBadgeProps {
    status: ExecutionStatus;
    size?: 'sm' | 'md';
}

const EXECUTION_STATUS_ICONS: Record<ExecutionStatus, React.FC<{ className?: string }>> = {
    pending: Clock,
    running: Play,
    completed: CheckCircle,
    failed: XCircle,
    cancelled: Ban,
    timed_out: Timer
};

const AgentSkillExecutionStatusBadge: React.FC<ExecutionStatusBadgeProps> = ({ status, size = 'sm' }) => {
    const config = EXECUTION_STATUS_CONFIG[status] || EXECUTION_STATUS_CONFIG.pending;
    const Icon = EXECUTION_STATUS_ICONS[status] || Clock;

    const sizeClasses = size === 'md'
        ? 'px-2.5 py-1 text-sm'
        : 'px-2 py-0.5 text-xs';

    const iconSize = size === 'md' ? 'w-4 h-4' : 'w-3 h-3';

    return (
        <span className={`inline-flex items-center space-x-1 font-medium rounded-full ${config.bgColor} ${config.color} ${sizeClasses}`}>
            <Icon className={iconSize} />
            <span>{config.label}</span>
        </span>
    );
};

export {
    AgentSkillStatusBadge,
    AgentSkillExecutionStatusBadge
};
export default AgentSkillStatusBadge;
