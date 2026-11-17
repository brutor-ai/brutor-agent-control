/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The close dialog. What it must never do:
 *
 *   - close without a kind (there is no default kind);
 *   - close without a note;
 *   - hide what a kind does to detection until after the click;
 *   - send a cause_fixed close without the window the operator saw — or
 *     send window fields on a kind that has none.
 */

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import ResolveDriftDialog from '../ResolveDriftDialog';
import type { DriftEvent } from '../../../lib/aiSystemTypes';
import { toLocalInputValue } from '../../../lib/driftResolution';
import { RESOLUTION_KIND_EFFECTS } from '../../../lib/driftResolutionTypes';

const NOW = new Date('2026-09-28T07:00:00Z');

const finding: DriftEvent = {
    id: 'drift-1',
    ai_system_id: 'sys-1',
    drift_class: 'contract',
    metric_key: 'envelope:min_completion_rate',
    detected_at: '2026-09-28T01:00:00Z',
    change_point_at: '2026-09-27T21:00:00Z',
    severity: 'high',
    attributed_cause: 'contract_breach',
    cause_evidence: {},
    correlated_metrics: [],
    response_taken: {},
    status: 'active',
    is_open: true,
    is_explained: true,
};

function renderDialog(onConfirm = vi.fn().mockResolvedValue(undefined)) {
    render(
        <ResolveDriftDialog
            events={[finding]}
            title="Completion rate breached"
            onConfirm={onConfirm}
            onCancel={vi.fn()}
            now={NOW}
        />,
    );
    return onConfirm;
}

const choose = (kind: string) =>
    fireEvent.click(within(screen.getByTestId(`resolve-kind-${kind}`)).getByRole('radio'));
const note = (text: string) => fireEvent.change(screen.getByTestId('resolve-note'), { target: { value: text } });

describe('ResolveDriftDialog', () => {
    it('requires a kind: a note alone cannot close the finding', () => {
        const onConfirm = renderDialog();
        note('fixed it');
        expect(screen.getByTestId('resolve-confirm')).toBeDisabled();
        expect(screen.getByTestId('resolve-blocked')).toHaveTextContent(/kind/);
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        expect(onConfirm).not.toHaveBeenCalled();
    });

    it('requires a note: a kind alone cannot close the finding', () => {
        renderDialog();
        choose('accepted_change');
        expect(screen.getByTestId('resolve-confirm')).toBeDisabled();
        expect(screen.getByTestId('resolve-blocked')).toHaveTextContent(/note/i);
    });

    it('states each kind’s effect on detection before the choice', () => {
        renderDialog();
        for (const kind of ['accepted_change', 'cause_fixed', 'false_positive'] as const) {
            expect(screen.getByTestId(`resolve-effect-${kind}`)).toHaveTextContent(RESOLUTION_KIND_EFFECTS[kind]);
        }
        // Nothing is pre-selected.
        for (const radio of screen.getAllByRole('radio')) expect(radio).not.toBeChecked();
    });

    it('cause fixed proposes change point → now and sends those bounds', async () => {
        const onConfirm = renderDialog();
        choose('cause_fixed');
        expect(screen.getByTestId('resolve-window-start')).toHaveValue(toLocalInputValue('2026-09-27T21:00:00Z'));
        expect(screen.getByTestId('resolve-window-end')).toHaveValue(toLocalInputValue(NOW.toISOString()));
        fireEvent.change(screen.getByTestId('resolve-cause-category'), { target: { value: 'provider_outage' } });
        note('provider credits ran out');
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() =>
            expect(onConfirm).toHaveBeenCalledWith({
                kind: 'cause_fixed',
                note: 'provider credits ran out',
                window_starts_at: '2026-09-27T21:00:00.000Z',
                window_ends_at: '2026-09-28T07:00:00.000Z',
                cause_category: 'provider_outage',
                tenant_wide: false,
            }),
        );
    });

    it('cause fixed sends the bounds the operator edited, and the tenant-wide scope', async () => {
        const onConfirm = renderDialog();
        choose('cause_fixed');
        fireEvent.change(screen.getByTestId('resolve-window-end'), {
            target: { value: toLocalInputValue('2026-09-28T06:40:00Z') },
        });
        fireEvent.click(screen.getByTestId('resolve-tenant-wide'));
        note('outage');
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() => expect(onConfirm).toHaveBeenCalled());
        expect(onConfirm.mock.calls[0][0]).toMatchObject({
            window_ends_at: '2026-09-28T06:40:00.000Z',
            tenant_wide: true,
            cause_category: 'other',
        });
    });

    it('an inverted window blocks the close', () => {
        renderDialog();
        choose('cause_fixed');
        fireEvent.change(screen.getByTestId('resolve-window-end'), {
            target: { value: toLocalInputValue('2026-09-27T20:00:00Z') },
        });
        note('x');
        expect(screen.getByTestId('resolve-confirm')).toBeDisabled();
        expect(screen.getByTestId('resolve-blocked')).toHaveTextContent(/end after/);
    });

    it('switching away from cause fixed drops the window fields', async () => {
        const onConfirm = renderDialog();
        choose('cause_fixed');
        fireEvent.click(screen.getByTestId('resolve-tenant-wide'));
        choose('accepted_change');
        expect(screen.queryByTestId('resolve-window')).not.toBeInTheDocument();
        note('the retune');
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() => expect(onConfirm).toHaveBeenCalledWith({ kind: 'accepted_change', note: 'the retune' }));
    });

    it('a refused close stays open with the server’s reason', async () => {
        renderDialog(vi.fn().mockRejectedValue(new Error('already closed (accepted_change)')));
        choose('false_positive');
        note('noise');
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        expect(await screen.findByTestId('resolve-error')).toHaveTextContent('already closed');
        expect(screen.getByTestId('resolve-drift-dialog')).toBeInTheDocument();
    });
});
