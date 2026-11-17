/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Persisted per-column widths for a resizable table.
 *
 * Two things make this worth a hook rather than local state. Column widths
 * are a *workspace* preference — an operator who widens Outcome because their
 * systems return long terminal states expects it still widened tomorrow — and
 * the merge rules are easy to get wrong in a way that only bites later:
 *
 *   - stored keys that no longer exist are dropped, so a removed column does
 *     not leave a phantom width behind;
 *   - columns added after a user last saved fall back to their default rather
 *     than collapsing to zero;
 *   - anything non-finite or out of range is discarded, so a corrupted entry
 *     cannot render a table with an unusable column.
 */

import { useCallback, useEffect, useState } from 'react';
import { errorKind } from '../lib/narrow';

/** Narrow enough for a two-character count, wide enough to notice a drag. */
export const COLUMN_MIN_WIDTH = 56;
/** A column past this is almost always an accident of a runaway drag. */
export const COLUMN_MAX_WIDTH = 800;

export function clampColumnWidth(px: number): number {
    return Math.min(COLUMN_MAX_WIDTH, Math.max(COLUMN_MIN_WIDTH, Math.round(px)));
}

export interface ColumnWidths<K extends string> {
    /** Current width for every known column, defaults filled in. */
    widths: Record<K, number>;
    /** Set one column's width (clamped). */
    setWidth: (key: K, px: number) => void;
    /** Return one column to its default — the double-click affordance. */
    resetWidth: (key: K) => void;
    /** Return every column to its default. */
    resetAll: () => void;
    /** True when anything differs from the defaults; gates the reset control. */
    isCustomised: boolean;
}

export function useColumnWidths<K extends string>(
    storageKey: string,
    defaults: Record<K, number>,
): ColumnWidths<K> {
    const read = useCallback((): Record<K, number> => {
        try {
            const raw = localStorage.getItem(storageKey);
            if (!raw) return { ...defaults };
            const stored = JSON.parse(raw) as Record<string, unknown>;
            const merged = { ...defaults };
            for (const key of Object.keys(defaults) as K[]) {
                const v = stored[key];
                if (typeof v === 'number' && Number.isFinite(v)) {
                    merged[key] = clampColumnWidth(v);
                }
            }
            return merged;
        } catch (error) {
            console.error(`Failed to read ${storageKey} from localStorage:`, errorKind(error));
            return { ...defaults };
        }
        // `defaults` is a literal at every call site; depending on its identity
        // would re-read on every render.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [storageKey]);

    const [widths, setWidths] = useState<Record<K, number>>(read);

    useEffect(() => {
        try {
            localStorage.setItem(storageKey, JSON.stringify(widths));
        } catch (error) {
            console.error(`Failed to save ${storageKey} to localStorage:`, errorKind(error));
        }
    }, [storageKey, widths]);

    const setWidth = useCallback((key: K, px: number) => {
        setWidths((prev) => ({ ...prev, [key]: clampColumnWidth(px) }));
    }, []);

    const resetWidth = useCallback(
        (key: K) => setWidths((prev) => ({ ...prev, [key]: defaults[key] })),
        [defaults],
    );

    const resetAll = useCallback(() => setWidths({ ...defaults }), [defaults]);

    const isCustomised = (Object.keys(defaults) as K[]).some((k) => widths[k] !== defaults[k]);

    return { widths, setWidth, resetWidth, resetAll, isCustomised };
}

export default useColumnWidths;
