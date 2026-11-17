/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Click-to-open help marker for section headings and table columns.
 *
 * Used instead of `<span title={hint}>`: a native title only appears
 * after a hover delay and never on click or touch, so a "?" carrying one
 * reads as broken. This renders a 16px HelpCircle that opens a real
 * popover with the help text; closes on outside click or Escape.
 *
 * ## Why the panel is a portal
 *
 * Absolute positioning inside the marker is fine in open page flow and
 * wrong everywhere else: on the rightmost column of a table the panel runs
 * off the edge, and because the table sits in an `overflow-x-auto` wrapper
 * the overflowing part is *clipped* rather than merely off-screen — the
 * text is unreachable at any scroll position.
 *
 * So the panel renders into `document.body` with fixed coordinates measured
 * from the marker. That escapes every ancestor's overflow, and lets it flip
 * to right-aligned near the viewport edge and upward near the bottom, which
 * an `absolute` panel cannot do without knowing where it ended up.
 */

import { useCallback, useRef, useState } from 'react';
import { HelpCircle } from 'lucide-react';
import { AnchoredPopover } from './AnchoredPopover';

interface HelpTipProps {
    /** The help text shown in the popover. */
    text: string;
    /** Optional heading inside the popover (defaults to none). */
    title?: string;
    /** Accessible name; defaults to "What does this mean?". */
    ariaLabel?: string;
    className?: string;
    /** Icon size classes; defaults to the 16px marker. Dense surfaces (card
     *  KPI labels) pass a smaller one. */
    iconClassName?: string;
    /** Test id of the button; the popover gets `${testId}-popover`. */
    testId?: string;
}

/** Positioning, portal and close behaviour live in `AnchoredPopover`. */
export function HelpTip({
    text,
    title,
    ariaLabel,
    className = '',
    iconClassName = 'w-4 h-4',
    testId = 'help-tip',
}: HelpTipProps) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLSpanElement>(null);
    const close = useCallback(() => setOpen(false), []);

    return (
        <span ref={ref} className={`relative inline-flex ${className}`}>
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                aria-label={ariaLabel || 'What does this mean?'}
                data-testid={testId}
                className="text-muted hover:text-secondary transition-colors"
            >
                <HelpCircle className={iconClassName} />
            </button>
            <AnchoredPopover
                anchorRef={ref}
                open={open}
                onClose={close}
                testId={`${testId}-popover`}
                className="space-y-1.5"
            >
                {title && <span className="block text-xs font-semibold text-default">{title}</span>}
                <span className="block text-xs text-secondary font-normal leading-relaxed whitespace-normal">
                    {text}
                </span>
            </AnchoredPopover>
        </span>
    );
}
