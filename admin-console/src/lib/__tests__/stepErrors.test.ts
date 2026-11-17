/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { statusCodeText, stepErrorSummary } from '../stepErrors';
import type { RunStatsResponse } from '../aiSystemTypes';

function stats(overrides: Partial<RunStatsResponse>): RunStatsResponse {
    return {
        period_hours: 24,
        cost: {} as RunStatsResponse['cost'],
        actions: {} as RunStatsResponse['actions'],
        tool_errors: { total_tool_calls: 529, total_tool_errors: 0, overall_error_rate: 0, by_tool: [] },
        terminal_states: { total_runs: 171, by_state: [] },
        fragmented_runs: 0,
        ...overrides,
    };
}

describe('stepErrorSummary', () => {
    it('reports model errors the tool signal cannot see', () => {
        const s = stepErrorSummary(stats({
            step_errors: {
                total_calls: 700,
                total_errors: 8,
                overall_error_rate: 8 / 700,
                by_kind: [
                    { kind: 'model', calls: 171, errors: 8, error_rate: 8 / 171, status_codes: { '502': 8 } },
                    { kind: 'tool', calls: 529, errors: 0, error_rate: 0, status_codes: {} },
                ],
            },
        }));
        expect(s.allKinds).toBe(true);
        expect(s.totalErrors).toBe(8);
        expect(s.rows[0]).toMatchObject({ kind: 'model', errors: 8 });
    });

    it('labels the numbers of an answer without step_errors as tool calls only', () => {
        const s = stepErrorSummary(stats({}));
        expect(s.allKinds).toBe(false);
        expect(s.rows).toEqual([
            { kind: 'tool', calls: 529, errors: 0, error_rate: 0, status_codes: {} },
        ]);
    });
});

describe('statusCodeText', () => {
    it('lists codes most frequent first and names a call that got no response', () => {
        expect(statusCodeText({ none: 1, '502': 8 })).toBe('502 × 8, no response × 1');
        expect(statusCodeText({})).toBe('—');
    });
});
