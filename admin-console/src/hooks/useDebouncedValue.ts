/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useEffect, useState } from 'react';

/**
 * Trailing-edge debounce for a value that drives a server query.
 *
 * The failure mode this exists for: a search box wired straight into a
 * `useCallback` dependency chain re-runs its fetch effect on every keystroke,
 * because React effects are not debounced by anything. Typing a 20-character
 * correlation ID into the proxy-log search is 20 full-text scans against the
 * GIN index, 19 of which are thrown away — and, without a sequence guard, one
 * of those 19 can land last and repaint the table with results for a prefix.
 *
 * Keep the raw value in state for the input (so typing stays responsive) and
 * put only the debounced value in the fetch's dependency array.
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
    const [debounced, setDebounced] = useState<T>(value);

    useEffect(() => {
        const timer = setTimeout(() => setDebounced(value), delayMs);
        return () => clearTimeout(timer);
    }, [value, delayMs]);

    return debounced;
}

export default useDebouncedValue;
