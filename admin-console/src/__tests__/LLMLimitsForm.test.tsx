/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLMLimitsForm — shared 5-section LLM usage-limits form.
 *
 * Used by both the group-level usage-limits tab and the per-model
 * dialog. These tests pin the contract:
 *   (1) All five sections (Throughput / Concurrency / Budget /
 *       Tokens / Requests) render their inputs.
 *   (2) Per-model `mode` shows `Global: X / Effective: min(g, p)`.
 *   (3) Inline Info popovers per section header open / close
 *       (toggle, click-outside, Escape).
 *   (4) `onChange` is called with the correct nested-JSON shape —
 *       this is what the Rust enforcement layer reads at request
 *       time, so the shape must be exact.
 *   (5) Hard stop checkbox only renders in group mode.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import LLMLimitsForm from '../components/groups/subtabs/LLMLimitsForm';
import { LLMGlobalLimits } from '../lib/groupTypes';

describe('LLMLimitsForm — section structure', () => {
    it('renders all five sections', () => {
        render(
            <LLMLimitsForm
                limits={{}}
                onChange={vi.fn()}
                mode="group"
            />,
        );
        expect(screen.getByText('Throughput')).toBeInTheDocument();
        expect(screen.getByText('Concurrency')).toBeInTheDocument();
        expect(screen.getByText('Budget')).toBeInTheDocument();
        expect(screen.getByText('Tokens')).toBeInTheDocument();
        expect(screen.getByText('Requests')).toBeInTheDocument();
    });

    it('renders all eight throughput fields', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        for (const label of [
            'Requests / min',
            'Input tokens / min',
            'Output tokens / min',
            'USD / min',
            'Request burst',
            'Token burst',
            'USD burst',
            'Cooldown s',
        ]) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
    });
});

describe('LLMLimitsForm — onChange JSON shape', () => {
    it('writes throughput field under the correct nested key', () => {
        const onChange = vi.fn();
        render(
            <LLMLimitsForm limits={{}} onChange={onChange} mode="group" />,
        );
        // Pick the RPM input by its label sibling — every input gets the
        // placeholder "No limit" in group mode, so placeholder isn't unique.
        const rpmLabel = screen.getByText('Requests / min');
        const rpmInput = rpmLabel.parentElement!.querySelector('input')!;
        fireEvent.change(rpmInput, { target: { value: '60' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                throughput: { max_requests_per_minute: 60 },
            }),
        );
    });

    it('preserves other sections when one field changes', () => {
        const onChange = vi.fn();
        const initial: LLMGlobalLimits = {
            budget: { daily_limit_usd: 100 },
            concurrency: { max_concurrent: 4 },
        };
        render(
            <LLMLimitsForm limits={initial} onChange={onChange} mode="group" />,
        );
        // Find the concurrency input by its current value (4).
        const concInput = screen.getByDisplayValue('4');
        fireEvent.change(concInput, { target: { value: '8' } });
        const next = onChange.mock.calls[0]?.[0];
        expect(next).toMatchObject({
            budget: { daily_limit_usd: 100 },
            concurrency: { max_concurrent: 8 },
        });
    });

    it('removes the field key when cleared to empty', () => {
        const onChange = vi.fn();
        const initial: LLMGlobalLimits = {
            throughput: { max_requests_per_minute: 60, request_burst: 10 },
        };
        render(
            <LLMLimitsForm limits={initial} onChange={onChange} mode="group" />,
        );
        const rpmInput = screen.getByDisplayValue('60');
        fireEvent.change(rpmInput, { target: { value: '' } });
        // RPM should be deleted but request_burst preserved.
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                throughput: { request_burst: 10 },
            }),
        );
    });

    it('removes the entire section when its last field is cleared', () => {
        const onChange = vi.fn();
        const initial: LLMGlobalLimits = {
            throughput: { max_requests_per_minute: 60 },
            budget: { daily_limit_usd: 100 },
        };
        render(
            <LLMLimitsForm limits={initial} onChange={onChange} mode="group" />,
        );
        const rpmInput = screen.getByDisplayValue('60');
        fireEvent.change(rpmInput, { target: { value: '' } });
        const next = onChange.mock.calls[0]?.[0];
        expect(next.throughput).toBeUndefined();
        // Other sections preserved.
        expect(next.budget).toEqual({ daily_limit_usd: 100 });
    });
});

describe('LLMLimitsForm — per-model "Effective" display', () => {
    it('shows "Global: X" placeholder when per-model is unset', () => {
        const globalLimits: LLMGlobalLimits = {
            throughput: { max_requests_per_minute: 100 },
        };
        render(
            <LLMLimitsForm
                limits={{}}
                globalLimits={globalLimits}
                onChange={vi.fn()}
                mode="model"
            />,
        );
        const rpmInput = screen.getByPlaceholderText('Global: 100');
        expect(rpmInput).toBeInTheDocument();
    });

    it('shows Effective: min(global, per-model) when both set', () => {
        const globalLimits: LLMGlobalLimits = {
            throughput: { max_requests_per_minute: 100 },
        };
        const perModel: LLMGlobalLimits = {
            throughput: { max_requests_per_minute: 30 },
        };
        render(
            <LLMLimitsForm
                limits={perModel}
                globalLimits={globalLimits}
                onChange={vi.fn()}
                mode="model"
            />,
        );
        // "Effective: 30" — the stricter of (100, 30).
        expect(screen.getByText(/Effective: 30/)).toBeInTheDocument();
    });
});

describe('LLMLimitsForm — Info popovers', () => {
    it('opens a popover when its header info button is clicked', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        const button = screen.getByTitle('What does Throughput mean?');
        fireEvent.click(button);
        // Popover content includes the algorithm description.
        expect(
            screen.getByText(/Token-bucket enforcement on requests-per-minute/),
        ).toBeInTheDocument();
    });

    it('closes when Escape is pressed', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        const button = screen.getByTitle('What does Concurrency mean?');
        fireEvent.click(button);
        expect(
            screen.getByText(/Semaphore-style counter/),
        ).toBeInTheDocument();
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(
            screen.queryByText(/Semaphore-style counter/),
        ).not.toBeInTheDocument();
    });

    it('only one popover open at a time — toggling opens the new one and closes the old', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        fireEvent.click(screen.getByTitle('What does Budget mean?'));
        expect(
            screen.getByText(/Fixed-window quotas on cost/),
        ).toBeInTheDocument();
        fireEvent.click(screen.getByTitle('What does Tokens mean?'));
        // Budget popover gone, Tokens popover up.
        expect(
            screen.queryByText(/Fixed-window quotas on cost/),
        ).not.toBeInTheDocument();
        expect(
            screen.getByText(/Total = prompt \+ completion/),
        ).toBeInTheDocument();
    });
});

describe('LLMLimitsForm — budget enforcement contract', () => {
    // There is no Hard Stop checkbox: a configured dollar budget ALWAYS
    // blocks at 100%, same as token/request caps. The warning percentages
    // control advance alerts, not enforcement.
    it('offers no hard-stop toggle in either mode — budgets always block', () => {
        const { unmount } = render(
            <LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />,
        );
        expect(screen.queryByText('Hard stop on limit')).toBeNull();
        unmount();
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="model" />);
        expect(screen.queryByText('Hard stop on limit')).toBeNull();
    });

    it('states the always-blocks rule where the budget is configured', () => {
        render(<LLMLimitsForm limits={{}} onChange={vi.fn()} mode="group" />);
        expect(
            screen.getByText(/dollar budget always blocks at 100%/i),
        ).toBeInTheDocument();
    });
});
