/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useColumnWidths
 *
 * The persistence is the boring half. What actually breaks tables in the field
 * is the *merge* between what an operator saved months ago and the column set
 * shipping today:
 *
 *   - a column that has since been removed must not leave a phantom width
 *     behind in the stored blob's shadow;
 *   - a column added after the last save must fall back to its default rather
 *     than to `undefined`, which renders as a zero-width column;
 *   - a corrupt or out-of-range entry must be discarded rather than producing
 *     a table nobody can drag back.
 *
 * `isCustomised` gates the "reset columns" affordance, so it has to be false
 * on a clean load and false again after a reset — a reset control that stays
 * lit forever is how operators learn to ignore it.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import {
    useColumnWidths,
    clampColumnWidth,
    COLUMN_MIN_WIDTH,
    COLUMN_MAX_WIDTH,
} from '../useColumnWidths';

const KEY = 'systems-table-widths';
type Col = 'name' | 'owner' | 'outcome';
const DEFAULTS: Record<Col, number> = { name: 240, owner: 160, outcome: 120 };

describe('clampColumnWidth', () => {
    it('holds a sane width unchanged', () => {
        expect(clampColumnWidth(240)).toBe(240);
    });

    it('floors at the minimum so a column cannot be dragged out of existence', () => {
        expect(clampColumnWidth(0)).toBe(COLUMN_MIN_WIDTH);
        expect(clampColumnWidth(-500)).toBe(COLUMN_MIN_WIDTH);
    });

    it('caps at the maximum so a runaway drag cannot break the layout', () => {
        expect(clampColumnWidth(100_000)).toBe(COLUMN_MAX_WIDTH);
    });

    it('rounds sub-pixel drag deltas to whole pixels', () => {
        expect(clampColumnWidth(240.6)).toBe(241);
        expect(clampColumnWidth(240.4)).toBe(240);
    });
});

describe('useColumnWidths', () => {
    beforeEach(() => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('starts at the defaults and reports no customisation', () => {
        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths).toEqual(DEFAULTS);
        expect(result.current.isCustomised).toBe(false);
    });

    it('hydrates saved widths over the defaults', () => {
        localStorage.setItem(KEY, JSON.stringify({ owner: 300 }));

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths).toEqual({ ...DEFAULTS, owner: 300 });
        expect(result.current.isCustomised).toBe(true);
    });

    it('drops stored keys for columns that no longer exist', () => {
        localStorage.setItem(KEY, JSON.stringify({ owner: 300, retiredColumn: 999 }));

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(Object.keys(result.current.widths).sort()).toEqual(['name', 'outcome', 'owner']);
        expect(result.current.widths).not.toHaveProperty('retiredColumn');
    });

    it('gives a newly added column its default rather than a zero width', () => {
        // Saved before `outcome` existed.
        localStorage.setItem(KEY, JSON.stringify({ name: 300, owner: 200 }));

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths.outcome).toBe(DEFAULTS.outcome);
    });

    it('clamps stored widths that are out of range', () => {
        localStorage.setItem(KEY, JSON.stringify({ name: 5, owner: 99_999 }));

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths.name).toBe(COLUMN_MIN_WIDTH);
        expect(result.current.widths.owner).toBe(COLUMN_MAX_WIDTH);
    });

    it.each([
        ['a string', '300'],
        ['null', null],
        ['NaN', Number.NaN],
        ['Infinity', Number.POSITIVE_INFINITY],
    ])('discards a %s entry and keeps the default', (_label, bad) => {
        localStorage.setItem(KEY, JSON.stringify({ owner: bad }));

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths.owner).toBe(DEFAULTS.owner);
    });

    it('falls back to the defaults when the stored blob is unparseable', () => {
        localStorage.setItem(KEY, 'not-json-at-all');

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(result.current.widths).toEqual(DEFAULTS);
        expect(console.error).toHaveBeenCalled();
    });

    it('clamps on setWidth and persists the result', () => {
        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        act(() => result.current.setWidth('name', 10));

        expect(result.current.widths.name).toBe(COLUMN_MIN_WIDTH);
        expect(JSON.parse(localStorage.getItem(KEY) as string)).toMatchObject({
            name: COLUMN_MIN_WIDTH,
        });
    });

    it('leaves sibling columns alone when one is resized', () => {
        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        act(() => result.current.setWidth('owner', 400));

        expect(result.current.widths).toEqual({ ...DEFAULTS, owner: 400 });
    });

    it('resetWidth returns one column to its default and leaves the rest custom', () => {
        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        act(() => result.current.setWidth('owner', 400));
        act(() => result.current.setWidth('name', 500));
        act(() => result.current.resetWidth('owner'));

        expect(result.current.widths.owner).toBe(DEFAULTS.owner);
        expect(result.current.widths.name).toBe(500);
        expect(result.current.isCustomised).toBe(true);
    });

    it('resetAll clears every override and unlights the reset affordance', () => {
        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        act(() => result.current.setWidth('owner', 400));
        act(() => result.current.setWidth('name', 500));
        expect(result.current.isCustomised).toBe(true);

        act(() => result.current.resetAll());

        expect(result.current.widths).toEqual(DEFAULTS);
        expect(result.current.isCustomised).toBe(false);
        expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(DEFAULTS);
    });

    it('a resize survives a remount through localStorage', () => {
        const first = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));
        act(() => first.result.current.setWidth('outcome', 320));
        first.unmount();

        const second = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(second.result.current.widths.outcome).toBe(320);
    });

    it('does not throw when persistence fails', () => {
        const setItem = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
            throw new Error('QuotaExceededError');
        });

        const { result } = renderHook(() => useColumnWidths<Col>(KEY, DEFAULTS));

        expect(() => act(() => result.current.setWidth('name', 300))).not.toThrow();
        expect(result.current.widths.name).toBe(300);
        expect(console.error).toHaveBeenCalled();

        setItem.mockRestore();
    });
});
