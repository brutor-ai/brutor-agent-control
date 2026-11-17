/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One timespan, shared across every analytics surface.
 *
 * Mission Control, Analytics, AI Systems and Resource Groups all ask the
 * same question over the same traffic — "what happened, and in what
 * window?" An admin investigating a cost spike over 90 days should not
 * have to re-pick 90 days on each of the four pages, nor lose the choice
 * on reload. So the selection lives in ONE localStorage key that all of
 * them read and write.
 *
 * ── Why not `useLocalStorageString` ──────────────────────────────────
 * That hook keeps its value in per-instance `useState`. Two components
 * reading the same key don't see each other's writes, so a shared key
 * would silently desynchronise the moment two of these surfaces were
 * mounted together (a Resource Group usage tab with the member-usage
 * modal open over it, for instance). `useSyncExternalStore` subscribes
 * every consumer to one source of truth, and the `storage` event extends
 * that to other browser tabs for free.
 *
 * ── Surfaces support different windows ───────────────────────────────
 * Mission Control offers calendar periods (MTD, last month) because
 * FinOps reconciles against invoices, and invoices are calendar months.
 * The other surfaces are rolling-window only. A surface therefore
 * declares what it supports, and an unsupported stored value is
 * *displayed* as that surface's fallback WITHOUT being written back —
 * so picking MTD in Mission Control, glancing at AI Systems, and coming
 * back still shows MTD. Degrading the view must not destroy the choice.
 */

import { useCallback, useSyncExternalStore } from 'react';

/** Every window any surface can offer. */
export type Timespan = '24h' | '7d' | '30d' | '90d' | 'mtd' | 'lastm' | 'all';

/** The rolling windows every analytics surface understands. */
export const ROLLING_TIMESPANS = ['24h', '7d', '30d', '90d', 'all'] as const;

export const TIMESPAN_STORAGE_KEY = 'brutor-analytics-timespan';

/**
 * Hours for the fixed rolling windows, for the APIs that take
 * `period_hours` rather than a label. `all` is 0, which the metrics
 * endpoints read as "no lower bound".
 *
 * MTD and last-month are deliberately absent: their length depends on
 * today's date, so they cannot be a constant. Surfaces that work in
 * hours don't offer them.
 */
export const TIMESPAN_HOURS: Record<Exclude<Timespan, 'mtd' | 'lastm'>, number> = {
    '24h': 24,
    '7d': 168,
    '30d': 720,
    '90d': 2160,
    all: 0,
};

// ---------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------

const listeners = new Set<() => void>();

function readRaw(): string {
    try {
        return localStorage.getItem(TIMESPAN_STORAGE_KEY) ?? '';
    } catch {
        // Safari private mode, storage disabled by policy, quota games.
        // A timespan preference is not worth breaking a dashboard over.
        return '';
    }
}

function writeRaw(value: string): void {
    try {
        localStorage.setItem(TIMESPAN_STORAGE_KEY, value);
    } catch {
        // Ignore — the in-memory notification below still updates every
        // mounted consumer for this session; only persistence is lost.
    }
    // `storage` does not fire in the tab that performed the write, so
    // same-tab consumers are notified explicitly.
    listeners.forEach((l) => l());
}

function onStorageEvent(e: StorageEvent) {
    if (e.key === null || e.key === TIMESPAN_STORAGE_KEY) {
        listeners.forEach((l) => l());
    }
}

function subscribe(cb: () => void): () => void {
    if (listeners.size === 0) {
        window.addEventListener('storage', onStorageEvent);
    }
    listeners.add(cb);
    return () => {
        listeners.delete(cb);
        if (listeners.size === 0) {
            window.removeEventListener('storage', onStorageEvent);
        }
    };
}

// `readRaw` returns a string, so React's Object.is comparison is by
// value and no snapshot cache is needed to avoid re-render loops.
const getSnapshot = readRaw;

/**
 * Read and write the shared analytics timespan.
 *
 * @param supported the windows this surface can render
 * @param fallback  shown when nothing is stored yet, or when the stored
 *                  value is one this surface doesn't offer. Each surface
 *                  passes its own default, shown until the admin
 *                  actually picks something.
 * @param alsoAccept escape hatch for surfaces with an open-ended value
 *                  that can't be enumerated — Mission Control encodes a
 *                  custom calendar range as `custom:<start>:<end>`, and
 *                  those must survive a visit to a surface that only
 *                  does fixed windows.
 */
export function useTimespanPreference<T extends string>(
    supported: readonly T[],
    fallback: T,
    alsoAccept?: (raw: string) => boolean,
): [T, (value: T) => void] {
    const raw = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

    const usable =
        (supported as readonly string[]).includes(raw) || (raw !== '' && alsoAccept?.(raw) === true);
    const value = usable ? (raw as T) : fallback;

    const setValue = useCallback((next: T) => {
        writeRaw(next);
    }, []);

    return [value, setValue];
}

/**
 * Variant for surfaces whose API takes `period_hours`. Returns the label
 * for rendering the toggle and the hour count for the request.
 */
export function useTimespanHoursPreference(
    supported: readonly Exclude<Timespan, 'mtd' | 'lastm'>[],
    fallback: Exclude<Timespan, 'mtd' | 'lastm'>,
): [Exclude<Timespan, 'mtd' | 'lastm'>, number, (value: Exclude<Timespan, 'mtd' | 'lastm'>) => void] {
    const [value, setValue] = useTimespanPreference(supported, fallback);
    return [value, TIMESPAN_HOURS[value], setValue];
}
