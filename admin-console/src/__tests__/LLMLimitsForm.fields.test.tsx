/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLMLimitsForm — per-field round-trip coverage for the Budget /
 * Tokens / Requests / Concurrency sections that the primary
 * LLMLimitsForm.test.tsx exercises only at the section level. Each
 * field here gets an individual onChange nested-shape assertion.
 *
 * Fields share labels across sections (e.g. "Daily Warning" appears in
 * Budget, Tokens and Requests), so every lookup is scoped to its
 * section via the section header + `within`.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

import LLMLimitsForm from '../components/groups/subtabs/LLMLimitsForm';

/** The number input for `label` scoped to the section with header `section`. */
function fieldInput(section: string, label: string) {
    const sec = screen.getByText(section).closest('section')!;
    const lbl = within(sec).getByText(label);
    const el = lbl.parentElement!.querySelector('input');
    if (!el) throw new Error(`no input for "${label}" in section "${section}"`);
    return el;
}

interface Case {
    section: string; // section header text
    sectionKey: string; // JSON section key
    label: string;
    key: string;
    value: number;
}

const CASES: Case[] = [
    // Budget
    { section: 'Budget', sectionKey: 'budget', label: 'Daily Limit', key: 'daily_limit_usd', value: 100 },
    { section: 'Budget', sectionKey: 'budget', label: 'Monthly Limit', key: 'monthly_limit_usd', value: 2500 },
    { section: 'Budget', sectionKey: 'budget', label: 'Daily Warning', key: 'daily_warning_percent', value: 80 },
    { section: 'Budget', sectionKey: 'budget', label: 'Monthly Warning', key: 'monthly_warning_percent', value: 90 },
    // Tokens
    { section: 'Tokens', sectionKey: 'tokens', label: 'Daily total', key: 'daily_limit', value: 1000000 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Monthly total', key: 'monthly_limit', value: 30000000 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Daily Warning', key: 'daily_warning_percent', value: 75 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Monthly Warning', key: 'monthly_warning_percent', value: 85 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Daily input', key: 'daily_input_limit', value: 800000 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Monthly input', key: 'monthly_input_limit', value: 24000000 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Daily output', key: 'daily_output_limit', value: 200000 },
    { section: 'Tokens', sectionKey: 'tokens', label: 'Monthly output', key: 'monthly_output_limit', value: 6000000 },
    // Requests
    { section: 'Requests', sectionKey: 'requests', label: 'Daily Limit', key: 'daily_limit', value: 5000 },
    { section: 'Requests', sectionKey: 'requests', label: 'Monthly Limit', key: 'monthly_limit', value: 150000 },
    { section: 'Requests', sectionKey: 'requests', label: 'Daily Warning', key: 'daily_warning_percent', value: 80 },
    { section: 'Requests', sectionKey: 'requests', label: 'Monthly Warning', key: 'monthly_warning_percent', value: 90 },
    // Concurrency
    { section: 'Concurrency', sectionKey: 'concurrency', label: 'Max concurrent', key: 'max_concurrent', value: 8 },
];

describe('LLMLimitsForm — per-field round-trip', () => {
    it.each(CASES)(
        '$sectionKey.$key writes under the correct nested key',
        ({ section, sectionKey, label, key, value }) => {
            const onChange = vi.fn();
            render(<LLMLimitsForm limits={{}} onChange={onChange} mode="group" />);
            fireEvent.change(fieldInput(section, label), { target: { value: String(value) } });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ [sectionKey]: { [key]: value } }),
            );
        },
    );
});

describe('LLMLimitsForm: the input and output token quotas', () => {
    it('says the four of them are enforced', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        expect(screen.getByTestId('token-quota-help')).toHaveTextContent(
            'The gateway enforces the four input and output quotas (daily and monthly) as it enforces the totals: a call is refused once any quota that is set is reached.',
        );
    });
});

describe('LLMLimitsForm — no budget hard_stop control', () => {
    // There is no hard_stop opt-out: a dollar budget always enforces at
    // 100%, so there is no checkbox to toggle it.
    it('renders no hard-stop control in group mode', () => {
        const onChange = vi.fn();
        render(<LLMLimitsForm limits={{}} onChange={onChange} mode="group" />);
        expect(screen.queryByText('Hard stop on limit')).toBeNull();
    });
});

describe('LLMLimitsForm — clear semantics per section', () => {
    it('clearing a budget field deletes just that key, preserving siblings', () => {
        const onChange = vi.fn();
        render(
            <LLMLimitsForm
                limits={{ budget: { daily_limit_usd: 100, monthly_limit_usd: 2500 } }}
                onChange={onChange}
                mode="group"
            />,
        );
        fireEvent.change(fieldInput('Budget', 'Daily Limit'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0];
        expect(next.budget).toEqual({ monthly_limit_usd: 2500 });
    });

    it('clearing the only concurrency field removes the section', () => {
        const onChange = vi.fn();
        render(
            <LLMLimitsForm
                limits={{ concurrency: { max_concurrent: 8 }, budget: { daily_limit_usd: 100 } }}
                onChange={onChange}
                mode="group"
            />,
        );
        fireEvent.change(fieldInput('Concurrency', 'Max concurrent'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0];
        expect(next.concurrency).toBeUndefined();
        expect(next.budget).toEqual({ daily_limit_usd: 100 });
    });
});
