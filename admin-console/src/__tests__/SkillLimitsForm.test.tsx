/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * SkillLimitsForm — pure "Execution Limits" skill form.
 *
 * Pins the onChange contract for execution.{daily_limit, monthly_limit,
 * daily_warning_percent, monthly_warning_percent}:
 *   (1) Each field renders and round-trips under execution.<field>.
 *   (2) Editing one field preserves same-section siblings.
 *   (3) Clearing a field emits it as `undefined`.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import SkillLimitsForm from '../components/groups/subtabs/SkillLimitsForm';

const inputFor = (label: string): HTMLInputElement =>
    screen.getByText(label).parentElement!.querySelector('input')!;

// [field, label, typedValue]
const FIELDS: [string, string, number][] = [
    ['daily_limit', 'Daily Limit', 100],
    ['monthly_limit', 'Monthly Limit', 2000],
    ['daily_warning_percent', 'Daily Warning (%)', 80],
    ['monthly_warning_percent', 'Monthly Warning (%)', 90],
];

describe('SkillLimitsForm — structure', () => {
    it('renders every execution-limit field', () => {
        render(<SkillLimitsForm value={{}} onChange={vi.fn()} />);
        for (const [, label] of FIELDS) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
    });
});

describe('SkillLimitsForm — onChange nested shape (round-trip per field)', () => {
    it.each(FIELDS)('writes execution.%s', (field, label, value) => {
        const onChange = vi.fn();
        render(<SkillLimitsForm value={{}} onChange={onChange} />);
        fireEvent.change(inputFor(label), { target: { value: String(value) } });
        expect(onChange).toHaveBeenCalledWith({ execution: { [field]: value } });
    });
});

describe('SkillLimitsForm — merge & clear', () => {
    it('preserves same-section siblings when one field changes', () => {
        const onChange = vi.fn();
        render(
            <SkillLimitsForm
                value={{ execution: { daily_limit: 100 } }}
                onChange={onChange}
            />,
        );
        fireEvent.change(inputFor('Monthly Limit'), { target: { value: '2000' } });
        expect(onChange).toHaveBeenCalledWith({
            execution: { daily_limit: 100, monthly_limit: 2000 },
        });
    });

    it('emits a cleared field as undefined while keeping siblings', () => {
        const onChange = vi.fn();
        render(
            <SkillLimitsForm
                value={{ execution: { daily_limit: 100, monthly_limit: 2000 } }}
                onChange={onChange}
            />,
        );
        fireEvent.change(inputFor('Daily Limit'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0] as { execution: Record<string, unknown> };
        expect(next.execution.daily_limit).toBeUndefined();
        expect(next.execution.monthly_limit).toBe(2000);
    });
});
