/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Signals → Errors: every failed call in the range's runs, by kind.
 *
 * Tool (MCP) errors alone would let a model-provider outage — every run
 * failing on its first model call, every tool call fine — read "Errors 0".
 * The control plane counts every kind (`step_errors`); one that does not
 * is shown its tool numbers, labelled as tool calls, never as "all errors".
 */

import type { RunStatsResponse, StepErrorKindRow } from './aiSystemTypes';

export interface StepErrorSummary {
    totalCalls: number;
    totalErrors: number;
    errorRate: number | null;
    rows: StepErrorKindRow[];
    /** False when the server only reported tool calls. */
    allKinds: boolean;
}

export function stepErrorSummary(stats: RunStatsResponse): StepErrorSummary {
    const steps = stats.step_errors;
    if (steps) {
        return {
            totalCalls: steps.total_calls,
            totalErrors: steps.total_errors,
            errorRate: steps.overall_error_rate ?? null,
            rows: steps.by_kind,
            allKinds: true,
        };
    }
    const tools = stats.tool_errors;
    return {
        totalCalls: tools.total_tool_calls,
        totalErrors: tools.total_tool_errors,
        errorRate: tools.overall_error_rate ?? null,
        rows: tools.total_tool_calls
            ? [{
                kind: 'tool',
                calls: tools.total_tool_calls,
                errors: tools.total_tool_errors,
                error_rate: tools.overall_error_rate ?? null,
                status_codes: {},
            }]
            : [],
        allKinds: false,
    };
}

/** "502 × 8, none × 1" — the status codes behind a kind's errors. */
export function statusCodeText(codes: Record<string, number>): string {
    const parts = Object.entries(codes)
        .sort((a, b) => b[1] - a[1])
        .map(([code, n]) => `${code === 'none' ? 'no response' : code} × ${n}`);
    return parts.length ? parts.join(', ') : '—';
}
