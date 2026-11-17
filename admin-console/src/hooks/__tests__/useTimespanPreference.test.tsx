/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The shared analytics timespan.
 *
 * What is worth defending here is the *sharing*, not the storage — a
 * per-component `useLocalStorageString` would pass a naive "it persists"
 * test while still leaving two mounted surfaces disagreeing, which is
 * exactly the bug this hook exists to prevent. So the tests below drive
 * two consumers at once.
 *
 * The other load-bearing behaviour is that a surface which cannot render
 * the stored window must *degrade* to its own fallback without
 * overwriting the stored value. Mission Control offers MTD; AI Systems
 * does not. Glancing at AI Systems must not silently destroy the
 * operator's MTD selection.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, act, cleanup } from '@testing-library/react';

import {
    useTimespanPreference,
    useTimespanHoursPreference,
    TIMESPAN_STORAGE_KEY,
    TIMESPAN_HOURS,
    type Timespan,
} from '../useTimespanPreference';

const ROLLING = ['24h', '7d', '30d', '90d', 'all'] as const;
const WITH_CALENDAR = ['24h', '7d', '30d', '90d', 'mtd', 'lastm', 'all'] as const;

/** Stands in for a surface that offers only rolling windows. */
function RollingSurface({ testId, fallback = '30d' }: { testId: string; fallback?: Timespan }) {
    const [value, setValue] = useTimespanPreference(
        ROLLING,
        fallback as (typeof ROLLING)[number],
    );
    return (
        <div>
            <span data-testid={testId}>{value}</span>
            <button data-testid={`${testId}-set-90d`} onClick={() => setValue('90d')}>
                90d
            </button>
        </div>
    );
}

/** Stands in for Mission Control, which also offers calendar periods. */
function CalendarSurface({ testId }: { testId: string }) {
    const [value, setValue] = useTimespanPreference(WITH_CALENDAR, '24h');
    return (
        <div>
            <span data-testid={testId}>{value}</span>
            <button data-testid={`${testId}-set-mtd`} onClick={() => setValue('mtd')}>
                mtd
            </button>
        </div>
    );
}

/** Stands in for AI Systems, whose API takes period_hours. */
function HoursSurface({ testId }: { testId: string }) {
    const [value, hours] = useTimespanHoursPreference(ROLLING, '24h');
    return (
        <span data-testid={testId}>
            {value}:{hours}
        </span>
    );
}

describe('useTimespanPreference', () => {
    beforeEach(() => {
        localStorage.clear();
    });
    afterEach(cleanup);

    it('falls back per surface when nothing has been chosen yet', () => {
        render(
            <>
                <RollingSurface testId="groups" fallback="30d" />
                <CalendarSurface testId="mc" />
            </>,
        );
        // Each surface keeps its own default until something is picked.
        expect(screen.getByTestId('groups').textContent).toBe('30d');
        expect(screen.getByTestId('mc').textContent).toBe('24h');
    });

    it('propagates a change to every mounted surface immediately', () => {
        render(
            <>
                <RollingSurface testId="a" />
                <RollingSurface testId="b" />
                <CalendarSurface testId="c" />
            </>,
        );

        act(() => {
            screen.getByTestId('a-set-90d').click();
        });

        expect(screen.getByTestId('a').textContent).toBe('90d');
        expect(screen.getByTestId('b').textContent).toBe('90d');
        expect(screen.getByTestId('c').textContent).toBe('90d');
    });

    it('persists the choice so it survives a remount', () => {
        const first = render(<RollingSurface testId="a" />);
        act(() => {
            screen.getByTestId('a-set-90d').click();
        });
        expect(localStorage.getItem(TIMESPAN_STORAGE_KEY)).toBe('90d');
        first.unmount();

        render(<RollingSurface testId="b" />);
        expect(screen.getByTestId('b').textContent).toBe('90d');
    });

    it('shows a fallback for an unsupported window WITHOUT destroying it', () => {
        render(
            <>
                <CalendarSurface testId="mc" />
                <RollingSurface testId="runs" fallback="24h" />
            </>,
        );

        act(() => {
            screen.getByTestId('mc-set-mtd').click();
        });

        // Mission Control shows MTD; the rolling-only surface degrades...
        expect(screen.getByTestId('mc').textContent).toBe('mtd');
        expect(screen.getByTestId('runs').textContent).toBe('24h');
        // ...but the stored preference is untouched, so coming back to
        // Mission Control still shows MTD.
        expect(localStorage.getItem(TIMESPAN_STORAGE_KEY)).toBe('mtd');
    });

    it("keeps Mission Control's custom calendar range via alsoAccept", () => {
        localStorage.setItem(TIMESPAN_STORAGE_KEY, 'custom:2026-01-01:2026-01-31');

        function Custom() {
            const [value] = useTimespanPreference(WITH_CALENDAR, '24h', (raw) =>
                raw.startsWith('custom:'),
            );
            return <span data-testid="custom">{value}</span>;
        }

        render(
            <>
                <Custom />
                <RollingSurface testId="runs" fallback="24h" />
            </>,
        );

        expect(screen.getByTestId('custom').textContent).toBe('custom:2026-01-01:2026-01-31');
        // A surface that can't render a custom range degrades, as above.
        expect(screen.getByTestId('runs').textContent).toBe('24h');
        expect(localStorage.getItem(TIMESPAN_STORAGE_KEY)).toBe('custom:2026-01-01:2026-01-31');
    });

    it('maps windows to the period_hours the run APIs expect', () => {
        localStorage.setItem(TIMESPAN_STORAGE_KEY, '90d');
        render(<HoursSurface testId="hours" />);
        expect(screen.getByTestId('hours').textContent).toBe('90d:2160');

        expect(TIMESPAN_HOURS['24h']).toBe(24);
        expect(TIMESPAN_HOURS['7d']).toBe(168);
        expect(TIMESPAN_HOURS['30d']).toBe(720);
        expect(TIMESPAN_HOURS['90d']).toBe(2160);
        // 0 is what the metrics endpoints read as "no lower bound".
        expect(TIMESPAN_HOURS.all).toBe(0);
    });

    it('ignores a garbage stored value rather than rendering it', () => {
        localStorage.setItem(TIMESPAN_STORAGE_KEY, 'not-a-window');
        render(<RollingSurface testId="a" fallback="30d" />);
        expect(screen.getByTestId('a').textContent).toBe('30d');
    });

    it('reads the shared key and no other: loading the module copies nothing into it', async () => {
        localStorage.clear();
        localStorage.setItem('mission-control-period', '90d');
        vi.resetModules();
        await import('../useTimespanPreference');
        expect(localStorage.getItem(TIMESPAN_STORAGE_KEY)).toBeNull();
        expect(localStorage.getItem('mission-control-period')).toBe('90d');
    });
});
