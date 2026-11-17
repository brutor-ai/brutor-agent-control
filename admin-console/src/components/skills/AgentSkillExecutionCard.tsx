/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One execution of a skill, on the Executions tab of the skill: what it did.
 *
 * Nothing about an approval is shown. A skill that needs one is held in
 * Mission Control → Approvals, and an execution exists only once it runs;
 * the gateway records every execution as needing no approval.
 */

import React from 'react';

import type { ExecutionSummary } from '../../lib/agentSkillTypes';
import { AgentSkillExecutionStatusBadge } from './AgentSkillStatusBadge';

export const ExecutionCard: React.FC<{ execution: ExecutionSummary }> = ({ execution }) => (
    <div className="border border-default rounded-lg p-3">
        <div className="flex items-center justify-between mb-1">
            <AgentSkillExecutionStatusBadge status={execution.execution_status} />
            <span className="text-xs text-muted">
                {new Date(execution.started_at).toLocaleString()}
            </span>
        </div>
        <div className="flex items-center space-x-4 text-xs text-muted mt-1">
            <code className="text-secondary">{execution.id.substring(0, 16)}...</code>
            {execution.duration_ms != null && <span>{execution.duration_ms}ms</span>}
            {execution.total_tokens != null && <span>{execution.total_tokens.toLocaleString()} tokens</span>}
            {execution.estimated_cost_usd != null && <span>${execution.estimated_cost_usd.toFixed(4)}</span>}
        </div>
        {execution.error_message && (
            <p className="text-xs text-red-600 mt-1 truncate">{execution.error_message}</p>
        )}
    </div>
);
