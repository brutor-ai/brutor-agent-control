/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure presentational skill-limits form. Renders the "Execution Limits"
 * card over a plain `SkillGlobalLimits` dict and reports edits back via
 * `onChange`. It owns no fetch/save state — the parent (e.g.
 * `GroupSkillLimitsTab`) holds the limits dict and runs
 * load/save/dirty-tracking, so the same form can be reused by any caller
 * that needs to edit skill limits.
 *
 * Sibling to `MCPLimitsForm`. The inherited-value badges, per-field
 * capacity breakdown popover, and the section help modal are surfaced from
 * the optional `inherited` / `effective` / `capacity` props (informational
 * only) — the popover and help modal are local UI state (no I/O), matching
 * the presentational-form pattern used by `MCPLimitsForm`.
 */

import React, { useState } from 'react';
import { Clock, HelpCircle } from 'lucide-react';
import { SkillGlobalLimits, LimitCapacityInfo } from '../../../lib/groupTypes';
import { InheritedLimitField } from '../limits/InheritedLimitField';
import { fieldCapacity } from '../../../lib/limitCapacity';
import { GroupSkillLimitsHelpModal } from './GroupSkillLimitsHelpModal';

interface SkillLimitsFormProps {
    value: SkillGlobalLimits | Record<string, any>;
    onChange: (next: SkillGlobalLimits | Record<string, any>) => void;
    inherited?: SkillGlobalLimits | Record<string, any> | null;
    effective?: SkillGlobalLimits | Record<string, any> | null;
    capacity?: LimitCapacityInfo | null;
    disabled?: boolean;
}

/** Get a nested value from a limits object by section.field path */
const getNestedValue = (
    limits: SkillGlobalLimits | Record<string, any> | null | undefined,
    section: string,
    field: string,
): number | undefined => {
    if (!limits) return undefined;
    const sec = (limits as Record<string, any>)[section] as Record<string, any> | undefined;
    return sec?.[field] ?? undefined;
};

/**
 * Pure presentational renderer for the skill execution-limit fields.
 *
 * Holds no fetch/save state — it renders `value`, surfaces inherited /
 * capacity indicators, and emits the next `SkillGlobalLimits` object
 * through `onChange`. The section help modal and its per-field capacity
 * popover are local UI state (no I/O), matching the presentational-form
 * pattern used by `MCPLimitsForm`.
 */
const SkillLimitsForm: React.FC<SkillLimitsFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    capacity = null,
    disabled = false,
}) => {
    // Help modal state
    const [showHelp, setShowHelp] = useState(false);

    // Capacity popover state
    const [capacityPopoverField, setCapacityPopoverField] = useState<string | null>(null);

    // Inherited / capacity indicators only apply when the group has an
    // ancestor — signalled by the presence of an inherited or capacity object.
    const hasParent = inherited != null || capacity != null;

    // Update a nested field and emit the next limits object
    const updateLimit = (section: keyof SkillGlobalLimits, field: string, val: any) => {
        const next = {
            ...value,
            [section]: {
                ...((value as Record<string, any>)[section] || {}),
                [field]: val,
            },
        };
        onChange(next);
    };

    /**
     * One limit input. The inherited badge, capacity breakdown and
     * over-limit warning all live in the shared `InheritedLimitField`,
     * which the LLM and MCP limits forms render too — one renderer, not
     * three copies of the same 120 lines.
     */
    const renderLimitInput = (
        label: string,
        section: keyof SkillGlobalLimits,
        field: string,
        opts: { step?: string; isPercent?: boolean; min?: string } = {}
    ) => {
        const suffix = opts.isPercent ? '%' : '';
        const fieldKey = `${section}.${field}`;
        return (
            <InheritedLimitField
                label={label}
                value={getNestedValue(value, section, field)}
                onChange={(v) => updateLimit(section, field, v)}
                inherited={getNestedValue(inherited, section, field)}
                effective={getNestedValue(effective, section, field)}
                capacity={fieldCapacity(capacity, section, field)}
                hasParent={hasParent}
                suffix={suffix}
                step={opts.step || (opts.isPercent ? '1' : '0.01')}
                min={opts.min || '0'}
                disabled={disabled}
                fieldKey={fieldKey}
                popoverOpen={capacityPopoverField === fieldKey}
                onTogglePopover={setCapacityPopoverField}
            />
        );
    };


    return (
        <>
            {/* Limits Card */}
            <div className="grid grid-cols-1 gap-6">
                {/* Execution Limits */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <div className="flex items-center justify-between mb-4">
                        <div className="flex items-center gap-3">
                            <Clock className="w-5 h-5 text-primary" />
                            <h3 className="font-medium text-default">Execution Limits</h3>
                        </div>
                        <button
                            onClick={() => setShowHelp(true)}
                            className="text-muted hover:text-primary transition-colors"
                            title="Help"
                        >
                            <HelpCircle className="w-5 h-5" />
                        </button>
                    </div>

                    <div className="space-y-4">
                        {renderLimitInput('Daily Limit', 'execution', 'daily_limit', { step: '1' })}
                        {renderLimitInput('Monthly Limit', 'execution', 'monthly_limit', { step: '1' })}
                        {renderLimitInput('Daily Warning (%)', 'execution', 'daily_warning_percent', { isPercent: true })}
                        {renderLimitInput('Monthly Warning (%)', 'execution', 'monthly_warning_percent', { isPercent: true })}
                    </div>
                </div>
            </div>

            {/* Help Modal */}
            <GroupSkillLimitsHelpModal
                isOpen={showHelp}
                onClose={() => setShowHelp(false)}
            />
        </>
    );
};

export default SkillLimitsForm;
