/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A read of a part of a screen, and whether the screen got it.
 *
 * An empty panel says "there is nothing". A read that failed must not say
 * that: the part says it could not be loaded, with what the other side
 * answered, and offers to ask again (`ReadFailureNotice` draws it). A read
 * that fails AFTER it was answered once keeps what it holds and says since
 * when: the page shows the last data marked as stale, not an emptied part.
 *
 * Two forms, one outcome (`ReadFailure`):
 *
 *   - `useRead` holds the data itself. It asks for a key, asks again on
 *     `reload()` and, with `pollMs`, on an interval. `useKeyedLoad` is this
 *     hook with the data hidden while the read is failed.
 *   - `useReadFailures` is for a loader that keeps its data in the state of
 *     its component: the loader says `loaded(part)` or
 *     `failed(part, what, error)`, and the page asks for the failure of a
 *     part by name.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { failureSentence } from '../lib/narrow';

/** A read the screen did not get. */
export interface ReadFailure {
    /** What failed, then what the other side said (`failureSentence`). */
    sentence: string;
    /** When the data still shown was loaded, in milliseconds; null where nothing is shown. */
    loadedAt: number | null;
}

/** The time of day `at` is, as `HH:MM` on the clock of the browser: what a stale mark says its data is of. */
export function loadedAtClock(at: number): string {
    const date = new Date(at);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

export interface Read<T> {
    /** The last answer for the current key; kept when a later read of the same key fails. Null before the first answer. */
    data: T | null;
    /** True when the last read for the current key failed. */
    failed: boolean;
    /** The failure in words, or ''. */
    sentence: string;
    /** The failure for `ReadFailureNotice`, or null. */
    failure: ReadFailure | null;
    /** What the failed read rejected with; undefined where it did not fail. */
    error: unknown;
    /** True until an answer or a failure for the current key and the last `reload()` has arrived. A poll is not a load. */
    loading: boolean;
    /** Ask again for the same key. */
    reload: () => void;
    /** Replace the held answer (after a save, say) without asking. */
    setData: (updater: (prev: T | null) => T | null) => void;
}

export interface ReadOptions {
    /** Ask again every so many milliseconds. A poll that fails keeps the data and marks it stale. */
    pollMs?: number;
}

interface Stored<T> {
    key: string;
    version: number;
    data: T | null;
    loadedAt: number | null;
    failed: boolean;
    error: unknown;
}

/**
 * `loader` must be referentially stable (wrap it in `useCallback`): it is a
 * dependency of the effect, so a loader that closes over new parameters
 * starts the read again.
 */
export function useRead<T>(key: string, what: string, loader: () => Promise<T>, options: ReadOptions = {}): Read<T> {
    const { pollMs } = options;
    const [version, setVersion] = useState(0);
    const [tick, setTick] = useState(0);
    const [stored, setStored] = useState<Stored<T>>({ key: '', version: -1, data: null, loadedAt: null, failed: false, error: undefined });

    useEffect(() => {
        let cancelled = false;
        loader()
            .then((data) => {
                if (!cancelled) setStored({ key, version, data, loadedAt: Date.now(), failed: false, error: undefined });
            })
            .catch((error: unknown) => {
                if (cancelled) return;
                setStored((prev) =>
                    prev.key === key
                        ? { ...prev, version, failed: true, error }
                        : { key, version, data: null, loadedAt: null, failed: true, error },
                );
            });
        return () => {
            cancelled = true;
        };
    }, [key, version, tick, loader]);

    useEffect(() => {
        if (!pollMs) return undefined;
        const timer = setInterval(() => setTick((t) => t + 1), pollMs);
        return () => clearInterval(timer);
    }, [pollMs]);

    const mine = stored.key === key;
    const failed = mine && stored.failed;
    const sentence = failed ? failureSentence(what, stored.error) : '';
    const loadedAt = mine ? stored.loadedAt : null;
    const failure = useMemo<ReadFailure | null>(() => (failed ? { sentence, loadedAt } : null), [failed, sentence, loadedAt]);
    const reload = useCallback(() => setVersion((v) => v + 1), []);
    const setData = useCallback((updater: (prev: T | null) => T | null) => setStored((prev) => ({ ...prev, data: updater(prev.data) })), []);

    return {
        data: mine ? stored.data : null,
        failed,
        sentence,
        failure,
        error: failed ? stored.error : undefined,
        loading: !(mine && stored.version === version),
        reload,
        setData,
    };
}

/**
 * The failure of a read whose loader keeps the sentence in its data (a part
 * of a keyed load that failed beside parts that were answered): what
 * `ReadFailureNotice` shows, or null for no sentence.
 */
export function failureSaying(sentence: string | undefined): ReadFailure | null {
    return sentence ? { sentence, loadedAt: null } : null;
}

/** What a loader says of the parts it reads. The same object on every render. */
export interface ReadMarks<Part extends string = string> {
    /** The part was answered: its failure, if it had one, is gone, and its data is of now. */
    loaded: (part: Part) => void;
    /** The read of the part failed. Where it was answered before, the failure carries that time. */
    failed: (part: Part, what: string, error: unknown) => void;
    /** The read of the part failed and the loader shows nothing of it any more (another key, an emptied list). */
    lost: (part: Part, what: string, error: unknown) => void;
    /** The part now shows something else (another group, another period): what was known of it is dropped. */
    forget: (part: Part) => void;
}

/**
 * The failures of the parts a component loads itself, by the name the
 * component gives each part: `[failureOf, marks]`.
 */
export function useReadFailures<Part extends string = string>(): [(part: Part) => ReadFailure | null, ReadMarks<Part>] {
    const [failures, setFailures] = useState<Partial<Record<string, ReadFailure>>>({});
    const loadedAt = useRef<Partial<Record<string, number>>>({});

    const marks = useMemo<ReadMarks<Part>>(() => {
        const clear = (part: Part): void =>
            setFailures((prev) => {
                if (!(part in prev)) return prev;
                const rest = { ...prev };
                delete rest[part];
                return rest;
            });
        // The same failure said again changes nothing: a read that fails on
        // every render does not render the page again by failing.
        const hold = (part: Part, failure: ReadFailure): void =>
            setFailures((prev) => {
                const held = prev[part];
                if (held && held.sentence === failure.sentence && held.loadedAt === failure.loadedAt) return prev;
                return { ...prev, [part]: failure };
            });
        return {
            loaded: (part) => {
                loadedAt.current[part] = Date.now();
                clear(part);
            },
            failed: (part, what, error) => {
                hold(part, { sentence: failureSentence(what, error), loadedAt: loadedAt.current[part] ?? null });
            },
            lost: (part, what, error) => {
                delete loadedAt.current[part];
                hold(part, { sentence: failureSentence(what, error), loadedAt: null });
            },
            forget: (part) => {
                delete loadedAt.current[part];
                clear(part);
            },
        };
    }, []);

    const failureOf = useCallback((part: Part): ReadFailure | null => failures[part] ?? null, [failures]);
    return [failureOf, marks];
}
