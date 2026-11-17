/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

import { useKeyedLoad } from '../useKeyedLoad';

function deferred<T>() {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

describe('useKeyedLoad', () => {
    it('is loading until the result for the current key arrives', async () => {
        const d = deferred<string>();
        const loader = vi.fn(() => d.promise);
        const { result } = renderHook(() => useKeyedLoad('a', loader));
        expect(result.current.loading).toBe(true);
        expect(result.current.data).toBeNull();
        d.resolve('A');
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.data).toBe('A');
        expect(result.current.error).toBe('');
        expect(loader).toHaveBeenCalledTimes(1);
    });

    it('a key change is a new load and never shows the old key\'s data', async () => {
        const answers: Record<string, ReturnType<typeof deferred<string>>> = { a: deferred(), b: deferred() };
        const loader = vi.fn((key: string) => answers[key].promise);
        const { result, rerender } = renderHook(
            ({ key, load }: { key: string; load: () => Promise<string> }) => useKeyedLoad(key, load),
            { initialProps: { key: 'a', load: () => loader('a') } },
        );
        answers.a.resolve('A');
        await waitFor(() => expect(result.current.data).toBe('A'));

        const loadB = () => loader('b');
        rerender({ key: 'b', load: loadB });
        expect(result.current.loading).toBe(true);
        expect(result.current.data).toBeNull();
        answers.b.resolve('B');
        await waitFor(() => expect(result.current.data).toBe('B'));
    });

    it('reports a failure through errorMessage and clears it on reload', async () => {
        let attempt = 0;
        const loader = vi.fn(() => (attempt++ === 0 ? Promise.reject(new Error('boom')) : Promise.resolve('ok')));
        const { result } = renderHook(() => useKeyedLoad('a', loader, 'fallback'));
        await waitFor(() => expect(result.current.error).toBe('boom'));
        expect(result.current.loading).toBe(false);
        act(() => result.current.reload());
        expect(result.current.loading).toBe(true);
        await waitFor(() => expect(result.current.data).toBe('ok'));
        expect(result.current.error).toBe('');
        expect(loader).toHaveBeenCalledTimes(2);
    });

    it('setData replaces the held result without a load', async () => {
        const loader = vi.fn(() => Promise.resolve({ n: 1 }));
        const { result } = renderHook(() => useKeyedLoad('a', loader));
        await waitFor(() => expect(result.current.data).toEqual({ n: 1 }));
        act(() => result.current.setData((prev) => ({ n: (prev?.n ?? 0) + 1 })));
        expect(result.current.data).toEqual({ n: 2 });
        expect(loader).toHaveBeenCalledTimes(1);
    });

    it('ignores a result that arrives after the key moved on', async () => {
        const slow = deferred<string>();
        const fast = deferred<string>();
        const loaders: Record<string, () => Promise<string>> = { a: () => slow.promise, b: () => fast.promise };
        const { result, rerender } = renderHook(
            ({ key }: { key: string }) => useKeyedLoad(key, loaders[key]),
            { initialProps: { key: 'a' } },
        );
        rerender({ key: 'b' });
        fast.resolve('B');
        await waitFor(() => expect(result.current.data).toBe('B'));
        slow.resolve('A');
        await new Promise((r) => setTimeout(r, 0));
        expect(result.current.data).toBe('B');
    });
});
