/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Load a resource for a key, and know whether what you hold is for THAT key.
 *
 * The usual `useEffect(() => { void load(); }, [load])` with a `load` that
 * opens on `setLoading(true)` is the render-loop smell the React Compiler
 * lint (`react-hooks/set-state-in-effect`) flags: the effect sets state
 * synchronously, which re-renders, which — with an unstable dependency —
 * re-runs the effect. This hook keeps the effect pure: it only starts the
 * request and stores the result when it arrives. "Loading" is DERIVED —
 * the stored result is stamped with the key and version it was loaded for,
 * and anything else is a load in flight. A `reload()` bumps the version.
 *
 * `loader` must be referentially stable (wrap it in `useCallback`); it is a
 * dependency of the effect by design, so a loader that closes over new
 * parameters restarts the load.
 */

import { useRead } from './useRead';
import { errorMessage } from '../lib/narrow';

export interface KeyedLoad<T> {
    /** The result for the current key, or null while it loads or after a failure. */
    data: T | null;
    /** The failure for the current key, or '' . */
    error: string;
    /** True until a result (or failure) for the current key and version has arrived. */
    loading: boolean;
    /** Run the loader again for the same key. */
    reload: () => void;
    /** Replace the held result locally (after a save, say) without reloading. */
    setData: (updater: (prev: T | null) => T | null) => void;
}

export function useKeyedLoad<T>(
    key: string,
    loader: () => Promise<T>,
    fallback = 'Failed to load',
): KeyedLoad<T> {
    const read = useRead(key, fallback, loader);
    return {
        data: read.failed ? null : read.data,
        error: read.failed ? errorMessage(read.error, fallback) : '',
        loading: read.loading,
        reload: read.reload,
        setData: read.setData,
    };
}
