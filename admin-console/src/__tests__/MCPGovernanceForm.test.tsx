/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCPGovernanceForm — pure "Default Access Levels" governance form.
 *
 * Pins the onChange contract:
 *   (1) The four access selects (tool / resource / prompt /
 *       resource-template) expose exactly enabled / approval_required /
 *       disabled and emit the chosen value under a flat field key.
 *   (2) Clearing a select emits `undefined` for that field.
 *   (3) approval_timeout_seconds (number) round-trips as an int.
 *   (4) Editing one field preserves the others (flat object spread).
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MCPGovernanceForm from '../components/groups/subtabs/MCPGovernanceForm';

/** Locate the <select> paired with a label. */
const selectFor = (label: string): HTMLSelectElement =>
    screen.getByText(label).parentElement!.querySelector('select')!;

const inputFor = (label: string): HTMLInputElement =>
    screen.getByText(label).parentElement!.querySelector('input')!;

// [field, label]
const SELECT_FIELDS: [string, string][] = [
    ['default_tool_access', 'Default Tool Access'],
    ['default_resource_access', 'Default Resource Access'],
    ['default_prompt_access', 'Default Prompt Access'],
    ['default_resource_template_access', 'Default Resource Template Access'],
];

describe('MCPGovernanceForm — select option values', () => {
    it('every access select exposes enabled / approval_required / disabled', () => {
        render(<MCPGovernanceForm value={{}} onChange={vi.fn()} />);
        for (const [, label] of SELECT_FIELDS) {
            const opts = Array.from(selectFor(label).querySelectorAll('option')).map(
                (o) => (o).value,
            );
            expect(opts).toEqual(['', 'enabled', 'approval_required', 'disabled']);
        }
    });
});

describe('MCPGovernanceForm — onChange shape (round-trip per field)', () => {
    it.each(SELECT_FIELDS)('%s writes the selected access value', (field, label) => {
        const onChange = vi.fn();
        render(<MCPGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(selectFor(label), { target: { value: 'approval_required' } });
        expect(onChange).toHaveBeenCalledWith({ [field]: 'approval_required' });
    });

    it.each(SELECT_FIELDS)('%s clears to undefined', (field, label) => {
        const onChange = vi.fn();
        render(
            <MCPGovernanceForm value={{ [field]: 'enabled' }} onChange={onChange} />,
        );
        fireEvent.change(selectFor(label), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ [field]: undefined });
    });

    it('approval_timeout_seconds round-trips as an int', () => {
        const onChange = vi.fn();
        render(<MCPGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(inputFor('Approval Timeout (seconds)'), {
            target: { value: '300' },
        });
        expect(onChange).toHaveBeenCalledWith({ approval_timeout_seconds: 300 });
    });

    it('approval_timeout_seconds clears to undefined', () => {
        const onChange = vi.fn();
        render(
            <MCPGovernanceForm
                value={{ approval_timeout_seconds: 300 }}
                onChange={onChange}
            />,
        );
        fireEvent.change(inputFor('Approval Timeout (seconds)'), {
            target: { value: '' },
        });
        expect(onChange).toHaveBeenCalledWith({ approval_timeout_seconds: undefined });
    });
});

describe('MCPGovernanceForm — merge semantics', () => {
    it('preserves other fields when one changes', () => {
        const onChange = vi.fn();
        render(
            <MCPGovernanceForm
                value={{ default_resource_access: 'disabled' }}
                onChange={onChange}
            />,
        );
        fireEvent.change(selectFor('Default Tool Access'), {
            target: { value: 'enabled' },
        });
        expect(onChange).toHaveBeenCalledWith({
            default_resource_access: 'disabled',
            default_tool_access: 'enabled',
        });
    });
});

describe('MCPGovernanceForm — approval timeout bounds', () => {
    const RANGE = 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).';

    it('bounds the field to 1 .. 604800 seconds', () => {
        render(<MCPGovernanceForm value={{}} onChange={vi.fn()} />);
        const input = screen.getByTestId<HTMLInputElement>('governance-input-approval_timeout_seconds');
        expect(input.min).toBe('1');
        expect(input.max).toBe('604800');
        expect(screen.queryByText(RANGE)).toBeNull();
    });

    it('says the range beside a value outside it', () => {
        const { rerender } = render(<MCPGovernanceForm value={{ approval_timeout_seconds: 0 }} onChange={vi.fn()} />);
        expect(screen.getByTestId('approval-timeout-problem')).toHaveTextContent(RANGE);
        rerender(<MCPGovernanceForm value={{ approval_timeout_seconds: 604801 }} onChange={vi.fn()} />);
        expect(screen.getByTestId('approval-timeout-problem')).toHaveTextContent(RANGE);
        rerender(<MCPGovernanceForm value={{ approval_timeout_seconds: 1 }} onChange={vi.fn()} />);
        expect(screen.queryByTestId('approval-timeout-problem')).toBeNull();
    });
});
