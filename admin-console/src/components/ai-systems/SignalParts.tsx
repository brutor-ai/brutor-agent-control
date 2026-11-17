/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The building blocks of the Signals sub-tabs — a section heading with its
 * "observed" marker, a figure tile with its "?" and a table header cell —
 * shared by the runs tab and the outcome signal sections so they read alike.
 */

import type React from 'react';

import { HelpTip } from '../ui/HelpTip';
import { TABLE_TH_TYPE } from '../ui/tableStyles';

export function SignalHeading({ title, hint }: { title: string; hint: string }) {
    return (
        <div className="flex items-start gap-2 mb-3">
            <h3 className="text-sm font-semibold text-default">{title}</h3>
            {/* Read-only marker. The Liveness card carries the matching
                "expectation · editable" chip — the page mixes observation
                with one configurable expectation, and the chips are how an
                operator tells which is which. */}
            <span className="px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider bg-surface-alt text-muted border border-default">
                observed
            </span>
            <HelpTip title={title} text={hint} />
        </div>
    );
}

export function Th({ children }: { children: React.ReactNode }) {
    return <th className={`${TABLE_TH_TYPE} px-4 py-2 text-left`}>{children}</th>;
}

export function Tile({
    label,
    value,
    hint,
    tone,
    testId,
    help,
}: {
    label: string;
    value: string;
    hint?: string;
    /** `neutral` is the no-work grey: neither good nor bad. */
    tone?: 'warn' | 'unknown' | 'neutral';
    testId?: string;
    /** A "?" beside the label: what the number is and how it is computed. */
    help?: string;
}) {
    const valueClass =
        tone === 'warn'
            ? 'text-amber-700'
            : tone === 'unknown'
              ? 'text-muted italic'
              : tone === 'neutral'
                ? 'text-slate-600 dark:text-slate-300'
                : 'text-default';
    return (
        <div className="p-4 border border-default rounded-lg bg-surface" data-testid={testId}>
            <div className="flex items-center gap-1.5 text-xs text-muted uppercase tracking-wider">
                {label}
                {help && <HelpTip title={label} text={help} ariaLabel={`What ${label} means`} className="normal-case tracking-normal" />}
            </div>
            <div className={`text-xl font-semibold mt-1 ${valueClass}`}>{value}</div>
            {hint && <div className="text-xs text-muted mt-1">{hint}</div>}
        </div>
    );
}
