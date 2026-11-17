/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Display names for AI System ids that a payload carries bare — a drift
 * event's `caller_system_id`, a sealed contract's dependency targets (the
 * closure deliberately stores no names: a rename must not change a hash).
 *
 * Loaded once per mount and only when `enabled`, so a panel with nothing to
 * resolve makes no request. Where the list cannot be loaded the ids stay as
 * they are, and `failure` is what the screen says of it (`AISystemNamesNotice`):
 * an id in place of a name is read as the name unless the screen says the
 * names are missing.
 */

import { useCallback, useEffect, useState } from 'react';

import { useGateway } from './useGateway';
import { useReadFailures, type ReadFailure } from './useRead';

export interface AISystemNames {
    /** Display name by AI System id; empty until the list is answered. */
    names: Record<string, string>;
    /** Why the names could not be loaded, or null. */
    failure: ReadFailure | null;
    /** Ask for the list again. */
    retry: () => void;
}

/** What the screen says where the names stand in for ids. */
export const NAMES_NOT_LOADED = 'Failed to load the names of the AI Systems; ids are shown in their place';

export function useAISystemNames(enabled: boolean, what: string = NAMES_NOT_LOADED): AISystemNames {
    const { fetchAISystems } = useGateway();
    const [names, setNames] = useState<Record<string, string>>({});
    const [readFailure, reads] = useReadFailures<'names'>();
    const [asked, setAsked] = useState(0);

    useEffect(() => {
        if (!enabled) return;
        let cancelled = false;
        fetchAISystems({ limit: 500, include_inactive: true }).then(
            (res) => {
                if (cancelled) return;
                setNames(
                    Object.fromEntries(res.systems.map((s) => [s.id, s.display_name || s.name])),
                );
                reads.loaded('names');
            },
            (error: unknown) => {
                // The ids stay ids, and the screen says the names are missing.
                if (!cancelled) reads.failed('names', what, error);
            },
        );
        return () => {
            cancelled = true;
        };
    }, [enabled, fetchAISystems, reads, what, asked]);

    const retry = useCallback(() => setAsked((n) => n + 1), []);
    return { names, failure: readFailure('names'), retry };
}
