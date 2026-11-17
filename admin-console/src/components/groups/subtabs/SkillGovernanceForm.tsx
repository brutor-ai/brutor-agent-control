/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational skill global-governance form. Renders the
 * "Skill Execution Settings" card (execution mode / discovery / approval
 * timeout + execution controls) over a plain `SkillGlobalGovernance` dict
 * and reports edits back via `onChange`. It owns no state and does no
 * fetching/saving — the parent (e.g. `GroupSkillGovernanceTab`) holds the
 * governance dict and runs load/save/dirty-tracking, so the same form can
 * be reused by any caller that needs to edit skill governance.
 *
 * Sibling to `MCPGovernanceForm`. Inherited-value badges and the effective
 * hint are surfaced from the optional `inherited` / `effective` props
 * (informational only); the section help modal stays with the owning tab.
 */

import React from 'react';
import { Sparkles, ArrowDown } from 'lucide-react';
import { SkillGlobalGovernance } from '../../../lib/groupTypes';
import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, approvalTimeoutProblem } from '../../../lib/approvalTimeout';

/** What the Control Plane says of the cap on the tool calls of one execution. */
const MAX_TOOL_CALLS_ADVISORY =
    'Advisory: the gateway hands this number to the agent with the skill, and the agent is expected to stop after it; the gateway does not count the tool calls of an execution.';

interface SkillGovernanceFormProps {
    value: SkillGlobalGovernance | Record<string, any>;
    onChange: (next: SkillGlobalGovernance | Record<string, any>) => void;
    inherited?: SkillGlobalGovernance | Record<string, any> | null;
    effective?: SkillGlobalGovernance | Record<string, any> | null;
    disabled?: boolean;
}

const executionModeOptions = [
    { value: 'unrestricted', label: 'Unrestricted' },
    { value: 'approval_required', label: 'Approval Required' },
    { value: 'disabled', label: 'Disabled' },
];

/**
 * Pure presentational renderer for the skill governance field set.
 *
 * Holds no state and performs no I/O — it renders `value`, surfaces
 * inherited-value badges from `inherited`, and emits the next
 * `SkillGlobalGovernance` object through `onChange`. The owning tab keeps
 * load/save/help behavior.
 */
const SkillGovernanceForm: React.FC<SkillGovernanceFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    disabled = false,
}) => {
    // Inherited badges only apply when an inherited governance object is
    // present (which only happens when the group has an ancestor with
    // governance configured).
    const hasParent = inherited != null;

    // Update a field and emit the next governance object
    const updateField = (field: keyof SkillGlobalGovernance, val: any) => {
        const next = {
            ...value,
            [field]: val,
        };
        onChange(next);
    };

    const governance = value as Record<string, any>;
    const inheritedGovernance = inherited as Record<string, any> | null;
    const effectiveGovernance = effective as Record<string, any> | null;

    const inheritedExecMode = inheritedGovernance?.execution_mode;
    const inheritedDiscovery = inheritedGovernance?.discovery_enabled;
    const inheritedTimeout = inheritedGovernance?.approval_timeout_seconds;
    const effectiveTimeout = effectiveGovernance?.approval_timeout_seconds;
    const timeoutProblem = approvalTimeoutProblem(governance.approval_timeout_seconds);
    const inheritedMaxConcurrent = inheritedGovernance?.max_concurrent_executions;
    const effectiveMaxConcurrent = effectiveGovernance?.max_concurrent_executions;
    const inheritedMaxToolCalls = inheritedGovernance?.max_tool_calls_per_execution;

    return (
        <div className="bg-surface border border-default rounded-lg p-6">
            <div className="flex items-center gap-3 mb-4">
                <Sparkles className="w-5 h-5 text-amber-600" />
                <h3 className="font-medium text-default">Skill Execution Settings</h3>
            </div>
            <div className="space-y-4">
                {/* Execution Mode */}
                <div>
                    <label className="block text-sm font-medium text-secondary mb-1">
                        Execution Mode
                        {inheritedExecMode !== undefined && hasParent && (
                            <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedExecMode}`}>
                                <ArrowDown className="w-3 h-3 mr-0.5" />
                                inherited: {inheritedExecMode}
                            </span>
                        )}
                    </label>
                    <select
                        value={governance.execution_mode ?? ''}
                        onChange={(e) => updateField('execution_mode', e.target.value || undefined)}
                        disabled={disabled}
                        className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary ${
                            inheritedExecMode !== undefined && hasParent ? 'border-primary bg-primary-pale/30' : 'border-strong'
                        }`}
                    >
                        <option value="">{inheritedExecMode !== undefined && hasParent ? `Inherited: ${inheritedExecMode}` : 'Not set'}</option>
                        {executionModeOptions.map(opt => (
                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                    </select>
                </div>

                {/* Discovery Enabled */}
                <div>
                    <label className="block text-sm font-medium text-secondary mb-1">
                        Discovery Enabled
                        {inheritedDiscovery !== undefined && hasParent && (
                            <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedDiscovery}`}>
                                <ArrowDown className="w-3 h-3 mr-0.5" />
                                inherited: {inheritedDiscovery ? 'Yes' : 'No'}
                            </span>
                        )}
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer mt-2">
                        <input
                            type="checkbox"
                            checked={governance.discovery_enabled ?? false}
                            onChange={(e) => updateField('discovery_enabled', e.target.checked)}
                            disabled={disabled}
                            className="w-4 h-4 text-primary border-strong rounded focus:ring-primary"
                        />
                        <span className="text-sm text-secondary">Allow skill discovery for this group</span>
                    </label>
                </div>

                {/* Approval Timeout */}
                <div>
                    <label className="block text-sm font-medium text-secondary mb-1">
                        Approval Timeout (seconds)
                        {inheritedTimeout !== undefined && hasParent && (
                            <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedTimeout}`}>
                                <ArrowDown className="w-3 h-3 mr-0.5" />
                                max {inheritedTimeout}
                            </span>
                        )}
                    </label>
                    <input
                        type="number"
                        step="1"
                        min={APPROVAL_TIMEOUT_MIN_SECONDS}
                        max={APPROVAL_TIMEOUT_MAX_SECONDS}
                        value={governance.approval_timeout_seconds ?? ''}
                        onChange={(e) => updateField('approval_timeout_seconds', e.target.value ? parseInt(e.target.value) : undefined)}
                        disabled={disabled}
                        placeholder={inheritedTimeout !== undefined && hasParent ? `Inherited: ${inheritedTimeout}` : 'No limit'}
                        className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary ${
                            inheritedTimeout !== undefined && hasParent ? 'border-primary bg-primary-pale/30' : 'border-strong'
                        }`}
                    />
                    {timeoutProblem && (
                        <p role="alert" className="text-xs text-red-600 mt-0.5">{timeoutProblem}</p>
                    )}
                    {inheritedTimeout !== undefined && hasParent && effectiveTimeout !== undefined &&
                     governance.approval_timeout_seconds !== undefined && effectiveTimeout !== governance.approval_timeout_seconds && (
                        <p className="text-xs text-muted mt-0.5">
                            Effective: {effectiveTimeout}
                        </p>
                    )}
                </div>

                {/* Concurrency & Depth */}
                <div className="space-y-4 pt-4 border-t border-default">
                    <h4 className="text-sm font-medium text-default">Execution Controls</h4>

                    <div>
                        <label className="block text-sm font-medium text-secondary mb-1">
                            Max Concurrent Executions
                            {inheritedMaxConcurrent !== undefined && hasParent && (
                                <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedMaxConcurrent}`}>
                                    <ArrowDown className="w-3 h-3 mr-0.5" />
                                    max {inheritedMaxConcurrent}
                                </span>
                            )}
                        </label>
                        <input
                            type="number"
                            min="1"
                            value={governance.max_concurrent_executions ?? ''}
                            onChange={(e) => updateField('max_concurrent_executions', e.target.value ? parseInt(e.target.value) : undefined)}
                            disabled={disabled}
                            placeholder={inheritedMaxConcurrent !== undefined && hasParent ? `Inherited: ${inheritedMaxConcurrent}` : 'No limit'}
                            className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary ${
                                inheritedMaxConcurrent !== undefined && hasParent ? 'border-primary bg-primary-pale/30' : 'border-strong'
                            }`}
                        />
                        {inheritedMaxConcurrent !== undefined && hasParent && effectiveMaxConcurrent !== undefined &&
                         governance.max_concurrent_executions !== undefined && effectiveMaxConcurrent !== governance.max_concurrent_executions && (
                            <p className="text-xs text-muted mt-0.5">
                                Effective: {effectiveMaxConcurrent}
                            </p>
                        )}
                        <p className="text-xs text-muted mt-1">
                            Max skill executions running simultaneously in this group. Prevents resource exhaustion.
                        </p>
                    </div>

                    <div>
                        <label htmlFor="skill-max-tool-calls" className="block text-sm font-medium text-secondary mb-1">
                            Max Tool Calls per Execution (advisory)
                            {inheritedMaxToolCalls !== undefined && hasParent && (
                                <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedMaxToolCalls}`}>
                                    <ArrowDown className="w-3 h-3 mr-0.5" />
                                    max {inheritedMaxToolCalls}
                                </span>
                            )}
                        </label>
                        <input
                            id="skill-max-tool-calls"
                            type="number"
                            min="1"
                            value={governance.max_tool_calls_per_execution ?? ''}
                            onChange={(e) => updateField('max_tool_calls_per_execution', e.target.value ? parseInt(e.target.value) : undefined)}
                            disabled={disabled}
                            placeholder={inheritedMaxToolCalls !== undefined && hasParent ? `Inherited: ${inheritedMaxToolCalls}` : 'No limit'}
                            className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary ${
                                inheritedMaxToolCalls !== undefined && hasParent ? 'border-primary bg-primary-pale/30' : 'border-strong'
                            }`}
                        />
                        <p className="text-xs text-muted mt-1" data-testid="skill-max-tool-calls-advisory">
                            {MAX_TOOL_CALLS_ADVISORY}
                        </p>
                    </div>

                </div>
            </div>
        </div>
    );
};

export default SkillGovernanceForm;
