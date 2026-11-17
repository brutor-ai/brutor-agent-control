/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Converts a TimeRange into request-body fields. Kept apart from
// TimeRangePicker.tsx so that file exports only its component — a file mixing
// components and helpers opts out of Fast Refresh.
import type { TimeRange } from './TimeRangePicker';

export function timeRangeToBody(range: TimeRange): {
    hours_ago?: number;
    start_time?: string;
    end_time?: string;
} {
    if (range.kind === 'preset') {
        // Preserve preset semantics: for sub-hour ranges we send
        // explicit start/end because the backend's hours_ago is
        // floored to 1.
        if (range.minutes < 60) {
            const end = new Date();
            const start = new Date(end.getTime() - range.minutes * 60 * 1000);
            return { start_time: start.toISOString(), end_time: end.toISOString() };
        }
        return { hours_ago: Math.round(range.minutes / 60) };
    }
    return { start_time: range.start, end_time: range.end };
}
