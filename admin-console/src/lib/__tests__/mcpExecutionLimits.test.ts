/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The gateway reads a stated MCP execution limit as a positive number (a
 * whole one for a concurrency allowance) and refuses every call of the group
 * to the server when it cannot: the console says so before a save, instead
 * of sending a value that stops all traffic.
 */

import { describe, expect, it } from 'vitest';

import { mcpExecutionLimitsProblem } from '../mcpExecutionLimits';

describe('mcpExecutionLimitsProblem', () => {
    it('takes no limits, unset limits and positive values', () => {
        expect(mcpExecutionLimitsProblem({})).toBeNull();
        expect(mcpExecutionLimitsProblem(null)).toBeNull();
        expect(
            mcpExecutionLimitsProblem({
                execution: { timeout_seconds: 0.5, max_concurrency_per_user: undefined, max_concurrency_per_group: 3 },
                data: { max_payload_size_mb: 0.25 },
                frequency: { cooldown_seconds: 0 },
            }),
        ).toBeNull();
    });

    it.each([
        [{ execution: { timeout_seconds: 0 } }, 'Timeout (seconds) must be a positive number.'],
        [{ execution: { timeout_seconds: -5 } }, 'Timeout (seconds) must be a positive number.'],
        [{ data: { max_payload_size_mb: 0 } }, 'Max Payload Size (MB) must be a positive number.'],
        [{ execution: { max_concurrency_per_user: 0 } }, 'Max Concurrency (per user) must be a whole number of at least 1.'],
        [{ execution: { max_concurrency_per_user: 2.5 } }, 'Max Concurrency (per user) must be a whole number of at least 1.'],
        [{ execution: { max_concurrency_per_group: -1 } }, 'Max Concurrency (per group) must be a whole number of at least 1.'],
        [{ execution: { max_concurrency_per_group: 1.5 } }, 'Max Concurrency (per group) must be a whole number of at least 1.'],
        [{ execution: { timeout_seconds: Number.NaN } }, 'Timeout (seconds) must be a positive number.'],
    ])('refuses %j', (limits, sentence) => {
        expect(mcpExecutionLimitsProblem(limits)).toBe(
            `${sentence} The gateway refuses every call of the group to the server while a stated limit is not one.`,
        );
    });
});
