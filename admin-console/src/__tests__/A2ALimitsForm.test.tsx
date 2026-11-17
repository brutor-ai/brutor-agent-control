/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A2ALimitsForm — pure presentational A2A usage-limits form.
 *
 * These tests pin the contract:
 *   (1) Both cards (Invocations / Delegation chain) render.
 *   (2) Every field round-trips through `onChange` with the exact
 *       nested-JSON shape the Rust enforcement layer reads
 *       (invocations.* and chain.max_delegation_depth).
 *   (3) The delegation-depth field is bounded by ABSOLUTE_MAX_DEPTH
 *       (=32) — surfaced as the native `max` attribute (the hard
 *       ceiling the proxy also enforces).
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import A2ALimitsForm, {
    ABSOLUTE_MAX_DEPTH,
    type A2ALimits,
} from '../components/groups/subtabs/A2ALimitsForm';

// Resolve the <input type="number"> that belongs to a given LimitRow
// label. The row is a grid: [ <div><label/></div>, <input/>, <span/> ].
function numInput(label: string): HTMLInputElement {
    const el = screen.getByText(label);
    const grid = el.parentElement!.parentElement!;
    const input = grid.querySelector('input[type="number"]') as HTMLInputElement;
    if (!input) throw new Error(`No number input under label: ${label}`);
    return input;
}

describe('A2ALimitsForm — structure', () => {
    it('renders both cards', () => {
        render(<A2ALimitsForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByText('Invocations')).toBeInTheDocument();
        expect(screen.getByText('Delegation chain')).toBeInTheDocument();
    });

    it('renders all invocation + chain fields', () => {
        render(<A2ALimitsForm value={{}} onChange={vi.fn()} />);
        for (const label of [
            'Daily limit',
            'Monthly limit',
            'max_delegation_depth',
        ]) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
        // There is no hard_stop opt-out — invocation caps always enforce.
        expect(screen.queryByRole('checkbox')).toBeNull();
    });
});

describe('A2ALimitsForm — round-trip onChange shape', () => {
    it('invocations.daily_limit', () => {
        const onChange = vi.fn();
        render(<A2ALimitsForm value={{}} onChange={onChange} />);
        fireEvent.change(numInput('Daily limit'), { target: { value: '10' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ invocations: { daily_limit: 10 } }),
        );
    });

    it('invocations.monthly_limit', () => {
        const onChange = vi.fn();
        render(<A2ALimitsForm value={{}} onChange={onChange} />);
        fireEvent.change(numInput('Monthly limit'), { target: { value: '200' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ invocations: { monthly_limit: 200 } }),
        );
    });

    it('no longer renders an invocations hard_stop checkbox (caps always enforce)', () => {
        const onChange = vi.fn();
        render(<A2ALimitsForm value={{}} onChange={onChange} />);
        expect(screen.queryByRole('checkbox')).toBeNull();
    });

    it('chain.max_delegation_depth', () => {
        const onChange = vi.fn();
        render(<A2ALimitsForm value={{}} onChange={onChange} />);
        fireEvent.change(numInput('max_delegation_depth'), { target: { value: '5' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ chain: { max_delegation_depth: 5 } }),
        );
    });

    it('clearing a field to empty emits null (unset intent)', () => {
        const onChange = vi.fn();
        const initial: A2ALimits = { invocations: { daily_limit: 10 } };
        render(<A2ALimitsForm value={initial} onChange={onChange} />);
        fireEvent.change(screen.getByDisplayValue('10'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ invocations: { daily_limit: null } }),
        );
    });

    it('preserves the other section when one field changes', () => {
        const onChange = vi.fn();
        const initial: A2ALimits = {
            invocations: { daily_limit: 10 },
            chain: { max_delegation_depth: 4 },
        };
        render(<A2ALimitsForm value={initial} onChange={onChange} />);
        fireEvent.change(numInput('max_delegation_depth'), { target: { value: '6' } });
        const next = onChange.mock.calls[0][0];
        expect(next).toMatchObject({
            invocations: { daily_limit: 10 },
            chain: { max_delegation_depth: 6 },
        });
    });
});

describe('A2ALimitsForm — delegation-depth ceiling', () => {
    it('caps the depth input at ABSOLUTE_MAX_DEPTH via the native max attribute', () => {
        render(<A2ALimitsForm value={{}} onChange={vi.fn()} />);
        const depth = numInput('max_delegation_depth');
        expect(ABSOLUTE_MAX_DEPTH).toBe(32);
        expect(depth.getAttribute('max')).toBe(String(ABSOLUTE_MAX_DEPTH));
    });

    it('does not JS-clamp above the ceiling — the native max attr + proxy enforce it', () => {
        // The component parses the raw value straight through; the hard
        // cap is the DOM `max` attribute (and the Rust proxy's
        // ABSOLUTE_MAX_DEPTH). Pin that behavior so a "clamp in JS" change
        // is a conscious decision, not a silent one.
        const onChange = vi.fn();
        render(<A2ALimitsForm value={{}} onChange={onChange} />);
        fireEvent.change(numInput('max_delegation_depth'), { target: { value: '40' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ chain: { max_delegation_depth: 40 } }),
        );
    });
});
