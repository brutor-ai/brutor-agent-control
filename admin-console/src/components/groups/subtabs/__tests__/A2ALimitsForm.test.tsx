/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The agent-to-agent limits form: invocation caps and delegation depth.
 *
 * `max_delegation_depth` is the one field here that mirrors a hard proxy
 * constant — the Rust runtime refuses any chain deeper than
 * `ABSOLUTE_MAX_DEPTH` whatever the tenant asks for — so the input must
 * carry that ceiling rather than letting an operator save a number the
 * runtime will silently ignore.
 *
 * The parse guard is the other thing worth pinning: garbage typed into a
 * number field must emit NOTHING, so the owning tab is not marked dirty by
 * an edit that was never a number.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import A2ALimitsForm, { ABSOLUTE_MAX_DEPTH } from '../A2ALimitsForm';

function renderForm(props: Partial<React.ComponentProps<typeof A2ALimitsForm>> = {}) {
    const onChange = vi.fn();
    const view = render(<A2ALimitsForm value={{}} onChange={onChange} {...props} />);
    return { onChange, ...view };
}

const fieldFor = (label: string) =>
    screen.getByText(label).closest('div')?.parentElement?.querySelector('input') as HTMLInputElement;

describe('A2ALimitsForm — the two sections', () => {
    it('caps invocations and delegation depth separately', () => {
        renderForm();
        expect(screen.getByText('Invocations')).toBeInTheDocument();
        expect(screen.getByText('Delegation chain')).toBeInTheDocument();
        expect(screen.getByText('Daily limit')).toBeInTheDocument();
        expect(screen.getByText('max_delegation_depth')).toBeInTheDocument();
    });

    it('states that invocation caps block, and the percentages only warn', () => {
        renderForm();
        expect(screen.getByText(/always block once the daily or monthly limit/)).toBeInTheDocument();
    });

    it('names the proxy’s absolute depth ceiling in the section description', () => {
        renderForm();
        expect(screen.getByText(new RegExp(`Absolute proxy cap: ${ABSOLUTE_MAX_DEPTH}`))).toBeInTheDocument();
    });

    it('the depth input carries that ceiling so an ignorable value cannot be saved', () => {
        renderForm();
        expect(fieldFor('max_delegation_depth')).toHaveAttribute('max', String(ABSOLUTE_MAX_DEPTH));
    });

    it('offers no warning percentages: nothing alerts before the cap in this release', () => {
        renderForm();
        expect(screen.queryByText('Daily warning (%)')).toBeNull();
        expect(screen.queryByText('Monthly warning (%)')).toBeNull();
    });
});

describe('A2ALimitsForm — editing', () => {
    it('reads back the stored limits', () => {
        renderForm({ value: { invocations: { daily_limit: 250 }, chain: { max_delegation_depth: 4 } } });
        expect(fieldFor('Daily limit')).toHaveValue(250);
        expect(fieldFor('max_delegation_depth')).toHaveValue(4);
    });

    it('an unset limit shows the em-dash placeholder rather than a zero', () => {
        renderForm();
        expect(fieldFor('Daily limit')).toHaveValue(null);
        expect(fieldFor('Daily limit')).toHaveAttribute('placeholder', '—');
    });

    it('an edit merges into its section and leaves the other alone', () => {
        const { onChange } = renderForm({
            value: { invocations: { daily_limit: 100, monthly_limit: 2000 }, chain: { max_delegation_depth: 4 } },
        });
        fireEvent.change(fieldFor('Daily limit'), { target: { value: '150' } });
        expect(onChange).toHaveBeenCalledWith({
            invocations: { daily_limit: 150, monthly_limit: 2000 },
            chain: { max_delegation_depth: 4 },
        });
    });

    it('clearing a limit emits an explicit null so the save can unset it', () => {
        const { onChange } = renderForm({ value: { invocations: { daily_limit: 100 } } });
        fireEvent.change(fieldFor('Daily limit'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ invocations: { daily_limit: null } });
    });

    it('a section with nothing stored yet accepts its first value', () => {
        const { onChange } = renderForm({ value: { invocations: { daily_limit: 1 } } });
        fireEvent.change(fieldFor('max_delegation_depth'), { target: { value: '8' } });
        expect(onChange).toHaveBeenCalledWith({
            invocations: { daily_limit: 1 },
            chain: { max_delegation_depth: 8 },
        });
    });

    it('a read-only caller gets fields it cannot edit', () => {
        renderForm({ disabled: true });
        expect(fieldFor('Daily limit')).toBeDisabled();
        expect(fieldFor('max_delegation_depth')).toBeDisabled();
    });
});

describe('A2ALimitsForm — inheritance hints', () => {
    it('says what each field inherits', () => {
        renderForm({
            inherited: { invocations: { daily_limit: 500 } },
        });
        expect(screen.getByText('Inherited: 500')).toBeInTheDocument();
    });

    it('says nothing about inheritance for a root group', () => {
        renderForm();
        expect(screen.queryByText(/Inherited:/)).toBeNull();
    });

    it('discloses the effective value after the restrictive merge', () => {
        renderForm({
            value: { invocations: { daily_limit: 900 } },
            effective: { invocations: { daily_limit: 500 } },
        });
        expect(screen.getByText('Effective: 500')).toBeInTheDocument();
    });

    it('a non-numeric inherited value is ignored rather than printed raw', () => {
        renderForm({ inherited: { invocations: { daily_limit: 'unlimited' } } });
        expect(screen.queryByText(/Inherited:/)).toBeNull();
    });
});
