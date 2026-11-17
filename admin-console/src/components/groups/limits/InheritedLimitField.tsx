/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One numeric limit input that shows what it inherits and how much of the
 * shared budget is left.
 *
 * Every group-wide limits form needs the same three facts on every field:
 * the ceiling inherited from ancestors, the capacity still unclaimed under
 * the group that holds the budget, and whether what is typed exceeds
 * either. This one implementation serves the LLM, MCP and Skill forms, so
 * a limit under a capped organisation never shows a bare "No limit" box on
 * any of them.
 *
 * The API refuses a limit looser than the inherited floor (HTTP 400), so
 * the over-inherited state here is a prediction of that refusal, shown
 * while the operator types rather than after they save. Over-*capacity* is
 * a softer thing: over-subscribing siblings is a real strategy, and the
 * server allows it unless the immediate parent is the budget holder, so it
 * is drawn as a warning that states the arithmetic.
 */

import React from 'react';
import { ArrowDown, Info } from 'lucide-react';
import {
    type FieldCapacity,
    limitFieldVerdict,
} from '../../../lib/limitCapacity';

export interface InheritedLimitFieldProps {
    label: string;
    value: number | undefined;
    onChange: (next: number | undefined) => void;
    /** Ceiling merged from every ancestor. */
    inherited?: number;
    /** This group's value after the restrictive merge. */
    effective?: number;
    capacity?: FieldCapacity | null;
    /** Root groups inherit nothing; the badges stay off. */
    hasParent?: boolean;
    suffix?: string;
    /** Unit shown after the label, as its own element — keeps the label's
     *  own text node intact for callers (and tests) that match on it. */
    suffixNote?: string;
    step?: string;
    min?: string;
    disabled?: boolean;
    /** Stable id for tests and for the capacity popover's open state. */
    fieldKey: string;
    popoverOpen?: boolean;
    onTogglePopover?: (key: string | null) => void;
    /** Compact rendering for the dense four-across grids. */
    dense?: boolean;
    /** What the limit does, shown under the input. */
    help?: string;
}

export const InheritedLimitField: React.FC<InheritedLimitFieldProps> = ({
    label,
    value,
    onChange,
    inherited,
    effective,
    capacity,
    hasParent = true,
    suffix = '',
    suffixNote,
    step,
    min,
    disabled,
    fieldKey,
    popoverOpen = false,
    onTogglePopover,
    dense = false,
    help,
}) => {
    const hasInherited = inherited !== undefined && hasParent;
    const cap = hasParent ? capacity ?? null : null;
    const hasCapacity = cap?.available !== undefined;
    const { overInherited, overCapacity, ceiling } = limitFieldVerdict(value, hasInherited ? inherited : undefined, cap);
    const isOverLimit = overInherited || overCapacity;

    const labelCls = dense ? 'block text-xs text-muted mb-1' : 'block text-sm font-medium text-secondary mb-1';
    const inputCls = dense ? 'w-full px-2 py-1.5 text-sm border rounded' : 'w-full px-3 py-2 border rounded-lg';

    return (
        <div data-testid={`limit-field-${fieldKey}`}>
            <label className={labelCls}>
                {label}
                {suffixNote ? <span className="text-muted ml-1">({suffixNote})</span> : null}
                {hasCapacity && (
                    <span
                        className={`ml-1.5 inline-flex items-center text-xs ${isOverLimit ? 'text-red-600 font-semibold' : 'text-amber-600'}`}
                        data-testid={`limit-available-${fieldKey}`}
                    >
                        <ArrowDown className="w-3 h-3 mr-0.5" />
                        available {cap?.available}{suffix} of {cap?.holderLimit}{suffix}
                    </span>
                )}
                {!hasCapacity && hasInherited && (
                    <span
                        className={`ml-1.5 inline-flex items-center text-xs ${overInherited ? 'text-red-600 font-semibold' : 'text-amber-600'}`}
                        data-testid={`limit-inherited-${fieldKey}`}
                    >
                        <ArrowDown className="w-3 h-3 mr-0.5" />
                        max {inherited}{suffix}
                    </span>
                )}
                {hasCapacity && onTogglePopover && (
                    <button
                        type="button"
                        onClick={() => onTogglePopover(popoverOpen ? null : fieldKey)}
                        className="ml-1 text-muted hover:text-primary align-middle"
                        title="Show capacity breakdown"
                        data-testid={`limit-capacity-toggle-${fieldKey}`}
                    >
                        <Info className="w-3 h-3" />
                    </button>
                )}
            </label>
            <input
                type="number"
                step={step ?? '1'}
                min={min ?? '0'}
                max={ceiling}
                value={value ?? ''}
                onChange={(e) => onChange(e.target.value ? parseFloat(e.target.value) : undefined)}
                disabled={disabled}
                placeholder={
                    hasCapacity
                        ? `Available: ${cap?.available}`
                        : hasInherited
                          ? `Inherited: ${inherited}`
                          : 'No limit'
                }
                data-testid={`limit-input-${fieldKey}`}
                className={`${inputCls} bg-surface text-default disabled:opacity-50 focus:ring-2 ${
                    isOverLimit
                        ? 'border-red-500 focus:ring-red-500 focus:border-red-500'
                        : hasCapacity || hasInherited
                          ? 'border-default bg-surface-alt/30 focus:ring-amber-500 focus:border-amber-500'
                          : 'border-strong focus:ring-primary focus:border-primary'
                }`}
            />
            {isOverLimit && (
                <p className="text-xs text-red-600 mt-0.5 font-medium" data-testid={`limit-error-${fieldKey}`}>
                    {overInherited
                        ? `Above the inherited limit of ${inherited}${suffix} — the save will be rejected`
                        : `Above the ${cap?.available}${suffix} still unclaimed — ${cap?.holderName ?? 'the parent group'} allows ${cap?.holderLimit}${suffix} and other groups have taken ${cap?.claimed ?? 0}${suffix}`}
                </p>
            )}
            {help && (
                <p className="text-xs text-muted mt-1" data-testid={`limit-help-${fieldKey}`}>
                    {help}
                </p>
            )}
            {/* The effective value is only news when this group set its own
                and the merge tightened it. With nothing set, the
                placeholder already reads "Inherited: X". */}
            {!isOverLimit && value !== undefined && effective !== undefined && effective !== value && (
                <span className="text-xs text-muted mt-0.5 block" data-testid={`limit-effective-${fieldKey}`}>
                    Effective: {effective}{suffix}
                </span>
            )}
            {popoverOpen && hasCapacity && cap && (
                <div className="mt-1.5 p-3 bg-surface-alt border border-default rounded-lg text-xs space-y-1.5 shadow-sm" data-testid={`limit-capacity-${fieldKey}`}>
                    <div className="flex justify-between">
                        <span className="text-secondary">
                            {cap.holderIsParent ? 'Parent' : 'Set by'} &ldquo;{cap.holderName}&rdquo;:
                        </span>
                        <span className="font-medium">{cap.holderLimit}{suffix}</span>
                    </div>
                    {cap.others.length > 0 && (
                        <>
                            <div className="text-muted font-medium pt-1">Also drawing on it:</div>
                            {cap.others.map((o) => (
                                <div key={o.id} className="flex justify-between pl-3">
                                    <span className="text-secondary">{o.name}:</span>
                                    <span>{o.value !== undefined ? `${o.value}${suffix}` : '—'}</span>
                                </div>
                            ))}
                            <div className="flex justify-between border-t border-default pt-1.5">
                                <span className="text-secondary">Already claimed:</span>
                                <span className="font-medium">{cap.claimed ?? 0}{suffix}</span>
                            </div>
                        </>
                    )}
                    <div className="flex justify-between border-t border-default pt-1.5">
                        <span className="font-semibold text-default">Available for this group:</span>
                        <span className="font-semibold text-green-700">{cap.available}{suffix}</span>
                    </div>
                </div>
            )}
        </div>
    );
};

export default InheritedLimitField;
