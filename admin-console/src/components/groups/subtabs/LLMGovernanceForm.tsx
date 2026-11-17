/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect } from 'react';
import {
    Thermometer,
    Maximize,
    Wrench,
    MessageSquare,
    ArrowDown
} from 'lucide-react';
import { LLMGovernance, FragmentPosition } from '../../../lib/groupTypes';
import {
    governanceRelaxations,
    relaxationsByPath,
    type GovernanceRelaxation,
} from '../../../lib/governanceInheritance';
import NotInThisRelease from '../../common/NotInThisRelease';

interface LLMGovernanceFormProps {
    value: LLMGovernance;
    onChange: (next: LLMGovernance) => void;
    inherited?: LLMGovernance | null;   // for the inherited-value badges
    effective?: LLMGovernance | null;    // optional effective display
    disabled?: boolean;
    /** Raised whenever the edited policy would relax something it
     *  inherits. The owning tab blocks Save on a non-empty list — the API
     *  refuses the same values with a 400, and an operator should learn
     *  that while typing, not after pressing Save. */
    onRelaxationsChange?: (relaxations: GovernanceRelaxation[]) => void;
}

/** Get a nested value from a governance object by section.field path */
const getNestedValue = (gov: LLMGovernance | null | undefined, section: keyof LLMGovernance, field: string): any => {
    if (!gov) return undefined;
    const sec = gov[section] as Record<string, any> | undefined;
    return sec?.[field] ?? undefined;
};

const enforceOptions = [
    { value: 'clamp', label: 'Clamp (adjust to limit)' },
    { value: 'reject', label: 'Reject (block request)' },
];

// A model's tool calls are allowed or refused. The gateway holds nothing for
// a person on them, and the Control Plane refuses an approval there.
const accessOptions = [
    { value: 'enabled', label: 'Enabled' },
    { value: 'disabled', label: 'Disabled' },
];

/**
 * Pure presentational renderer for the LLM governance field sections.
 *
 * Holds no state and performs no I/O — it renders `value`, surfaces
 * inherited-value badges from `inherited`, and emits the next
 * `LLMGovernance` object through `onChange`. The owning tab keeps
 * load/save/help behavior.
 */
const LLMGovernanceForm: React.FC<LLMGovernanceFormProps> = ({
    value,
    onChange,
    inherited = null,
    effective = null,
    disabled = false,
    onRelaxationsChange,
}) => {
    // Inherited badges only apply when an inherited governance object is
    // present (which only happens when the group has an ancestor with
    // governance configured).
    const hasParent = inherited != null;

    // Every field that is currently looser than what this group inherits.
    // Recomputed on each render from the two objects, so it can never
    // drift out of step with what is on screen.
    const relaxations = governanceRelaxations(value, inherited, 'llm');
    const relaxationFor = relaxationsByPath(relaxations);
    const relaxationKey = relaxations.map((r) => r.path).join('|');
    useEffect(() => {
        onRelaxationsChange?.(relaxations);
        // `relaxationKey` stands in for the list: it changes exactly when
        // the set of offending fields does, which keeps the callback from
        // firing on every keystroke that leaves the verdict unchanged.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [relaxationKey]);

    // Update a nested field and emit the next governance object
    const inputCeiling = value.context_window?.max_input_tokens != null;

    const updateField = (section: keyof LLMGovernance, field: string, val: any) => {
        const next: LLMGovernance = {
            ...value,
            [section]: {
                ...(value[section] || {}),
                [field]: val
            }
        };
        onChange(next);
    };

    /** Render a number input with inheritance indicator */
    const renderNumberInput = (
        label: string,
        section: keyof LLMGovernance,
        field: string,
        opts: { step?: string; min?: string; max?: string; placeholder?: string } = {}
    ) => {
        const inheritedValue = getNestedValue(inherited, section, field);
        const effectiveValue = getNestedValue(effective, section, field);
        const current = getNestedValue(value, section, field);
        const hasInherited = inheritedValue !== undefined && hasParent;
        const path = `${section}.${field}`;
        const relaxed = relaxationFor[path];

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    {label}
                    {hasInherited && (
                        <span className={`ml-1.5 inline-flex items-center text-xs ${relaxed ? 'text-red-600 font-semibold' : 'text-primary'}`} title={`Inherited: ${inheritedValue}`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            max {inheritedValue}
                        </span>
                    )}
                </label>
                <input
                    type="number"
                    step={opts.step || '1'}
                    min={opts.min || '0'}
                    max={opts.max || (hasInherited ? String(inheritedValue) : undefined)}
                    value={current ?? ''}
                    onChange={(e) => updateField(section, field, e.target.value ? parseFloat(e.target.value) : undefined)}
                    disabled={disabled}
                    placeholder={hasInherited ? `Inherited: ${inheritedValue}` : (opts.placeholder || 'No limit')}
                    aria-invalid={relaxed ? true : undefined}
                    data-testid={`governance-input-${path}`}
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 ${
                        relaxed
                            ? 'border-red-500 focus:ring-red-500 focus:border-red-500'
                            : hasInherited
                              ? 'border-primary bg-primary-pale/30 focus:ring-primary focus:border-primary'
                              : 'border-strong focus:ring-primary focus:border-primary'
                    }`}
                />
                {relaxed && (
                    <p className="text-xs text-red-600 mt-0.5 font-medium" data-testid={`governance-error-${path}`}>
                        {relaxed.message}
                    </p>
                )}
                {!relaxed && hasInherited && effectiveValue !== undefined && current !== undefined && effectiveValue !== current && (
                    <p className="text-xs text-muted mt-0.5">
                        Effective: {effectiveValue}
                    </p>
                )}
            </div>
        );
    };

    /** Render a select input with inheritance indicator */
    const renderSelectInput = (
        label: string,
        section: keyof LLMGovernance,
        field: string,
        options: { value: string; label: string }[]
    ) => {
        const inheritedValue = getNestedValue(inherited, section, field);
        const current = getNestedValue(value, section, field);
        const hasInherited = inheritedValue !== undefined && hasParent;
        const path = `${section}.${field}`;
        const relaxed = relaxationFor[path];

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    {label}
                    {hasInherited && (
                        <span className={`ml-1.5 inline-flex items-center text-xs ${relaxed ? 'text-red-600 font-semibold' : 'text-primary'}`} title={`Inherited: ${inheritedValue}`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            inherited: {inheritedValue}
                        </span>
                    )}
                </label>
                <select
                    value={current ?? ''}
                    onChange={(e) => updateField(section, field, e.target.value || undefined)}
                    disabled={disabled}
                    aria-invalid={relaxed ? true : undefined}
                    data-testid={`governance-input-${path}`}
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 ${
                        relaxed
                            ? 'border-red-500 focus:ring-red-500 focus:border-red-500'
                            : hasInherited
                              ? 'border-primary bg-primary-pale/30 focus:ring-primary focus:border-primary'
                              : 'border-strong focus:ring-primary focus:border-primary'
                    }`}
                >
                    <option value="">{hasInherited ? `Inherited: ${inheritedValue}` : 'Not set'}</option>
                    {options.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                </select>
                {relaxed && (
                    <p className="text-xs text-red-600 mt-0.5 font-medium" data-testid={`governance-error-${path}`}>
                        {relaxed.message}
                    </p>
                )}
            </div>
        );
    };

    /** Render a textarea for list fields (one entry per line) */
    const renderTextareaList = (
        label: string,
        section: keyof LLMGovernance,
        field: string,
        placeholder: string
    ) => {
        const inheritedValue = getNestedValue(inherited, section, field) as string[] | undefined;
        const current = getNestedValue(value, section, field) as string[] | undefined;
        const hasInherited = inheritedValue !== undefined && inheritedValue.length > 0 && hasParent;

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    {label}
                    {hasInherited && (
                        <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedValue.length} entries`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            {inheritedValue.length} inherited
                        </span>
                    )}
                </label>
                <textarea
                    rows={3}
                    value={(current || []).join('\n')}
                    onChange={(e) => {
                        const lines = e.target.value.split('\n');
                        const filtered = lines.length === 1 && lines[0] === '' ? undefined : lines;
                        updateField(section, field, filtered);
                    }}
                    disabled={disabled}
                    placeholder={placeholder}
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary text-sm font-mono ${
                        hasInherited ? 'border-primary bg-primary-pale/30' : 'border-strong'
                    }`}
                />
                <p className="text-xs text-muted mt-0.5">One entry per line</p>
            </div>
        );
    };

    /** Render mandatory_fragments textarea */
    const renderFragments = () => {
        const inheritedValue = getNestedValue(inherited, 'system_prompt', 'mandatory_fragments') as Array<{ position: FragmentPosition; text: string }> | undefined;
        const current = (value.system_prompt?.mandatory_fragments || []) as Array<{ position: FragmentPosition; text: string }>;
        const hasInherited = inheritedValue !== undefined && inheritedValue.length > 0 && hasParent;

        // Serialize: [prepend] text or [append] text
        const serialized = current.map(f => `[${f.position}] ${f.text}`).join('\n');

        const handleChange = (val: string) => {
            const lines = val.split('\n').filter(l => l.trim());
            const fragments: Array<{ position: FragmentPosition; text: string }> = lines.map(line => {
                const match = line.match(/^\[(prepend|append)\]\s*(.*)/i);
                if (match) {
                    return { position: match[1].toLowerCase() as FragmentPosition, text: match[2] };
                }
                // Default to append if no prefix
                return { position: 'append' as FragmentPosition, text: line.trim() };
            }).filter(f => f.text);

            updateField('system_prompt', 'mandatory_fragments', fragments.length > 0 ? fragments : undefined);
        };

        return (
            <div>
                <label className="block text-sm font-medium text-secondary mb-1">
                    Mandatory Fragments
                    {hasInherited && (
                        <span className="ml-1.5 inline-flex items-center text-xs text-primary" title={`Inherited: ${inheritedValue.length} fragments`}>
                            <ArrowDown className="w-3 h-3 mr-0.5" />
                            {inheritedValue.length} inherited
                        </span>
                    )}
                </label>
                <textarea
                    rows={4}
                    value={serialized}
                    onChange={(e) => handleChange(e.target.value)}
                    disabled={disabled}
                    placeholder="[prepend] You must always respond in English.\n[append] Always cite your sources."
                    className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-primary focus:border-primary text-sm font-mono ${
                        hasInherited ? 'border-primary bg-primary-pale/30' : 'border-strong'
                    }`}
                />
                <p className="text-xs text-muted mt-0.5">
                    One fragment per line. Prefix with [prepend] or [append] (default: append).
                </p>
            </div>
        );
    };

    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Temperature */}
            <div className="bg-surface border border-default rounded-lg p-6">
                <div className="flex items-center gap-3 mb-4">
                    <Thermometer className="w-5 h-5 text-muted" />
                    <h3 className="font-medium text-default">Temperature</h3>
                </div>
                <div className="space-y-4">
                    {renderNumberInput('Max Temperature', 'temperature', 'max', { step: '0.1', min: '0', max: '2' })}
                    {renderSelectInput('Enforce Mode', 'temperature', 'enforce', enforceOptions)}
                </div>
            </div>

            {/* Context Window */}
            <div className="bg-surface border border-default rounded-lg p-6">
                <div className="flex items-center gap-3 mb-4">
                    <Maximize className="w-5 h-5 text-primary" />
                    <h3 className="font-medium text-default">Context Window</h3>
                </div>
                <div className="space-y-4">
                    {renderNumberInput('Max Input Tokens', 'context_window', 'max_input_tokens')}
                    {renderNumberInput('Max Output Tokens', 'context_window', 'max_output_tokens')}
                    {/* With an input ceiling a larger request is refused: the
                        gateway cannot cut a request's input to the ceiling,
                        so clamp is offered for an output ceiling alone. */}
                    {renderSelectInput(
                        'Enforce Mode',
                        'context_window',
                        'enforce',
                        inputCeiling ? enforceOptions.filter((option) => option.value !== 'clamp') : enforceOptions,
                    )}
                    {inputCeiling && value.context_window?.enforce === 'clamp' && (
                        <NotInThisRelease
                            testId="context-window-clamp-unavailable"
                            label="Clamp with an input ceiling"
                            value="stored"
                            consequence="Choose Reject to save this policy."
                        />
                    )}
                </div>
            </div>

            {/* Tool Use */}
            <div className="bg-surface border border-default rounded-lg p-6">
                <div className="flex items-center gap-3 mb-4">
                    <Wrench className="w-5 h-5 text-muted" />
                    <h3 className="font-medium text-default">Tool Use</h3>
                </div>
                <div className="space-y-4">
                    {renderSelectInput('Access', 'tool_use', 'access', accessOptions)}
                    <p className="text-xs text-muted" data-testid="tool-use-no-approval">
                        A model&apos;s tool calls are not held for approval. To have a person approve a call, put the approval on the
                        tool itself: an MCP capability, a skill, or an argument policy on the tool&apos;s own surface.
                    </p>
                    {renderTextareaList('Allowed Tools', 'tool_use', 'allowed_tools', 'tool_name_1\ntool_name_2')}
                    {renderTextareaList('Blocked Tools', 'tool_use', 'blocked_tools', 'tool_name_1\ntool_name_2')}
                    {renderNumberInput('Max Tool Calls Per Request', 'tool_use', 'max_tool_calls_per_request')}
                    {renderNumberInput('Max Tool Calls Per Hour', 'tool_use', 'max_tool_calls_per_hour')}
                    {renderNumberInput('Max Tool Calls Per Day', 'tool_use', 'max_tool_calls_per_day')}
                    {renderTextareaList('Banned Argument Patterns', 'tool_use', 'banned_argument_patterns', 'regex_pattern_1\nregex_pattern_2')}
                </div>
            </div>

            {/* System Prompt */}
            <div className="bg-surface border border-default rounded-lg p-6">
                <div className="flex items-center gap-3 mb-4">
                    <MessageSquare className="w-5 h-5 text-green-600" />
                    <h3 className="font-medium text-default">System Prompt</h3>
                </div>
                <div className="space-y-4">
                    {renderFragments()}
                    {renderTextareaList('Banned Patterns', 'system_prompt', 'banned_patterns', 'regex_pattern_1\nregex_pattern_2')}
                </div>
            </div>
        </div>
    );
};

export default LLMGovernanceForm;
