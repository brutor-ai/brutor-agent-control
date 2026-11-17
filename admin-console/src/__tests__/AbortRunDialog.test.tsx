/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The abort confirm dialog.
 *
 * Two properties it must hold, both of which are about honesty rather than
 * mechanics: an operator cannot stop a run without saying why, and the
 * dialog states what abort actually does rather than implying a hard kill.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import AbortRunDialog from '../components/ai-systems/AbortRunDialog';

const base = {
    rootTaskId: '01JRUN000000000000000001',
    aiSystemName: 'Claims Triage Agent',
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
};

describe('AbortRunDialog', () => {
    it('cannot be confirmed without a reason', () => {
        render(<AbortRunDialog {...base} onConfirm={vi.fn()} />);
        expect(screen.getByTestId('abort-confirm')).toBeDisabled();
    });

    it('treats whitespace as no reason at all', () => {
        render(<AbortRunDialog {...base} onConfirm={vi.fn()} />);
        fireEvent.change(screen.getByTestId('abort-reason'), { target: { value: '   ' } });
        expect(screen.getByTestId('abort-confirm')).toBeDisabled();
    });

    it('submits the trimmed reason', async () => {
        const onConfirm = vi.fn().mockResolvedValue(undefined);
        render(<AbortRunDialog {...base} onConfirm={onConfirm} />);
        fireEvent.change(screen.getByTestId('abort-reason'), {
            target: { value: '  looping since 14:02  ' },
        });
        fireEvent.click(screen.getByTestId('abort-confirm'));
        await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('looping since 14:02'));
    });

    it('states that abort stops the NEXT action, not the current one', () => {
        // Overclaiming a hard kill is exactly what external validation
        // catches, and the honest version is still the control operators want.
        render(<AbortRunDialog {...base} onConfirm={vi.fn()} />);
        const note = screen.getByTestId('abort-limit-note');
        expect(note).toHaveTextContent('next');
        expect(note).toHaveTextContent('already in flight completes');
        expect(note).toHaveTextContent('does not undo');
    });

    it('says the blast radius is one run', () => {
        render(<AbortRunDialog {...base} onConfirm={vi.fn()} />);
        expect(screen.getByTestId('abort-scope-note')).toHaveTextContent(
            'only this run',
        );
    });

    it('names the run being stopped', () => {
        render(<AbortRunDialog {...base} onConfirm={vi.fn()} />);
        expect(screen.getByText(base.rootTaskId)).toBeInTheDocument();
        expect(screen.getByText(/Claims Triage Agent/)).toBeInTheDocument();
    });

    it('surfaces a failure instead of pretending the run stopped', async () => {
        const onConfirm = vi.fn().mockRejectedValue(new Error('run already finished'));
        render(<AbortRunDialog {...base} onConfirm={onConfirm} />);
        fireEvent.change(screen.getByTestId('abort-reason'), { target: { value: 'x' } });
        fireEvent.click(screen.getByTestId('abort-confirm'));
        expect(await screen.findByTestId('abort-error')).toHaveTextContent(
            'run already finished',
        );
        // And stays open so the operator can retry or cancel.
        expect(screen.getByTestId('abort-run-dialog')).toBeInTheDocument();
    });

    it('cancels without calling confirm', () => {
        const onCancel = vi.fn();
        const onConfirm = vi.fn();
        render(<AbortRunDialog {...base} onConfirm={onConfirm} onCancel={onCancel} />);
        fireEvent.click(screen.getByLabelText('Cancel'));
        expect(onCancel).toHaveBeenCalled();
        expect(onConfirm).not.toHaveBeenCalled();
    });
});
