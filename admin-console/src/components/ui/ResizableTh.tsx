/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A table header cell that can be dragged to a new width, with an optional
 * help marker.
 *
 * The drag listens on `document` rather than the handle so the pointer can
 * leave the 5px strip mid-drag without the column freezing — the common way
 * a hand-rolled resizer feels broken. Width is measured from the header
 * cell's own left edge, not from a delta, so a drag that starts after the
 * cursor has drifted still lands where the pointer is.
 *
 * Keyboard users get the same control: the handle is focusable and arrow
 * keys nudge the width, because a column an operator cannot read is an
 * accessibility problem, not a cosmetic one.
 */

import { useCallback, useRef } from 'react';
import { TABLE_TH_TYPE } from './tableStyles';

import { HelpTip } from './HelpTip';
import { clampColumnWidth, COLUMN_MIN_WIDTH } from '../../hooks/useColumnWidths';

/** How far one arrow-key press moves an edge. */
const KEY_STEP = 16;

interface ResizableThProps {
    children: React.ReactNode;
    /** Current width in px. */
    width: number;
    /** Called with the new width as the pointer moves. */
    onResize: (px: number) => void;
    /** Double-click on the handle returns the column to its default. */
    onReset?: () => void;
    /** Explanation shown behind an (i) marker beside the label. */
    help?: { title: string; text: string };
    /** Distinguishes the handles in tests. */
    testId?: string;
    className?: string;
}

export function ResizableTh({
    children,
    width,
    onResize,
    onReset,
    help,
    testId,
    className = '',
}: ResizableThProps) {
    const thRef = useRef<HTMLTableCellElement>(null);

    const startDrag = useCallback(
        (e: React.MouseEvent) => {
            // Header cells may sit inside a clickable row; a resize is not a
            // click on the thing being resized.
            e.preventDefault();
            e.stopPropagation();
            const left = thRef.current?.getBoundingClientRect().left ?? 0;
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';

            const onMove = (ev: MouseEvent) => onResize(clampColumnWidth(ev.clientX - left));
            const onUp = () => {
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        },
        [onResize],
    );

    const onKeyDown = useCallback(
        (e: React.KeyboardEvent) => {
            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                onResize(clampColumnWidth(width - KEY_STEP));
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                onResize(clampColumnWidth(width + KEY_STEP));
            } else if ((e.key === 'Enter' || e.key === ' ') && onReset) {
                e.preventDefault();
                onReset();
            }
        },
        [onResize, onReset, width],
    );

    return (
        <th
            ref={thRef}
            style={{ width, minWidth: COLUMN_MIN_WIDTH }}
            className={`relative px-4 py-2 text-left ${TABLE_TH_TYPE} ${className}`}
        >
            <span className="flex items-center gap-1.5">
                <span className="truncate">{children}</span>
                {help && <HelpTip title={help.title} text={help.text} ariaLabel={help.title} />}
            </span>
            <span
                role="separator"
                aria-orientation="vertical"
                aria-label={`Resize column`}
                tabIndex={0}
                data-testid={testId}
                onMouseDown={startDrag}
                onDoubleClick={onReset}
                onKeyDown={onKeyDown}
                title="Drag to resize, double-click to reset"
                className="absolute top-0 right-0 h-full w-[5px] cursor-col-resize group focus:outline-none"
            >
                <span className="block w-[2px] h-full ml-auto bg-transparent group-hover:bg-primary-400/60 group-focus:bg-primary-500 transition-colors" />
            </span>
        </th>
    );
}

export default ResizableTh;
