/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational MCP global-governance form. Renders the
 * "Default Access Levels" card (tool / resource / prompt / resource-template
 * access + approval timeout) over a plain `MCPGlobalGovernance` dict and
 * reports edits back via `onChange`. It owns no state and does no
 * fetching/saving — the parent (e.g. `GroupMCPGovernanceTab`) holds the
 * governance dict and runs load/save/dirty-tracking, so the same form can be
 * reused by any caller that needs to edit MCP governance.
 *
 * Sibling to `LLMGovernanceForm`. Inherited-value badges and the effective
 * hint are surfaced from the optional `inherited` / `effective` props
 * (informational only).
 */

import React from 'react';
import { Shield, ArrowDown } from 'lucide-react';
import { MCPGlobalGovernance } from '../../../lib/groupTypes';
import { APPROVAL_TIMEOUT_MAX_SECONDS, APPROVAL_TIMEOUT_MIN_SECONDS, approvalTimeoutProblem } from '../../../lib/approvalTimeout';
import { governanceRelaxations, relaxationsByPath } from '../../../lib/governanceInheritance';

interface MCPGovernanceFormProps {
    value: MCPGlobalGovernance | Record<string, any>;
    onChange: (next: MCPGlobalGovernance | Record<string, any>) => void;
    inherited?: MCPGlobalGovernance | Record<string, any> | null;
    effective?: MCPGlobalGovernance | Record<string, any> | null;
    disabled?: boolean;
}

const accessOptions = [
    { value: 'enabled', label: 'Enabled' },
    { value: 'approval_required', label: 'Approval Required' },
    { value: 'disabled', label: 'Disabled' },
];

/**
 * Pure presentational renderer for the MCP governance field set.
 *
 * Holds no state and performs no I/O — it renders `value`, surfaces
 * inherited-value badges from `inherited`, and emits the next
 * `MCPGlobalGovernance` object through `onChange`. The owning tab keeps
 * load/save/help behavior.
 */
const MCPGovernanceForm: React.FC<MCPGovernanceFormProps> = ({
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

    // MCP governance is a flat blob, so a field's path is its own name.
    const relaxationFor = relaxationsByPath(
        governanceRelaxations(value, inherited, 'mcp'),
    );

    // Update a field and emit the next governance object
    const updateField = (field: keyof MCPGlobalGovernance, val: any) => {
        const next = {
            ...value,
            [field]: val,
        };
        onChange(next);
    };

    /** Render a select input with inheritance indicator */
    const renderSelectInput = (
        label: string,
        field: keyof MCPGlobalGovernance,
    ) => {
        const inheritedVal = inherited ? (inherited as Record<string, any>)[field] : undefined;
        const current = (value as Record<string, any>)[field];
        const hasInherited = inheritedVal !== undefined && hasParent;
        const relaxed = relaxationFor[field as string];

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    {label}
                    {hasInherited && (
                        <span className={`ml-1.5 inline-flex items-center text-xs ${relaxed ? 'text-red-600 font-semibold' : 'text-primary'}`} title={`Inherited: ${inheritedVal}`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            inherited: {String(inheritedVal)}
                        </span>
                    )}
                </label>
                <select
                    value={current ?? ''}
                    onChange={(e) => updateField(field, e.target.value || undefined)}
                    disabled={disabled}
                    aria-invalid={relaxed ? true : undefined}
                    data-testid={`governance-input-${field}`}
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 ${
                        relaxed
                            ? 'border-red-500 focus:ring-red-500 focus:border-red-500'
                            : hasInherited
                              ? 'border-primary bg-primary-pale/30 focus:ring-primary focus:border-primary'
                              : 'border-strong focus:ring-primary focus:border-primary'
                    }`}
                >
                    <option value="">{hasInherited ? `Inherited: ${inheritedVal}` : 'Not set'}</option>
                    {accessOptions.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                </select>
                {relaxed && (
                    <p className="text-xs text-red-600 mt-0.5 font-medium" data-testid={`governance-error-${field}`}>
                        {relaxed.message}
                    </p>
                )}
            </div>
        );
    };

    /** Render a number input with inheritance indicator */
    const renderNumberInput = (
        label: string,
        field: keyof MCPGlobalGovernance,
    ) => {
        const inheritedVal = inherited ? (inherited as Record<string, any>)[field] : undefined;
        const effectiveVal = effective ? (effective as Record<string, any>)[field] : undefined;
        const current = (value as Record<string, any>)[field];
        const hasInherited = inheritedVal !== undefined && hasParent;
        const relaxed = relaxationFor[field as string];
        // The one number of this form with bounds of its own in the contract.
        const isTimeout = field === 'approval_timeout_seconds';
        const timeoutProblem = isTimeout ? approvalTimeoutProblem(current) : null;

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    {label}
                    {hasInherited && (
                        <span className={`ml-1.5 inline-flex items-center text-xs ${relaxed ? 'text-red-600 font-semibold' : 'text-primary'}`} title={`Inherited: ${inheritedVal}`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            max {String(inheritedVal)}
                        </span>
                    )}
                </label>
                <input
                    type="number"
                    step="1"
                    min={isTimeout ? APPROVAL_TIMEOUT_MIN_SECONDS : 0}
                    max={isTimeout ? APPROVAL_TIMEOUT_MAX_SECONDS : undefined}
                    value={current ?? ''}
                    onChange={(e) => updateField(field, e.target.value ? parseInt(e.target.value) : undefined)}
                    disabled={disabled}
                    placeholder={hasInherited ? `Inherited: ${inheritedVal}` : 'No limit'}
                    aria-invalid={relaxed ? true : undefined}
                    data-testid={`governance-input-${field}`}
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 ${
                        relaxed
                            ? 'border-red-500 focus:ring-red-500 focus:border-red-500'
                            : hasInherited
                              ? 'border-primary bg-primary-pale/30 focus:ring-primary focus:border-primary'
                              : 'border-strong focus:ring-primary focus:border-primary'
                    }`}
                />
                {timeoutProblem && (
                    <p role="alert" className="text-xs text-red-600 mt-0.5" data-testid="approval-timeout-problem">{timeoutProblem}</p>
                )}
                {relaxed && (
                    <p className="text-xs text-red-600 mt-0.5 font-medium" data-testid={`governance-error-${field}`}>
                        {relaxed.message}
                    </p>
                )}
                {!relaxed && hasInherited && effectiveVal !== undefined && current !== undefined && effectiveVal !== current && (
                    <p className="text-xs text-muted mt-0.5">
                        Effective: {String(effectiveVal)}
                    </p>
                )}
            </div>
        );
    };

    return (
        <div className="bg-surface border border-default rounded-lg p-6">
            <div className="flex items-center gap-3 mb-4">
                <Shield className="w-5 h-5 text-muted" />
                <h3 className="font-medium text-default">Default Access Levels</h3>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {renderSelectInput('Default Tool Access', 'default_tool_access')}
                {renderSelectInput('Default Resource Access', 'default_resource_access')}
                {renderSelectInput('Default Prompt Access', 'default_prompt_access')}
                {renderSelectInput('Default Resource Template Access', 'default_resource_template_access')}
                {renderNumberInput('Approval Timeout (seconds)', 'approval_timeout_seconds')}
            </div>
        </div>
    );
};

export default MCPGovernanceForm;
