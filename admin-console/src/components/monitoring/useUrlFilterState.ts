/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * useUrlFilterSync — keep filter state in sync with the URL.
 *
 * Two flavours:
 *
 *   useReadFiltersOnMount(prefix, decoders)
 *       Read the URL once, return a `{key: parsed}` map for the
 *       caller to apply via its existing setters. Used for the
 *       initial hydration; never re-runs.
 *
 *   useWriteFiltersToUrl(prefix, snapshot, encoders)
 *       Watch a snapshot of filter state and call replaceState
 *       whenever it changes. Each encoder maps a state value back
 *       to a URL string (or null to omit).
 *
 * Why two hooks instead of one bidirectional thing: keeping reads
 * and writes separate stops the obvious render loop (URL → state
 * → URL → state). The caller owns the state, the hooks own the
 * URL bridge. Datadog uses the same pattern; Sentry rolled their
 * own custom-state-machine version of this and it bit them on
 * the back-button.
 */

import { useEffect, useRef } from 'react';
import type { TimeRange } from './TimeRangePicker';

// ---------------------------------------------------------------------------
// Codecs — one per state-type the viewers care about.
// ---------------------------------------------------------------------------

export const encodeStringArray = (v: string[]): string | null =>
    v.length === 0 ? null : v.join(',');
export const decodeStringArray = (raw: string | null): string[] =>
    raw ? raw.split(',').filter(Boolean) : [];

export const encodeString = (v: string): string | null => (v ? v : null);
export const decodeString = (raw: string | null): string => raw ?? '';

/**
 * TimeRange codec. Preset minutes encode as the number, custom
 * ranges as `c|<start>|<end>`. The leading marker keeps both
 * cases distinguishable on round-trip without a second key.
 */
export const encodeTimeRange =
    (defaultMinutes: number) =>
    (v: TimeRange): string | null => {
        if (v.kind === 'preset') {
            return v.minutes === defaultMinutes ? null : String(v.minutes);
        }
        if (!v.start || !v.end) return null;
        return `c|${v.start}|${v.end}`;
    };

export const decodeTimeRange =
    (defaultMinutes: number) =>
    (raw: string | null): TimeRange => {
        if (!raw) return { kind: 'preset', minutes: defaultMinutes };
        if (raw.startsWith('c|')) {
            const [, start, end] = raw.split('|');
            if (start && end) return { kind: 'custom', start, end };
            return { kind: 'preset', minutes: defaultMinutes };
        }
        const n = Number(raw);
        if (Number.isFinite(n) && n > 0) return { kind: 'preset', minutes: n };
        return { kind: 'preset', minutes: defaultMinutes };
    };

// ---------------------------------------------------------------------------
// Read-on-mount: one snapshot, never re-runs.
// ---------------------------------------------------------------------------

/**
 * Read URL params under the given prefix and run each decoder.
 * Returns `null` if there's nothing under the prefix at all — the
 * caller then knows to use its built-in defaults without applying
 * any setters. (Distinguishes "no URL state to hydrate from" from
 * "URL state present but every value is the default".)
 */
export function readFiltersFromUrl<S extends Record<string, any>>(
    prefix: string,
    decoders: { [K in keyof S]: (raw: string | null) => S[K] },
): S | null {
    const params = new URLSearchParams(window.location.search);
    let any = false;
    for (const key of Object.keys(decoders)) {
        if (params.has(`${prefix}${key}`)) { any = true; break; }
    }
    if (!any) return null;
    const out: any = {};
    for (const key of Object.keys(decoders) as Array<keyof S>) {
        out[key] = decoders[key](params.get(`${prefix}${String(key)}`));
    }
    return out as S;
}

// ---------------------------------------------------------------------------
// Sync-on-change: writes URL whenever the snapshot changes.
// ---------------------------------------------------------------------------

/**
 * Watch a snapshot of filter state. On any change, rewrite the URL
 * with replaceState (no history spam). On unmount, restore the URL
 * to whatever the parent had — important for view-switching pages.
 *
 * The snapshot must be referentially stable across renders ONLY
 * when the values haven't changed — i.e. callers should NOT
 * inline `{ a, b, c }` in render and pass it. Either useMemo
 * the snapshot or rely on the deps array (each value separately).
 */
export function useUrlFilterSync<S extends Record<string, any>>(
    prefix: string,
    snapshot: S,
    encoders: { [K in keyof S]: (value: S[K]) => string | null },
): void {
    // Skip the very first render's write — the caller may have just
    // hydrated from the URL and writing back is a no-op that costs a
    // history rewrite on every page mount.
    const firstRender = useRef(true);

    useEffect(() => {
        if (firstRender.current) {
            firstRender.current = false;
            return;
        }
        const params = new URLSearchParams(window.location.search);
        for (const key of Object.keys(encoders) as Array<keyof S>) {
            const k = `${prefix}${String(key)}`;
            const ser = encoders[key](snapshot[key]);
            if (ser === null) params.delete(k);
            else params.set(k, ser);
        }
        const qs = params.toString();
        const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
        window.history.replaceState(window.history.state, '', url);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [prefix, ...Object.values(snapshot)]);
}
