/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A panel anchored to a trigger element, rendered into `document.body`.
 *
 * This is the one place that knows how to position a popover so it is never
 * clipped. A panel absolutely positioned inside its trigger is fine in open
 * page flow and wrong everywhere else: inside a table (which always sits in
 * an `overflow-*` wrapper) or a scrolling panel, the part that extends past
 * the container is CLIPPED, not merely off-screen — on a short table the help
 * text below the last row simply disappears.
 *
 * The panel therefore renders into `document.body` with fixed coordinates
 * measured from the anchor. That escapes every ancestor's overflow; it opens
 * below and left-aligned by default, flips to right-aligned near the right
 * edge and above near the bottom, and follows the anchor when any ancestor
 * scrolls. Closes on outside click (anchor and panel are "inside") and Escape.
 *
 * Use this for every click-to-open panel that can live inside a table or a
 * scroll container. `tests/.../popoverPlacement.test.ts` fails when a table
 * component hand-rolls an `absolute … top-full` panel instead.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { createPortal } from 'react-dom';

/** Breathing room kept between the panel and the viewport edge. */
const MARGIN = 8;
/** Gap between the anchor and the panel. */
const OFFSET = 6;

export interface AnchoredPopoverProps {
    /** The element the panel is positioned against (usually the trigger). */
    anchorRef: RefObject<HTMLElement | null>;
    open: boolean;
    onClose: () => void;
    /** Panel width in px; also used to decide which edge to align to. */
    width?: number;
    className?: string;
    testId?: string;
    children: ReactNode;
}

export function AnchoredPopover({
    anchorRef,
    open,
    onClose,
    width = 288,
    className = '',
    testId,
    children,
}: AnchoredPopoverProps) {
    const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
    const panelRef = useRef<HTMLDivElement>(null);

    const place = useCallback(() => {
        const a = anchorRef.current?.getBoundingClientRect();
        if (!a) return;
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        // Prefer opening rightward from the anchor; flip to right-aligned when
        // that would cross the viewport edge, then clamp so a narrow viewport
        // still lands inside rather than off the left.
        let left = a.left;
        if (left + width > vw - MARGIN) left = a.right - width;
        left = Math.max(MARGIN, Math.min(left, vw - width - MARGIN));

        // Below by default; above when the panel would run past the fold. Its
        // height is only known once rendered, hence the layout effect.
        const h = panelRef.current?.offsetHeight ?? 0;
        let top = a.bottom + OFFSET;
        if (h && top + h > vh - MARGIN) top = Math.max(MARGIN, a.top - OFFSET - h);

        setPos({ top, left });
    }, [anchorRef, width]);

    // Measure before paint on every open. No reset on close is needed: a
    // closed popover renders nothing, and the next open re-measures here
    // before the browser paints, so a stale position is never visible.
    useLayoutEffect(() => {
        if (open) place();
    }, [open, place]);

    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            const t = e.target as Node;
            if (anchorRef.current?.contains(t) || panelRef.current?.contains(t)) return;
            onClose();
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        // Capture phase so an ancestor scrolling container moves the panel too,
        // not just the window — a fixed panel would otherwise detach from its
        // anchor as the table scrolls.
        const onScroll = () => place();
        document.addEventListener('mousedown', onDown);
        document.addEventListener('keydown', onKey);
        window.addEventListener('scroll', onScroll, true);
        window.addEventListener('resize', onScroll);
        return () => {
            document.removeEventListener('mousedown', onDown);
            document.removeEventListener('keydown', onKey);
            window.removeEventListener('scroll', onScroll, true);
            window.removeEventListener('resize', onScroll);
        };
    }, [open, place, onClose, anchorRef]);

    if (!open) return null;
    return createPortal(
        <div
            ref={panelRef}
            data-testid={testId}
            style={{
                position: 'fixed',
                top: pos?.top ?? 0,
                left: pos?.left ?? 0,
                width,
                // Hidden for the one frame before it has been measured, so it
                // never flashes in the wrong place.
                visibility: pos ? 'visible' : 'hidden',
            }}
            className={`z-50 normal-case tracking-normal text-left bg-surface border border-default rounded-lg shadow-lg p-3 ${className}`}
        >
            {children}
        </div>,
        document.body,
    );
}
