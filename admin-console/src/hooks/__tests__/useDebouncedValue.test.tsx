/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The debounce the log viewers and the LLM catalog search rely on.
 *
 * What this guards is not the timer arithmetic but the contract the callers
 * assume: the value they put in a fetch dependency array must NOT change once
 * per keystroke. A `useEffect` / `useCallback` closure does not debounce
 * state changes on its own: without this hook, typing a 20-character
 * correlation ID is 20 full-text scans against the GIN index.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useDebouncedValue } from '../useDebouncedValue';

describe('useDebouncedValue', () => {
    beforeEach(() => {
        // Fake ONLY the timeout primitives the hook uses. A blanket
        // `useFakeTimers()` also stubs the microtask scheduler, which stalls
        // React's own scheduling and hangs teardown — the same trap
        // useHealthCheckBase.test.ts documents.
        vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    });

    afterEach(() => {
        vi.useRealTimers();
        cleanup();
    });

    it('returns the initial value straight away', () => {
        const { result } = renderHook(() => useDebouncedValue('a', 300));
        expect(result.current).toBe('a');
    });

    it('does not settle until the delay has elapsed', () => {
        const { result, rerender } = renderHook(
            ({ v }) => useDebouncedValue(v, 300),
            { initialProps: { v: 'a' } },
        );

        rerender({ v: 'ab' });
        act(() => { vi.advanceTimersByTime(299); });
        expect(result.current).toBe('a');

        act(() => { vi.advanceTimersByTime(1); });
        expect(result.current).toBe('ab');
    });

    it('emits once for a burst of keystrokes, not once per keystroke', () => {
        const seen: string[] = [];
        const { rerender } = renderHook(
            ({ v }) => { seen.push(useDebouncedValue(v, 300)); },
            { initialProps: { v: '' } },
        );

        // Someone typing a correlation ID at ~50ms/char.
        for (const v of ['c', 'co', 'cor', 'corr', 'corr-1']) {
            rerender({ v });
            act(() => { vi.advanceTimersByTime(50); });
        }
        // Nothing has settled yet — every render still reports the old value.
        expect(new Set(seen)).toEqual(new Set(['']));

        act(() => { vi.advanceTimersByTime(300); });
        rerender({ v: 'corr-1' });
        expect(seen[seen.length - 1]).toBe('corr-1');
        // The whole burst produced exactly one settled value beyond the initial.
        expect(new Set(seen)).toEqual(new Set(['', 'corr-1']));
    });

    it('drops an intermediate value when typing resumes before it settles', () => {
        const { result, rerender } = renderHook(
            ({ v }) => useDebouncedValue(v, 300),
            { initialProps: { v: 'a' } },
        );

        rerender({ v: 'ab' });
        act(() => { vi.advanceTimersByTime(200); });
        rerender({ v: 'abc' });
        act(() => { vi.advanceTimersByTime(200); });

        // 400ms total, but the timer restarted — 'ab' must never be observed.
        expect(result.current).toBe('a');

        act(() => { vi.advanceTimersByTime(100); });
        expect(result.current).toBe('abc');
    });

    it('does not emit after unmount', () => {
        const { rerender, unmount, result } = renderHook(
            ({ v }) => useDebouncedValue(v, 300),
            { initialProps: { v: 'a' } },
        );

        rerender({ v: 'ab' });
        unmount();
        act(() => { vi.advanceTimersByTime(1000); });

        expect(result.current).toBe('a');
    });
});
