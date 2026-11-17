/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A child group's limits form must say what it inherits and how much of
 * the shared budget is left.
 *
 * The MCP, Skill and LLM forms render one shared field for it. Without it
 * an AI system under an organisation capped at $200/day shows sections of
 * bare "No limit" boxes.
 *
 * Capacity is computed against every ancestor, not the immediate parent
 * only: an estate may set its budget once at the organisation. The field
 * therefore has to cope with a budget holder that is not the parent, and
 * name it.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import { InheritedLimitField } from '../components/groups/limits/InheritedLimitField';
import {
    fieldCapacity,
    getNestedLimit,
    limitFieldVerdict,
} from '../lib/limitCapacity';

const CAPACITY = {
    parent_limit: { budget: { daily_limit_usd: 200 } },
    parent_name: 'Nordica Insurance Group',
    parent_is_immediate: false,
    siblings: [
        { group_id: 'g1', group_name: 'Customer Operations', limits: { budget: { daily_limit_usd: 80 } } },
    ],
    sibling_total: { budget: { daily_limit_usd: 80 } },
    available: { budget: { daily_limit_usd: 120 } },
};

describe('limitFieldVerdict', () => {
    it('is clean when nothing is typed', () => {
        expect(limitFieldVerdict(undefined, 100, null)).toMatchObject({
            overInherited: false,
            overCapacity: false,
        });
    });

    it('flags a value above the inherited floor', () => {
        expect(limitFieldVerdict(150, 100, null).overInherited).toBe(true);
        expect(limitFieldVerdict(100, 100, null).overInherited).toBe(false);
    });

    it('flags a value above the unclaimed capacity separately', () => {
        const cap = fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')!;
        expect(limitFieldVerdict(150, 200, cap)).toMatchObject({
            overInherited: false,
            overCapacity: true,
        });
    });

    it('takes the tightest of the two as the input ceiling', () => {
        const cap = fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')!;
        expect(limitFieldVerdict(10, 200, cap).ceiling).toBe(120);
        expect(limitFieldVerdict(10, 50, cap).ceiling).toBe(50);
        expect(limitFieldVerdict(10, undefined, null).ceiling).toBeUndefined();
    });
});

describe('fieldCapacity', () => {
    it('slices one field out of the group-wide blob', () => {
        expect(fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')).toEqual({
            holderLimit: 200,
            holderName: 'Nordica Insurance Group',
            holderIsParent: false,
            claimed: 80,
            available: 120,
            others: [{ id: 'g1', name: 'Customer Operations', value: 80 }],
        });
    });

    it('reports nothing for a field the holder does not cap', () => {
        expect(fieldCapacity(CAPACITY, 'tokens', 'daily_limit')).toBeNull();
        expect(fieldCapacity(null, 'budget', 'daily_limit_usd')).toBeNull();
    });

    it('reads only numbers out of a limits blob', () => {
        expect(getNestedLimit({ budget: { hard_stop: true } }, 'budget', 'hard_stop')).toBeUndefined();
        expect(getNestedLimit(undefined, 'budget', 'x')).toBeUndefined();
    });
});

describe('InheritedLimitField', () => {
    const base = {
        label: 'Daily Limit',
        fieldKey: 'budget.daily_limit_usd',
        onChange: vi.fn(),
    };

    it('shows the inherited ceiling on a group that sets nothing itself', () => {
        render(<InheritedLimitField {...base} value={undefined} inherited={200} />);
        expect(screen.getByTestId('limit-inherited-budget.daily_limit_usd')).toHaveTextContent('max 200');
        expect(screen.getByTestId('limit-input-budget.daily_limit_usd')).toHaveAttribute(
            'placeholder',
            'Inherited: 200',
        );
    });

    it('shows the unclaimed capacity when a budget is shared', () => {
        render(
            <InheritedLimitField
                {...base}
                value={undefined}
                inherited={200}
                capacity={fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')}
            />,
        );
        expect(screen.getByTestId('limit-available-budget.daily_limit_usd')).toHaveTextContent(
            'available 120 of 200',
        );
    });

    it('warns that a value above the inherited limit will be refused', () => {
        render(<InheritedLimitField {...base} value={500} inherited={200} />);
        expect(screen.getByTestId('limit-error-budget.daily_limit_usd')).toHaveTextContent(
            'Above the inherited limit of 200 — the save will be rejected',
        );
    });

    it('states the arithmetic when a value over-commits the shared budget', () => {
        render(
            <InheritedLimitField
                {...base}
                value={150}
                inherited={200}
                capacity={fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')}
            />,
        );
        const err = screen.getByTestId('limit-error-budget.daily_limit_usd');
        expect(err).toHaveTextContent('Above the 120 still unclaimed');
        expect(err).toHaveTextContent('Nordica Insurance Group allows 200');
        expect(err).toHaveTextContent('other groups have taken 80');
    });

    it('names the budget holder in the breakdown, which need not be the parent', () => {
        render(
            <InheritedLimitField
                {...base}
                value={50}
                capacity={fieldCapacity(CAPACITY, 'budget', 'daily_limit_usd')}
                popoverOpen
                onTogglePopover={vi.fn()}
            />,
        );
        const popover = screen.getByTestId('limit-capacity-budget.daily_limit_usd');
        expect(popover).toHaveTextContent('Set by “Nordica Insurance Group”');
        expect(popover).toHaveTextContent('Customer Operations');
        expect(popover).toHaveTextContent('Already claimed');
    });

    it('shows the effective value only when the merge tightened what was set', () => {
        // Nothing set here: the placeholder already reads "Inherited: 200",
        // so repeating it as an effective value is noise.
        const { rerender } = render(
            <InheritedLimitField {...base} value={undefined} inherited={200} effective={200} />,
        );
        expect(screen.queryByTestId('limit-effective-budget.daily_limit_usd')).not.toBeInTheDocument();

        // Set to 150 and merged down to 120 by a tighter ancestor: worth saying.
        rerender(<InheritedLimitField {...base} value={150} inherited={200} effective={120} />);
        expect(screen.getByTestId('limit-effective-budget.daily_limit_usd')).toHaveTextContent('Effective: 120');

        // Set to a value the merge leaves alone: nothing to add.
        rerender(<InheritedLimitField {...base} value={100} inherited={200} effective={100} />);
        expect(screen.queryByTestId('limit-effective-budget.daily_limit_usd')).not.toBeInTheDocument();
    });

    it('says nothing about inheritance on a root group', () => {
        render(<InheritedLimitField {...base} value={50} inherited={200} hasParent={false} />);
        expect(screen.queryByTestId('limit-inherited-budget.daily_limit_usd')).not.toBeInTheDocument();
        expect(screen.queryByTestId('limit-error-budget.daily_limit_usd')).not.toBeInTheDocument();
    });

    it('reports a cleared box as unset rather than zero', () => {
        const onChange = vi.fn();
        render(<InheritedLimitField {...base} onChange={onChange} value={50} inherited={200} />);
        fireEvent.change(screen.getByTestId('limit-input-budget.daily_limit_usd'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith(undefined);
    });
});
