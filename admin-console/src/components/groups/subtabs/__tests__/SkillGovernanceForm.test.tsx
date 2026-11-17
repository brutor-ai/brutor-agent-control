/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The skill execution-governance form.
 *
 * Max concurrent executions bounds resource exhaustion. It floors at 1 — a
 * cap of zero would not be "unbounded", it would be "nothing runs" — and the
 * form says on its face what it is defending against. A cap on the tool
 * calls of one execution is not offered: the gateway does not count them in
 * this release.
 *
 * The empty option on Execution Mode carries the same meaning as everywhere
 * else in the console: on a root group it means ungoverned, under a parent
 * it means "whatever is inherited", so it relabels to name that value.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SkillGovernanceForm from '../SkillGovernanceForm';

function renderForm(props: Partial<React.ComponentProps<typeof SkillGovernanceForm>> = {}) {
    const onChange = vi.fn();
    const view = render(<SkillGovernanceForm value={{}} onChange={onChange} {...props} />);
    return { onChange, ...view };
}

describe('SkillGovernanceForm — the field set', () => {
    it('offers the execution mode, discovery, the approval timeout and the concurrency bound', () => {
        renderForm();
        expect(screen.getByText('Execution Mode')).toBeInTheDocument();
        expect(screen.getByText('Allow skill discovery for this group')).toBeInTheDocument();
        expect(screen.getByText(/Approval Timeout/)).toBeInTheDocument();
        expect(screen.getByText(/Max Concurrent Executions/)).toBeInTheDocument();
        expect(screen.queryByText(/Max Tool Calls Per Execution/)).toBeNull();
    });

    it('offers the three execution modes', () => {
        renderForm();
        for (const label of ['Unrestricted', 'Approval Required', 'Disabled']) {
            expect(screen.getByRole('option', { name: label })).toBeInTheDocument();
        }
    });

    it('says what the concurrency bound is defending against', () => {
        renderForm();
        expect(screen.getByText(/Prevents resource exhaustion/)).toBeInTheDocument();
    });

    it('the bounds floor at 1 — a cap of zero would stop everything', () => {
        renderForm();
        const numbers = screen.getAllByRole('spinbutton');
        // [approval timeout, max concurrent, max tool calls per execution (advisory)]
        // An approval timeout is 1 .. 604800 seconds.
        expect(numbers).toHaveLength(3);
        expect(numbers[0]).toHaveAttribute('min', '1');
        expect(numbers[0]).toHaveAttribute('max', '604800');
        expect(numbers[1]).toHaveAttribute('min', '1');
        expect(numbers[2]).toHaveAttribute('min', '1');
    });

    it('reads back what the group has set', () => {
        renderForm({
            value: {
                execution_mode: 'approval_required',
                discovery_enabled: true,
                approval_timeout_seconds: 300,
                max_concurrent_executions: 5,
            },
        });
        expect(screen.getByRole('combobox')).toHaveValue('approval_required');
        expect(screen.getByRole('checkbox')).toBeChecked();
        expect(screen.getByDisplayValue('300')).toBeInTheDocument();
        expect(screen.getByDisplayValue('5')).toBeInTheDocument();
    });

    it('a read-only caller gets fields it cannot edit', () => {
        renderForm({ disabled: true });
        expect(screen.getByRole('combobox')).toBeDisabled();
        expect(screen.getByRole('checkbox')).toBeDisabled();
        for (const n of screen.getAllByRole('spinbutton')) expect(n).toBeDisabled();
    });
});

describe('SkillGovernanceForm — editing', () => {
    it('emits the whole blob with only the edited key changed', () => {
        const { onChange } = renderForm({ value: { discovery_enabled: true } });
        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'disabled' } });
        expect(onChange).toHaveBeenCalledWith({ discovery_enabled: true, execution_mode: 'disabled' });
    });

    it('choosing the empty mode means "unset", not an empty string', () => {
        const { onChange } = renderForm({ value: { execution_mode: 'disabled' } });
        fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ execution_mode: undefined });
    });

    it('discovery is a plain boolean either way', () => {
        const { onChange } = renderForm({ value: { discovery_enabled: true } });
        fireEvent.click(screen.getByRole('checkbox'));
        expect(onChange).toHaveBeenCalledWith({ discovery_enabled: false });
    });

    it('a group that has never set discovery reads as off, not as indeterminate', () => {
        renderForm();
        expect(screen.getByRole('checkbox')).not.toBeChecked();
    });

    it('clearing a numeric bound emits undefined rather than zero', () => {
        const { onChange } = renderForm({ value: { max_concurrent_executions: 5 } });
        fireEvent.change(screen.getByDisplayValue('5'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ max_concurrent_executions: undefined });
    });

    it('numeric bounds are emitted as integers', () => {
        const { onChange } = renderForm();
        fireEvent.change(screen.getAllByRole('spinbutton')[1], { target: { value: '12' } });
        expect(onChange).toHaveBeenCalledWith({ max_concurrent_executions: 12 });
    });
});

describe('SkillGovernanceForm — inheritance', () => {
    it('a root group is told nothing about parents', () => {
        renderForm({ value: { execution_mode: 'disabled' } });
        expect(screen.queryByText(/inherited:/i)).toBeNull();
        expect(screen.getByRole('option', { name: 'Not set' })).toBeInTheDocument();
    });

    it('the empty option names what would be inherited', () => {
        renderForm({ inherited: { execution_mode: 'approval_required' } });
        expect(screen.getByRole('option', { name: 'Inherited: approval_required' })).toBeInTheDocument();
    });

    it('shows an inherited discovery flag as Yes/No, not as "true"', () => {
        const { rerender } = renderForm({ inherited: { discovery_enabled: false } });
        expect(screen.getByText(/inherited: No/)).toBeInTheDocument();
        rerender(
            <SkillGovernanceForm value={{}} onChange={vi.fn()} inherited={{ discovery_enabled: true }} />,
        );
        expect(screen.getByText(/inherited: Yes/)).toBeInTheDocument();
    });

    it('shows each numeric ceiling as a maximum, and seeds the placeholder', () => {
        renderForm({
            inherited: {
                approval_timeout_seconds: 120,
                max_concurrent_executions: 4,
            },
        });
        expect(screen.getByText('max 120')).toBeInTheDocument();
        expect(screen.getByText('max 4')).toBeInTheDocument();
        expect(screen.getByPlaceholderText('Inherited: 4')).toBeInTheDocument();
    });

    it('an ungoverned field reads "No limit" rather than a phantom ceiling', () => {
        renderForm();
        expect(screen.getAllByPlaceholderText('No limit')).toHaveLength(3);
    });
});

describe('SkillGovernanceForm — the effective hint', () => {
    it('discloses a merge that tightened the group’s own value', () => {
        renderForm({
            value: { max_concurrent_executions: 8 },
            inherited: { max_concurrent_executions: 10 },
            effective: { max_concurrent_executions: 4 },
        });
        expect(screen.getByText('Effective: 4')).toBeInTheDocument();
    });

    it('stays quiet when the merge changed nothing', () => {
        renderForm({
            value: { max_concurrent_executions: 8 },
            inherited: { max_concurrent_executions: 10 },
            effective: { max_concurrent_executions: 8 },
        });
        expect(screen.queryByText(/Effective:/)).toBeNull();
    });

    it('stays quiet when the group set nothing of its own', () => {
        renderForm({
            inherited: { max_concurrent_executions: 10 },
            effective: { max_concurrent_executions: 4 },
        });
        expect(screen.queryByText(/Effective:/)).toBeNull();
    });
});
