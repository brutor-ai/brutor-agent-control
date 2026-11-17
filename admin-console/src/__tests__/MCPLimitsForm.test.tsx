/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCPLimitsForm — pure presentational MCP server-limits form.
 *
 * Pins the onChange contract for the runtime enforcement layer:
 *   (1) Every field in every section (execution / data / frequency)
 *       renders and, when edited, emits the correct nested-JSON shape.
 *   (2) Editing one field preserves the other sections and same-section
 *       siblings (object-spread merge).
 *   (3) Clearing a field emits it as `undefined` (the form keeps the
 *       section key; it does not delete it) while siblings survive.
 *
 * NOTE ON CLEAR BEHAVIOR: unlike LLMLimitsForm (which deletes keys),
 * MCPLimitsForm's `updateField` sets the field to `undefined` and never
 * prunes the section — these tests assert that ACTUAL behavior.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MCPLimitsForm from '../components/groups/subtabs/MCPLimitsForm';

/** Find the number input paired with a given field label. */
const inputFor = (label: string): HTMLInputElement => {
    const lbl = screen.getByText(label);
    return lbl.parentElement!.querySelector('input')!;
};

// [section, field, label, typedValue]
const FIELDS: [string, string, string, number][] = [
    ['execution', 'timeout_seconds', 'Timeout (seconds)', 30],
    ['execution', 'max_concurrency_per_user', 'Max Concurrency (per user)', 4],
    ['execution', 'max_concurrency_per_group', 'Max Concurrency (per group)', 16],
    ['data', 'max_payload_size_mb', 'Max Payload Size (MB)', 2.5],
    ['frequency', 'max_calls_per_minute', 'Max Calls (per minute)', 60],
    ['frequency', 'max_calls_per_hour', 'Max Calls (per hour)', 1000],
    ['frequency', 'max_calls_per_day', 'Max Calls (per day)', 5000],
    ['frequency', 'burst_limit', 'Burst Limit', 10],
    ['frequency', 'cooldown_seconds', 'Cooldown (seconds)', 30],
];

describe('MCPLimitsForm — structure', () => {
    it('renders every field label', () => {
        render(<MCPLimitsForm value={{}} onChange={vi.fn()} />);
        for (const [, , label] of FIELDS) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
    });
});

describe('MCPLimitsForm — onChange nested-JSON shape (round-trip per field)', () => {
    it.each(FIELDS)(
        'writes %s.%s under the correct nested key',
        (section, field, label, value) => {
            const onChange = vi.fn();
            render(<MCPLimitsForm value={{}} onChange={onChange} />);
            fireEvent.change(inputFor(label), { target: { value: String(value) } });
            expect(onChange).toHaveBeenCalledWith({ [section]: { [field]: value } });
        },
    );
});

describe('MCPLimitsForm — merge & clear semantics', () => {
    it('preserves other sections and same-section siblings when one field changes', () => {
        const onChange = vi.fn();
        const initial = {
            execution: { timeout_seconds: 5 },
            data: { max_payload_size_mb: 10 },
        };
        render(<MCPLimitsForm value={initial} onChange={onChange} />);
        fireEvent.change(inputFor('Max Concurrency (per user)'), { target: { value: '3' } });
        expect(onChange).toHaveBeenCalledWith({
            execution: { timeout_seconds: 5, max_concurrency_per_user: 3 },
            data: { max_payload_size_mb: 10 },
        });
    });

    it('emits a cleared field as undefined while keeping same-section siblings', () => {
        const onChange = vi.fn();
        const initial = { execution: { timeout_seconds: 5, max_concurrency_per_user: 2 } };
        render(<MCPLimitsForm value={initial} onChange={onChange} />);
        fireEvent.change(inputFor('Timeout (seconds)'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0] as { execution: Record<string, unknown> };
        expect(next.execution.timeout_seconds).toBeUndefined();
        expect(next.execution.max_concurrency_per_user).toBe(2);
    });
});
