/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { useEffect } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';

import { useRead, useReadFailures } from '../useRead';

afterEach(() => {
    vi.useRealTimers();
});

describe('useRead', () => {
    it('holds the data of a read that was answered, and no failure', async () => {
        const { result } = renderHook(() => useRead('a', 'Failed to load the alerts', () => Promise.resolve(['one'])));
        expect(result.current.loading).toBe(true);
        await waitFor(() => expect(result.current.data).toEqual(['one']));
        expect(result.current.failed).toBe(false);
        expect(result.current.sentence).toBe('');
        expect(result.current.failure).toBeNull();
    });

    it('a first read that fails holds no data and the sentence with what the other side said', async () => {
        const loader = () => Promise.reject(new Error('The gateway is not reachable'));
        const { result } = renderHook(() => useRead('a', 'Failed to load the alerts', loader));
        await waitFor(() => expect(result.current.failed).toBe(true));
        expect(result.current.data).toBeNull();
        expect(result.current.sentence).toBe('Failed to load the alerts: The gateway is not reachable');
        expect(result.current.failure).toEqual({ sentence: result.current.sentence, loadedAt: null });
    });

    it('reload asks again and a later answer clears the failure', async () => {
        let attempt = 0;
        const loader = vi.fn(() => (attempt++ === 0 ? Promise.reject(new Error('no')) : Promise.resolve('ok')));
        const { result } = renderHook(() => useRead('a', 'Failed to load', loader));
        await waitFor(() => expect(result.current.failed).toBe(true));
        act(() => result.current.reload());
        await waitFor(() => expect(result.current.data).toBe('ok'));
        expect(result.current.failed).toBe(false);
        expect(result.current.failure).toBeNull();
    });

    it('a refresh that fails after an answer keeps the data and says when it was loaded', async () => {
        let attempt = 0;
        const loader = vi.fn(() => (attempt++ === 0 ? Promise.resolve(['one']) : Promise.reject(new Error('timeout'))));
        const before = Date.now();
        const { result } = renderHook(() => useRead('a', 'Failed to load the alerts', loader));
        await waitFor(() => expect(result.current.data).toEqual(['one']));
        act(() => result.current.reload());
        await waitFor(() => expect(result.current.failed).toBe(true));
        expect(result.current.data).toEqual(['one']);
        expect(result.current.sentence).toBe('Failed to load the alerts: timeout');
        expect(result.current.failure?.loadedAt).toBeGreaterThanOrEqual(before);
    });

    it('another key never shows the data of the key before it', async () => {
        let attempt = 0;
        const loader = () => (attempt++ === 0 ? Promise.resolve('A') : Promise.reject(new Error('no')));
        const { result, rerender } = renderHook(({ k }: { k: string }) => useRead(k, 'Failed to load', loader), {
            initialProps: { k: 'a' },
        });
        await waitFor(() => expect(result.current.data).toBe('A'));
        rerender({ k: 'b' });
        expect(result.current.data).toBeNull();
        await waitFor(() => expect(result.current.failed).toBe(true));
        expect(result.current.data).toBeNull();
        expect(result.current.failure?.loadedAt).toBeNull();
    });

    it('polls on its interval without going back to loading', async () => {
        vi.useFakeTimers();
        let n = 0;
        const loader = vi.fn(() => Promise.resolve(++n));
        const { result } = renderHook(() => useRead('a', 'Failed to load', loader, { pollMs: 1000 }));
        await act(async () => {
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(result.current.data).toBe(1);
        await act(async () => {
            await vi.advanceTimersByTimeAsync(1000);
        });
        expect(result.current.loading).toBe(false);
        expect(result.current.data).toBe(2);
        expect(loader).toHaveBeenCalledTimes(2);
    });
});

describe('useReadFailures', () => {
    it('has no failure for a part nothing was said about', () => {
        const { result } = renderHook(() => useReadFailures());
        expect(result.current[0]('alerts')).toBeNull();
    });

    it('a part that failed before it was ever loaded has the sentence and no time', () => {
        const { result } = renderHook(() => useReadFailures());
        act(() => result.current[1].failed('alerts', 'Failed to load the alerts', new Error('refused')));
        expect(result.current[0]('alerts')).toEqual({ sentence: 'Failed to load the alerts: refused', loadedAt: null });
        expect(result.current[0]('groups')).toBeNull();
    });

    it('a part that fails after it was loaded says when it was loaded; the next answer clears it', () => {
        const { result } = renderHook(() => useReadFailures());
        const before = Date.now();
        act(() => result.current[1].loaded('alerts'));
        expect(result.current[0]('alerts')).toBeNull();
        act(() => result.current[1].failed('alerts', 'Failed to load the alerts', new Error('refused')));
        expect(result.current[0]('alerts')?.loadedAt).toBeGreaterThanOrEqual(before);
        act(() => result.current[1].loaded('alerts'));
        expect(result.current[0]('alerts')).toBeNull();
    });

    it('lost is a failure of a part whose data the loader dropped: no time, whatever was loaded before', () => {
        const { result } = renderHook(() => useReadFailures());
        act(() => result.current[1].loaded('alerts'));
        act(() => result.current[1].lost('alerts', 'Failed to load the alerts', new Error('refused')));
        expect(result.current[0]('alerts')).toEqual({ sentence: 'Failed to load the alerts: refused', loadedAt: null });
    });

    it('the same failure said again commits nothing: a read that fails after every commit does not loop', () => {
        let commits = 0;
        const { result } = renderHook(() => {
            useEffect(() => {
                commits += 1;
            });
            return useReadFailures();
        });
        act(() => result.current[1].failed('alerts', 'Failed to load the alerts', new Error('refused')));
        const at = commits;
        const held = result.current[0]('alerts');
        act(() => result.current[1].failed('alerts', 'Failed to load the alerts', new Error('refused')));
        act(() => result.current[1].lost('alerts', 'Failed to load the alerts', new Error('refused')));
        expect(commits).toBe(at);
        expect(result.current[0]('alerts')).toBe(held);
    });

    it('forget drops what was known of a part: the next failure is a first one', () => {
        const { result } = renderHook(() => useReadFailures());
        act(() => result.current[1].loaded('alerts'));
        act(() => result.current[1].forget('alerts'));
        act(() => result.current[1].failed('alerts', 'Failed to load the alerts', new Error('refused')));
        expect(result.current[0]('alerts')?.loadedAt).toBeNull();
    });

    it('the marks are the same object on every render, and an answer with no failure to clear renders nothing', () => {
        let renders = 0;
        const { result } = renderHook(() => {
            renders += 1;
            return useReadFailures();
        });
        const marks = result.current[1];
        const at = renders;
        act(() => result.current[1].loaded('alerts'));
        expect(renders).toBe(at);
        act(() => result.current[1].failed('alerts', 'x', null));
        expect(result.current[1]).toBe(marks);
    });
});
