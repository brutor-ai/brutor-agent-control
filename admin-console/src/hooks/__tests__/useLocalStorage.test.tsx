/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useLocalStorage / useLocalStorageString
 *
 * These two look interchangeable and are not. The JSON variant round-trips
 * structured values and must survive a corrupt entry — a half-written object
 * left by a killed tab has to degrade to the default rather than throw on the
 * render that reads it. The string variant deliberately skips JSON so that a
 * plain value written by another surface (or by a human in devtools) is read
 * back verbatim rather than as a parse failure.
 *
 * The other load-bearing behaviour is that the *key* is a dependency: a table
 * that re-keys its preference (per-tenant storage keys, for instance) must
 * write to the new key and not keep clobbering the old one.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useLocalStorage, useLocalStorageString } from '../useLocalStorage';

describe('useLocalStorage', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('returns the default when nothing is stored', () => {
        const { result } = renderHook(() => useLocalStorage('layout', 'card'));

        expect(result.current[0]).toBe('card');
    });

    it('hydrates from an existing stored value instead of the default', () => {
        localStorage.setItem('layout', JSON.stringify('list'));

        const { result } = renderHook(() => useLocalStorage('layout', 'card'));

        expect(result.current[0]).toBe('list');
    });

    it('round-trips structured values, not just strings', () => {
        const { result } = renderHook(() =>
            useLocalStorage<{ sort: string; desc: boolean }>('table', { sort: 'name', desc: false })
        );

        act(() => result.current[1]({ sort: 'created', desc: true }));

        expect(result.current[0]).toEqual({ sort: 'created', desc: true });
        expect(JSON.parse(localStorage.getItem('table') as string)).toEqual({
            sort: 'created',
            desc: true,
        });
    });

    it('distinguishes a stored falsy value from an absent one', () => {
        localStorage.setItem('flag', JSON.stringify(false));

        const { result } = renderHook(() => useLocalStorage('flag', true));

        // `false` is a real stored preference — reading it as "nothing stored"
        // and falling back to `true` would silently re-enable a toggle the
        // operator turned off.
        expect(result.current[0]).toBe(false);
    });

    it('persists the initial default so the key exists after first mount', () => {
        renderHook(() => useLocalStorage('seeded', 42));

        expect(localStorage.getItem('seeded')).toBe('42');
    });

    it('supports updater functions like useState', () => {
        const { result } = renderHook(() => useLocalStorage('count', 1));

        act(() => result.current[1]((prev) => prev + 1));
        act(() => result.current[1]((prev) => prev + 1));

        expect(result.current[0]).toBe(3);
        expect(localStorage.getItem('count')).toBe('3');
    });

    it('falls back to the default when the stored entry is corrupt', () => {
        localStorage.setItem('layout', '{not json');

        const { result } = renderHook(() => useLocalStorage('layout', 'card'));

        expect(result.current[0]).toBe('card');
        expect(console.error).toHaveBeenCalled();
    });

    it('does not throw when the write fails (quota / private mode)', () => {
        const setItem = vi
            .spyOn(localStorage, 'setItem')
            .mockImplementation(() => {
                throw new Error('QuotaExceededError');
            });

        const { result } = renderHook(() => useLocalStorage('layout', 'card'));

        expect(() => act(() => result.current[1]('list'))).not.toThrow();
        // The in-memory value still advances — the UI stays usable even when
        // the preference cannot be persisted.
        expect(result.current[0]).toBe('list');
        expect(console.error).toHaveBeenCalled();

        setItem.mockRestore();
    });

    it('writes to the new key when the key changes', () => {
        const { result, rerender } = renderHook(
            ({ key }: { key: string }) => useLocalStorage(key, 'card'),
            { initialProps: { key: 'tenant-a' } }
        );

        act(() => result.current[1]('list'));
        expect(localStorage.getItem('tenant-a')).toBe('"list"');

        rerender({ key: 'tenant-b' });

        // The value carries over (useState is not re-initialised) but the new
        // key now owns it, and the old key is left untouched.
        expect(localStorage.getItem('tenant-b')).toBe('"list"');
        expect(localStorage.getItem('tenant-a')).toBe('"list"');
    });
});

describe('useLocalStorageString', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('returns the default when nothing is stored', () => {
        const { result } = renderHook(() => useLocalStorageString('view', 'card'));

        expect(result.current[0]).toBe('card');
    });

    it('reads a raw string back verbatim — no JSON quoting', () => {
        localStorage.setItem('view', 'list');

        const { result } = renderHook(() => useLocalStorageString('view', 'card'));

        expect(result.current[0]).toBe('list');
    });

    it('stores without JSON quoting', () => {
        const { result } = renderHook(() => useLocalStorageString('view', 'card'));

        act(() => result.current[1]('list'));

        expect(localStorage.getItem('view')).toBe('list');
    });

    it('treats a stored empty string as a real value', () => {
        localStorage.setItem('view', '');

        const { result } = renderHook(() => useLocalStorageString('view', 'card'));

        expect(result.current[0]).toBe('');
    });

    it('does not throw when the write fails', () => {
        const setItem = vi
            .spyOn(localStorage, 'setItem')
            .mockImplementation(() => {
                throw new Error('QuotaExceededError');
            });

        const { result } = renderHook(() => useLocalStorageString('view', 'card'));

        expect(() => act(() => result.current[1]('list'))).not.toThrow();
        expect(result.current[0]).toBe('list');

        setItem.mockRestore();
    });
});
