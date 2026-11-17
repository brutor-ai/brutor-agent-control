/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational MCP server-limits form. Renders the server-wide
 * Execution / Data / Frequency limit sections over a plain
 * `MCPGlobalLimits` dict and reports edits back via `onChange`. It owns
 * no I/O — the parent (e.g. `GroupMCPLimitsTab`) holds the limits dict and
 * runs load/save/dirty-tracking, so the same form can be reused by any
 * caller that needs to edit MCP server limits.
 *
 * Sibling to `LLMLimitsForm` / `LLMGovernanceForm`. The inherited-value
 * badges, per-field capacity breakdown popover, and per-section help modal
 * are surfaced from the optional `inherited` / `effective` / `capacity`
 * props (informational only) — the owning tab keeps the load/save cycle.
 */

import React, { useState } from 'react';
import { Clock, HardDrive, Zap, HelpCircle } from 'lucide-react';
import { MCPGlobalLimits, LimitCapacityInfo } from '../../../lib/groupTypes';
import { InheritedLimitField } from '../limits/InheritedLimitField';
import { fieldCapacity } from '../../../lib/limitCapacity';
import { MCPLimitsHelpModal } from './GroupMCPLimitsHelpModal';
import UnavailableSettings from '../../common/UnavailableSettings';
import { withoutUnavailable, type UnavailableSetting } from '../../../lib/unavailableSettings';
import { MCP_LIMIT_HELP } from '../../../lib/mcpExecutionLimits';

/**
 * MCP limits the gateway does not act on in this release, and the Control
 * Plane refuses: a proxy cannot bound a remote server's memory or CPU, and
 * rows, items and a context-window share would need each tool's own format.
 */
export const MCP_LIMITS_NOT_AVAILABLE: readonly UnavailableSetting[] = [
    { path: ['execution', 'memory_limit_mb'], label: 'Memory Limit (MB)' },
    { path: ['execution', 'cpu_limit_percent'], label: 'CPU Limit (%)' },
    { path: ['data', 'max_context_window_percent'], label: 'Max Context Window (%)' },
    { path: ['data', 'max_rows_per_query'], label: 'Max Rows per Query' },
    { path: ['data', 'max_items_per_response'], label: 'Max Items per Response' },
];

interface MCPLimitsFormProps {
    /** An MCP limits document (the contract's `McpLimitsDocument`). */
    value: MCPGlobalLimits;
    onChange: (next: MCPGlobalLimits) => void;
    inherited?: MCPGlobalLimits | null;
    effective?: MCPGlobalLimits | null;
    capacity?: LimitCapacityInfo | null;
    disabled?: boolean;
}

/** Get a nested value from a limits object by section.field path */
const getNestedValue = (
    limits: MCPGlobalLimits | null | undefined,
    section: string,
    field: string,
): number | undefined => {
    if (!limits) return undefined;
    const sec = (limits as Record<string, any>)[section] as Record<string, any> | undefined;
    return sec?.[field] ?? undefined;
};

/**
 * Pure presentational renderer for the MCP server-limit sections.
 *
 * Holds no fetch/save state — it renders `value`, surfaces inherited /
 * capacity indicators, and emits the next `MCPGlobalLimits` object through
 * `onChange`. The section help modal and its per-field capacity popover are
 * local UI state (no I/O), matching the presentational-form pattern used by
 * `LLMLimitsForm`.
 */
const MCPLimitsForm: React.FC<MCPLimitsFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    capacity = null,
    disabled = false,
}) => {
    // Help modal state (per-section)
    const [showHelp, setShowHelp] = useState(false);
    const [helpTab, setHelpTab] = useState<'execution' | 'data' | 'frequency'>('execution');

    // Capacity popover state
    const [capacityPopoverField, setCapacityPopoverField] = useState<string | null>(null);

    // Inherited / capacity indicators only apply when the group has an
    // ancestor — signalled by the presence of an inherited or capacity object.
    const hasParent = inherited != null || capacity != null;

    // Update a nested field and emit the next limits object
    const updateField = (section: keyof MCPGlobalLimits, field: string, val: any) => {
        // `field` is one of the section's, as the inputs below name them.
        const next = {
            ...value,
            [section]: {
                ...(value[section] || {}),
                [field]: val,
            },
        } as MCPGlobalLimits;
        onChange(withoutUnavailable(next, MCP_LIMITS_NOT_AVAILABLE));
    };

    // Open help modal for specific section
    const openHelp = (tab: 'execution' | 'data' | 'frequency') => {
        setHelpTab(tab);
        setShowHelp(true);
    };

    /**
     * One limit input. The inherited badge, capacity breakdown and
     * over-limit warning all live in the shared `InheritedLimitField`,
     * which the LLM and Skill limits forms render too — one renderer, not
     * three copies of the same 120 lines.
     */
    const renderLimitInput = (
        label: string,
        section: keyof MCPGlobalLimits,
        field: string,
        opts: { step?: string; min?: string } = {}
    ) => {
        const fieldKey = `${section}.${field}`;
        return (
            <InheritedLimitField
                label={label}
                value={getNestedValue(value, section, field)}
                onChange={(v) => updateField(section, field, v)}
                inherited={getNestedValue(inherited, section, field)}
                effective={getNestedValue(effective, section, field)}
                capacity={fieldCapacity(capacity, section, field)}
                hasParent={hasParent}
                step={opts.step || '1'}
                min={opts.min || '0'}
                disabled={disabled}
                fieldKey={fieldKey}
                popoverOpen={capacityPopoverField === fieldKey}
                onTogglePopover={setCapacityPopoverField}
                help={MCP_LIMIT_HELP.group[fieldKey]}
            />
        );
    };

    return (
        <div className="space-y-6">
            {/* First Row: Execution, Data, Frequency */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                {/* Execution Limits */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center gap-3">
                            <Clock className="w-5 h-5 text-primary" />
                            <h3 className="font-medium text-default">Execution Limits</h3>
                        </div>
                        <button
                            onClick={() => openHelp('execution')}
                            className="text-muted hover:text-primary transition-colors"
                            title="Help"
                        >
                            <HelpCircle className="w-5 h-5" />
                        </button>
                    </div>

                    <div className="space-y-4">
                        {renderLimitInput('Timeout (seconds)', 'execution', 'timeout_seconds', { min: '1' })}
                        {renderLimitInput('Max Concurrency (per user)', 'execution', 'max_concurrency_per_user', { min: '1' })}
                        {renderLimitInput('Max Concurrency (per group)', 'execution', 'max_concurrency_per_group', { min: '1' })}
                    </div>
                </div>

                {/* Data Limits */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center gap-3">
                            <HardDrive className="w-5 h-5 text-muted" />
                            <h3 className="font-medium text-default">Data Limits</h3>
                        </div>
                        <button
                            onClick={() => openHelp('data')}
                            className="text-muted hover:text-primary transition-colors"
                            title="Help"
                        >
                            <HelpCircle className="w-5 h-5" />
                        </button>
                    </div>

                    <div className="space-y-4">
                        {renderLimitInput('Max Payload Size (MB)', 'data', 'max_payload_size_mb', { step: '0.01', min: '0.01' })}
                    </div>
                </div>

                {/* Frequency Limits */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center gap-3">
                            <Zap className="w-5 h-5 text-yellow-600" />
                            <h3 className="font-medium text-default">Frequency Limits</h3>
                        </div>
                        <button
                            onClick={() => openHelp('frequency')}
                            className="text-muted hover:text-primary transition-colors"
                            title="Help"
                        >
                            <HelpCircle className="w-5 h-5" />
                        </button>
                    </div>

                    <div className="space-y-4">
                        {renderLimitInput('Max Calls (per minute)', 'frequency', 'max_calls_per_minute', { min: '1' })}
                        {renderLimitInput('Max Calls (per hour)', 'frequency', 'max_calls_per_hour', { min: '1' })}
                        {renderLimitInput('Max Calls (per day)', 'frequency', 'max_calls_per_day', { min: '1' })}
                        {renderLimitInput('Burst Limit', 'frequency', 'burst_limit', { min: '1' })}
                        {renderLimitInput('Cooldown (seconds)', 'frequency', 'cooldown_seconds')}
                    </div>
                </div>
            </div>

            <UnavailableSettings doc={value} settings={MCP_LIMITS_NOT_AVAILABLE} />

            {/* Help Modal */}
            <MCPLimitsHelpModal
                isOpen={showHelp}
                onClose={() => setShowHelp(false)}
                initialTab={helpTab}
            />
        </div>
    );
};

export default MCPLimitsForm;
