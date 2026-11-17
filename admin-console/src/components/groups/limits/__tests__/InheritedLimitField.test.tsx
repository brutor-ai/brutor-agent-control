/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The one limit input every group-wide limits form uses.
 *
 * Three facts have to survive on every field, because the operator is
 * predicting a server decision while they type:
 *
 *   - the ceiling merged from ancestors ("max N"), which the API refuses
 *     to exceed — so the field says so BEFORE the save;
 *   - the capacity still unclaimed under the budget holder ("available X
 *     of Y"), which is a warning and not a refusal;
 *   - the effective value after the restrictive merge, but only when the
 *     group set its own and the merge tightened it.
 *
 * A root group inherits nothing, so `hasParent={false}` must silence the
 * badges entirely rather than show a floor that does not exist.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import InheritedLimitField from '../InheritedLimitField';
import type { FieldCapacity } from '../../../../lib/limitCapacity';

function capacity(overrides: Partial<FieldCapacity> = {}): FieldCapacity {
    return {
        holderLimit: 100,
        holderName: 'Engineering',
        holderIsParent: true,
        claimed: 70,
        available: 30,
        others: [],
        ...overrides,
    };
}

const noop = () => {};

describe('InheritedLimitField — what the field discloses', () => {
    it('an ungoverned field offers no badges and reads "No limit"', () => {
        render(
            <InheritedLimitField label="Requests / min" value={undefined} onChange={noop} fieldKey="rpm" />,
        );
        expect(screen.queryByTestId('limit-inherited-rpm')).toBeNull();
        expect(screen.queryByTestId('limit-available-rpm')).toBeNull();
        expect(screen.getByTestId('limit-input-rpm')).toHaveAttribute('placeholder', 'No limit');
    });

    it('an inherited ceiling shows as "max N" and seeds the placeholder', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={50}
                suffix=" rpm"
                fieldKey="rpm"
            />,
        );
        expect(screen.getByTestId('limit-inherited-rpm')).toHaveTextContent('max 50 rpm');
        expect(screen.getByTestId('limit-input-rpm')).toHaveAttribute('placeholder', 'Inherited: 50');
    });

    it('a root group is shown no inherited floor it does not have', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={50}
                capacity={capacity()}
                hasParent={false}
                fieldKey="rpm"
            />,
        );
        expect(screen.queryByTestId('limit-inherited-rpm')).toBeNull();
        expect(screen.queryByTestId('limit-available-rpm')).toBeNull();
        expect(screen.getByTestId('limit-input-rpm')).toHaveAttribute('placeholder', 'No limit');
    });

    it('capacity replaces the bare ceiling with the shared-budget arithmetic', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={100}
                capacity={capacity()}
                fieldKey="rpm"
            />,
        );
        expect(screen.getByTestId('limit-available-rpm')).toHaveTextContent('available 30 of 100');
        // The two badges never both show — capacity is the more specific fact.
        expect(screen.queryByTestId('limit-inherited-rpm')).toBeNull();
        expect(screen.getByTestId('limit-input-rpm')).toHaveAttribute('placeholder', 'Available: 30');
    });
});

describe('InheritedLimitField — predicting the server', () => {
    it('over the inherited floor names the refusal that is coming', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={80}
                onChange={noop}
                inherited={50}
                suffix=" rpm"
                fieldKey="rpm"
            />,
        );
        expect(screen.getByTestId('limit-error-rpm')).toHaveTextContent(
            'Above the inherited limit of 50 rpm — the save will be rejected',
        );
    });

    it('over the unclaimed capacity states the arithmetic instead of a refusal', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={40}
                onChange={noop}
                inherited={100}
                capacity={capacity()}
                fieldKey="rpm"
            />,
        );
        const err = screen.getByTestId('limit-error-rpm');
        expect(err).toHaveTextContent('Above the 30 still unclaimed');
        expect(err).toHaveTextContent('Engineering allows 100');
        expect(err).toHaveTextContent('other groups have taken 70');
    });

    it('a value inside both ceilings raises nothing', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={20}
                onChange={noop}
                inherited={100}
                capacity={capacity()}
                fieldKey="rpm"
            />,
        );
        expect(screen.queryByTestId('limit-error-rpm')).toBeNull();
    });

    it('clamps the input max to the tightest of the two ceilings', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={100}
                capacity={capacity({ available: 30 })}
                fieldKey="rpm"
            />,
        );
        expect(screen.getByTestId('limit-input-rpm')).toHaveAttribute('max', '30');
    });
});

describe('InheritedLimitField — the effective note', () => {
    it('surfaces a merge that tightened what the group set', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={80}
                onChange={noop}
                effective={50}
                suffix=" rpm"
                fieldKey="rpm"
            />,
        );
        expect(screen.getByTestId('limit-effective-rpm')).toHaveTextContent('Effective: 50 rpm');
    });

    it('stays quiet when the merge changed nothing', () => {
        render(
            <InheritedLimitField label="Requests / min" value={50} onChange={noop} effective={50} fieldKey="rpm" />,
        );
        expect(screen.queryByTestId('limit-effective-rpm')).toBeNull();
    });

    it('stays quiet when the group set nothing — the placeholder already says it', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={50}
                effective={50}
                fieldKey="rpm"
            />,
        );
        expect(screen.queryByTestId('limit-effective-rpm')).toBeNull();
    });
});

describe('InheritedLimitField — editing', () => {
    it('reports a typed number, and clearing the box reports "unset"', () => {
        const onChange = vi.fn();
        render(<InheritedLimitField label="Requests / min" value={10} onChange={onChange} fieldKey="rpm" />);
        const input = screen.getByTestId('limit-input-rpm');

        fireEvent.change(input, { target: { value: '25' } });
        expect(onChange).toHaveBeenLastCalledWith(25);

        // Clearing must mean "no limit here", not zero.
        fireEvent.change(input, { target: { value: '' } });
        expect(onChange).toHaveBeenLastCalledWith(undefined);
    });

    it('a disabled field cannot be edited', () => {
        render(
            <InheritedLimitField label="Requests / min" value={10} onChange={noop} disabled fieldKey="rpm" />,
        );
        expect(screen.getByTestId('limit-input-rpm')).toBeDisabled();
    });
});

describe('InheritedLimitField — the capacity breakdown', () => {
    it('the toggle is only offered where there is capacity to break down', () => {
        const onToggle = vi.fn();
        const { rerender } = render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                inherited={50}
                onTogglePopover={onToggle}
                fieldKey="rpm"
            />,
        );
        expect(screen.queryByTestId('limit-capacity-toggle-rpm')).toBeNull();

        rerender(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                capacity={capacity()}
                onTogglePopover={onToggle}
                fieldKey="rpm"
            />,
        );
        fireEvent.click(screen.getByTestId('limit-capacity-toggle-rpm'));
        expect(onToggle).toHaveBeenCalledWith('rpm');
    });

    it('an open popover closes back to no field', () => {
        const onToggle = vi.fn();
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                capacity={capacity()}
                onTogglePopover={onToggle}
                popoverOpen
                fieldKey="rpm"
            />,
        );
        fireEvent.click(screen.getByTestId('limit-capacity-toggle-rpm'));
        expect(onToggle).toHaveBeenCalledWith(null);
    });

    it('names the other groups drawing on the budget', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                capacity={capacity({
                    others: [
                        { id: 'g-2', name: 'Finance', value: 40 },
                        { id: 'g-3', name: 'Legal', value: undefined },
                    ],
                })}
                popoverOpen
                fieldKey="rpm"
            />,
        );
        const pop = screen.getByTestId('limit-capacity-rpm');
        expect(pop).toHaveTextContent('Finance');
        expect(pop).toHaveTextContent('40');
        // A sibling that set nothing reads as a dash, not as zero.
        expect(pop).toHaveTextContent('Legal:—');
        expect(pop).toHaveTextContent('Already claimed:70');
        expect(pop).toHaveTextContent('Available for this group:30');
    });

    it('a budget held further up is labelled "Set by", not "Parent"', () => {
        render(
            <InheritedLimitField
                label="Requests / min"
                value={undefined}
                onChange={noop}
                capacity={capacity({ holderIsParent: false, holderName: 'ACME Corp' })}
                popoverOpen
                fieldKey="rpm"
            />,
        );
        const pop = screen.getByTestId('limit-capacity-rpm');
        expect(pop).toHaveTextContent('Set by');
        expect(pop).toHaveTextContent('ACME Corp');
        expect(pop).not.toHaveTextContent('Parent');
    });
});
