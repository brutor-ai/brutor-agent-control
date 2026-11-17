/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * SkillGovernanceForm — pure "Skill Execution Settings" form.
 *
 * Pins the flat onChange contract for:
 *   • execution_mode (select: unrestricted / approval_required / disabled)
 *   • discovery_enabled (checkbox)
 *   • approval_timeout_seconds (number, int)
 *   • max_concurrent_executions (number, int)
 * Plus merge (edits preserve other fields) and clear-to-undefined.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import SkillGovernanceForm from '../components/groups/subtabs/SkillGovernanceForm';

const inputFor = (label: string): HTMLInputElement =>
    screen.getByText(label).parentElement!.querySelector('input')!;

describe('SkillGovernanceForm — execution_mode select', () => {
    it('exposes unrestricted / approval_required / disabled', () => {
        render(<SkillGovernanceForm value={{}} onChange={vi.fn()} />);
        const opts = Array.from(
            screen.getByRole('combobox').querySelectorAll('option'),
        ).map((o) => (o).value);
        expect(opts).toEqual(['', 'unrestricted', 'approval_required', 'disabled']);
    });

    it('round-trips the chosen mode', () => {
        const onChange = vi.fn();
        render(<SkillGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(screen.getByRole('combobox'), {
            target: { value: 'approval_required' },
        });
        expect(onChange).toHaveBeenCalledWith({ execution_mode: 'approval_required' });
    });

    it('clears to undefined', () => {
        const onChange = vi.fn();
        render(
            <SkillGovernanceForm
                value={{ execution_mode: 'disabled' }}
                onChange={onChange}
            />,
        );
        fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ execution_mode: undefined });
    });
});

describe('SkillGovernanceForm — discovery_enabled checkbox', () => {
    it('emits true when checked from unset', () => {
        const onChange = vi.fn();
        render(<SkillGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.click(screen.getByRole('checkbox'));
        expect(onChange).toHaveBeenCalledWith({ discovery_enabled: true });
    });

    it('emits false when unchecked', () => {
        const onChange = vi.fn();
        render(
            <SkillGovernanceForm
                value={{ discovery_enabled: true }}
                onChange={onChange}
            />,
        );
        fireEvent.click(screen.getByRole('checkbox'));
        expect(onChange).toHaveBeenCalledWith({ discovery_enabled: false });
    });
});

describe('SkillGovernanceForm — number fields (round-trip per field)', () => {
    const NUMBER_FIELDS: [string, string, number][] = [
        ['approval_timeout_seconds', 'Approval Timeout (seconds)', 300],
        ['max_concurrent_executions', 'Max Concurrent Executions', 5],
    ];

    it.each(NUMBER_FIELDS)('%s round-trips as an int', (field, label, value) => {
        const onChange = vi.fn();
        render(<SkillGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(inputFor(label), { target: { value: String(value) } });
        expect(onChange).toHaveBeenCalledWith({ [field]: value });
    });

    it.each(NUMBER_FIELDS)('%s clears to undefined', (field, label) => {
        const onChange = vi.fn();
        render(
            <SkillGovernanceForm value={{ [field]: 42 }} onChange={onChange} />,
        );
        fireEvent.change(inputFor(label), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ [field]: undefined });
    });
});

describe('SkillGovernanceForm — merge semantics', () => {
    it('preserves other fields when one changes', () => {
        const onChange = vi.fn();
        render(
            <SkillGovernanceForm
                value={{ execution_mode: 'disabled' }}
                onChange={onChange}
            />,
        );
        fireEvent.change(inputFor('Max Concurrent Executions'), {
            target: { value: '3' },
        });
        expect(onChange).toHaveBeenCalledWith({
            execution_mode: 'disabled',
            max_concurrent_executions: 3,
        });
    });
});

describe('SkillGovernanceForm — approval timeout bounds', () => {
    const RANGE = 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).';

    it('bounds the field to 1 .. 604800 seconds', () => {
        render(<SkillGovernanceForm value={{}} onChange={vi.fn()} />);
        const input = inputFor('Approval Timeout (seconds)');
        expect(input.min).toBe('1');
        expect(input.max).toBe('604800');
        expect(screen.queryByText(RANGE)).toBeNull();
    });

    it('says the range beside a value outside it', () => {
        const { rerender } = render(<SkillGovernanceForm value={{ approval_timeout_seconds: 0 }} onChange={vi.fn()} />);
        expect(screen.getByRole('alert')).toHaveTextContent(RANGE);
        rerender(<SkillGovernanceForm value={{ approval_timeout_seconds: 604801 }} onChange={vi.fn()} />);
        expect(screen.getByRole('alert')).toHaveTextContent(RANGE);
        rerender(<SkillGovernanceForm value={{ approval_timeout_seconds: 604800 }} onChange={vi.fn()} />);
        expect(screen.queryByRole('alert')).toBeNull();
    });
});
