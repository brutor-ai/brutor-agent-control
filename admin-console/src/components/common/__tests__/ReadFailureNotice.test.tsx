/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import ReadFailureNotice from '../ReadFailureNotice';
import { loadedAtClock } from '../../../hooks/useRead';

describe('ReadFailureNotice', () => {
    it('renders nothing where the read did not fail', () => {
        const { container } = render(<ReadFailureNotice failure={null} onRetry={() => undefined} />);
        expect(container.innerHTML).toBe('');
    });

    it('announces the sentence of a read that gave nothing, with a Retry that is a button', () => {
        const onRetry = vi.fn();
        render(<ReadFailureNotice failure={{ sentence: 'Failed to load the alerts: refused', loadedAt: null }} onRetry={onRetry} />);
        const alert = screen.getByRole('alert');
        expect(alert.textContent).toContain('Failed to load the alerts: refused');
        expect(alert.textContent).not.toContain('Could not refresh');
        const retry = screen.getByRole('button', { name: 'Retry' });
        expect(retry.getAttribute('type')).toBe('button');
        fireEvent.click(retry);
        expect(onRetry).toHaveBeenCalledTimes(1);
    });

    it('marks kept data as stale in words, with the time it was loaded and the reason', () => {
        const loadedAt = new Date(2026, 0, 5, 14, 5).getTime();
        render(<ReadFailureNotice failure={{ sentence: 'Failed to load the alerts: timeout', loadedAt }} onRetry={() => undefined} />);
        const mark = screen.getByRole('status');
        expect(mark.textContent).toContain(`Could not refresh; showing what was loaded at ${loadedAtClock(loadedAt)}`);
        expect(mark.textContent).toContain('Failed to load the alerts: timeout');
        expect(screen.queryByRole('alert')).toBeNull();
        expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    });

    it('writes the time as hours and minutes', () => {
        expect(loadedAtClock(new Date(2026, 0, 5, 14, 5).getTime())).toBe('14:05');
        expect(loadedAtClock(new Date(2026, 0, 5, 9, 0).getTime())).toBe('09:00');
    });

    it('has no Retry where no way to ask again is given', () => {
        render(<ReadFailureNotice failure={{ sentence: 'Failed to load the groups', loadedAt: null }} />);
        expect(screen.queryByRole('button')).toBeNull();
        expect(screen.getByRole('alert').textContent).toContain('Failed to load the groups');
    });

    it('the compact form is one line with the same role, sentence and button', () => {
        render(<ReadFailureNotice compact failure={{ sentence: 'Failed to load the groups: 503', loadedAt: null }} onRetry={() => undefined} />);
        expect(screen.getByRole('alert').textContent).toContain('Failed to load the groups: 503');
        expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    });
});
