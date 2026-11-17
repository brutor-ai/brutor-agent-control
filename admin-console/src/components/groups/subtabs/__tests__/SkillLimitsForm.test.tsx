/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The skill execution-limits form.
 *
 * It is presentational on purpose — the parent tab owns the fetch, the save
 * and the dirty state — so what is worth pinning is the wiring: that each
 * field reaches the right `section.field` path in the emitted dict, that the
 * inherited / capacity props reach the shared field renderer, and that a
 * ROOT group (no inherited and no capacity object) is shown no inheritance
 * it does not have.
 *
 * The capacity popover is single-open: two breakdowns on screen at once
 * would invite comparing two different budgets as though they were one.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SkillLimitsForm from '../SkillLimitsForm';

const FIELDS = [
    ['Daily Limit', 'execution.daily_limit'],
    ['Monthly Limit', 'execution.monthly_limit'],
    ['Daily Warning (%)', 'execution.daily_warning_percent'],
    ['Monthly Warning (%)', 'execution.monthly_warning_percent'],
] as const;

function renderForm(props: Partial<React.ComponentProps<typeof SkillLimitsForm>> = {}) {
    const onChange = vi.fn();
    const view = render(<SkillLimitsForm value={{}} onChange={onChange} {...props} />);
    return { onChange, ...view };
}

describe('SkillLimitsForm — the field set', () => {
    it('offers the four execution limits, each on its own path', () => {
        renderForm();
        for (const [label, key] of FIELDS) {
            expect(screen.getByTestId(`limit-field-${key}`)).toHaveTextContent(label);
        }
    });

    it('reads back what the group has set', () => {
        renderForm({ value: { execution: { daily_limit: 500, daily_warning_percent: 80 } } });
        expect(screen.getByTestId('limit-input-execution.daily_limit')).toHaveValue(500);
        expect(screen.getByTestId('limit-input-execution.daily_warning_percent')).toHaveValue(80);
    });

    it('emits the whole limits dict with only the edited leaf changed', () => {
        const { onChange } = renderForm({
            value: { execution: { daily_limit: 100, monthly_limit: 2000 } },
        });
        fireEvent.change(screen.getByTestId('limit-input-execution.daily_limit'), {
            target: { value: '250' },
        });
        expect(onChange).toHaveBeenCalledWith({
            execution: { daily_limit: 250, monthly_limit: 2000 },
        });
    });

    it('clearing a limit emits undefined — "no cap", not "cap of zero"', () => {
        const { onChange } = renderForm({ value: { execution: { daily_limit: 100 } } });
        fireEvent.change(screen.getByTestId('limit-input-execution.daily_limit'), {
            target: { value: '' },
        });
        expect(onChange).toHaveBeenCalledWith({ execution: { daily_limit: undefined } });
    });

    it('a read-only caller gets fields it cannot edit', () => {
        renderForm({ disabled: true });
        for (const [, key] of FIELDS) {
            expect(screen.getByTestId(`limit-input-${key}`)).toBeDisabled();
        }
    });
});

describe('SkillLimitsForm — inheritance', () => {
    it('a root group is shown no inherited ceiling', () => {
        renderForm({ value: {} });
        expect(screen.queryByTestId('limit-inherited-execution.daily_limit')).toBeNull();
    });

    it('an inherited ceiling reaches the field and is flagged as exceeded', () => {
        renderForm({
            value: { execution: { daily_limit: 900 } },
            inherited: { execution: { daily_limit: 500 } },
        });
        expect(screen.getByTestId('limit-inherited-execution.daily_limit')).toHaveTextContent('max 500');
        expect(screen.getByTestId('limit-error-execution.daily_limit')).toHaveTextContent(
            'the save will be rejected',
        );
    });

    it('a merge that tightened the group’s own value is disclosed', () => {
        renderForm({
            value: { execution: { monthly_limit: 900 } },
            inherited: { execution: { monthly_limit: 900 } },
            effective: { execution: { monthly_limit: 400 } },
        });
        expect(screen.getByTestId('limit-effective-execution.monthly_limit')).toHaveTextContent(
            'Effective: 400',
        );
    });

    it('percent fields carry their unit into the inherited badge', () => {
        renderForm({ inherited: { execution: { daily_warning_percent: 75 } } });
        expect(screen.getByTestId('limit-inherited-execution.daily_warning_percent')).toHaveTextContent(
            'max 75%',
        );
    });

    it('shared-budget capacity replaces the bare ceiling with the arithmetic', () => {
        renderForm({
            capacity: {
                parent_limit: { execution: { daily_limit: 1000 } },
                parent_name: 'ACME Corp',
                parent_is_immediate: true,
                siblings: [],
                sibling_total: { execution: { daily_limit: 600 } },
                available: { execution: { daily_limit: 400 } },
            },
        });
        expect(screen.getByTestId('limit-available-execution.daily_limit')).toHaveTextContent(
            'available 400 of 1000',
        );
    });
});

describe('SkillLimitsForm — the capacity breakdown', () => {
    const capacity = {
        parent_limit: { execution: { daily_limit: 1000, monthly_limit: 20000 } },
        parent_name: 'ACME Corp',
        parent_is_immediate: true,
        siblings: [],
        sibling_total: { execution: { daily_limit: 600, monthly_limit: 5000 } },
        available: { execution: { daily_limit: 400, monthly_limit: 15000 } },
    };

    it('opens one field’s breakdown and closes it again', () => {
        renderForm({ capacity });
        const toggle = screen.getByTestId('limit-capacity-toggle-execution.daily_limit');
        fireEvent.click(toggle);
        expect(screen.getByTestId('limit-capacity-execution.daily_limit')).toBeInTheDocument();
        fireEvent.click(toggle);
        expect(screen.queryByTestId('limit-capacity-execution.daily_limit')).toBeNull();
    });

    it('only one breakdown is open at a time', () => {
        renderForm({ capacity });
        fireEvent.click(screen.getByTestId('limit-capacity-toggle-execution.daily_limit'));
        fireEvent.click(screen.getByTestId('limit-capacity-toggle-execution.monthly_limit'));
        expect(screen.getByTestId('limit-capacity-execution.monthly_limit')).toBeInTheDocument();
        expect(screen.queryByTestId('limit-capacity-execution.daily_limit')).toBeNull();
    });
});

describe('SkillLimitsForm — the help modal', () => {
    it('stays closed until asked for', () => {
        renderForm();
        expect(screen.getByTitle('Help')).toBeInTheDocument();
        expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('opens on the help button', () => {
        renderForm();
        fireEvent.click(screen.getByTitle('Help'));
        // The modal owns the only heading that names the section's rules.
        expect(screen.getAllByText(/skill/i).length).toBeGreaterThan(0);
    });
});
