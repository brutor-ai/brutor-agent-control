/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Break-glass panel tests.
 *
 * Break-glass has the same runtime effect as the hardcoded fail-open every
 * competing gateway ships: traffic passes unchecked. The three properties that
 * make it acceptable — attributed, explained, expiring — are UI-visible
 * properties as much as data ones, so most of these assert that the operator
 * cannot miss what is switched off or forget that it is on.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

import BreakGlassPanel from '../components/guardrails/BreakGlassPanel';
import type { BreakGlassState, BreakGlassWindow } from '../lib/guardrailTypes';

const _toast = {
    showToast: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
};
vi.mock('../components/common/Toast', () => ({ useToast: () => _toast }));

const _confirm = vi.fn().mockResolvedValue(true);
vi.mock('../components/common/ConfirmationDialog', () => ({ useConfirm: () => _confirm }));

const mockFetch = vi.fn();
const mockActivate = vi.fn();
const mockDeactivate = vi.fn();
const _client = {
    fetchGuardrailBreakGlass: mockFetch,
    activateGuardrailBreakGlass: mockActivate,
    deactivateGuardrailBreakGlass: mockDeactivate,
};
vi.mock('../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: _client }) }));

function window_(over: Partial<BreakGlassWindow> = {}): BreakGlassWindow {
    return {
        id: 'gbg-1',
        tenant_id: 't1',
        reason: 'Presidio returning 503s fleet-wide — INC-4471',
        activated_by: 'alice',
        activated_at: '2026-08-26T09:00:00Z',
        expires_at: '2026-08-26T10:00:00Z',
        deactivated_at: null,
        deactivated_by: null,
        is_active: true,
        remaining_seconds: 2700,
        ...over,
    };
}

function state(over: Partial<BreakGlassState> = {}): BreakGlassState {
    return {
        active: false,
        current: null,
        history: [],
        max_hours: 8,
        default_minutes: 60,
        ...over,
    };
}

beforeEach(() => {
    vi.clearAllMocks();
    _confirm.mockResolvedValue(true);
    mockFetch.mockResolvedValue(state());
});

describe('BreakGlassPanel — idle', () => {
    it('says enforcement is active when no window is open', async () => {
        render(<BreakGlassPanel />);
        expect(await screen.findByTestId('break-glass-idle')).toHaveTextContent(
            'Guardrail enforcement active',
        );
    });

    it('does not show the declare form until asked', async () => {
        render(<BreakGlassPanel />);
        await screen.findByTestId('break-glass-idle');
        expect(screen.queryByTestId('bg-form')).not.toBeInTheDocument();
    });
});

describe('BreakGlassPanel — a reason is mandatory', () => {
    it('keeps the confirm button disabled until the reason is long enough', async () => {
        render(<BreakGlassPanel />);
        fireEvent.click(await screen.findByTestId('bg-declare'));
        const confirmBtn = screen.getByTestId('bg-confirm');
        expect(confirmBtn).toBeDisabled();

        fireEvent.change(screen.getByTestId('bg-reason-input'), { target: { value: 'oops' } });
        expect(confirmBtn).toBeDisabled();

        fireEvent.change(screen.getByTestId('bg-reason-input'), {
            target: { value: 'Presidio down fleet-wide, incident INC-4471' },
        });
        expect(confirmBtn).toBeEnabled();
    });

    it('sends the reason and duration on activate', async () => {
        render(<BreakGlassPanel />);
        fireEvent.click(await screen.findByTestId('bg-declare'));
        fireEvent.change(screen.getByTestId('bg-reason-input'), {
            target: { value: 'Presidio down fleet-wide, incident INC-4471' },
        });
        fireEvent.change(screen.getByTestId('bg-minutes-input'), { target: { value: '30' } });
        fireEvent.click(screen.getByTestId('bg-confirm'));
        await waitFor(() =>
            expect(mockActivate).toHaveBeenCalledWith(
                'Presidio down fleet-wide, incident INC-4471',
                30,
            ),
        );
    });

    it('omits duration so the server default applies when left blank', async () => {
        render(<BreakGlassPanel />);
        fireEvent.click(await screen.findByTestId('bg-declare'));
        fireEvent.change(screen.getByTestId('bg-reason-input'), {
            target: { value: 'Lakera unreachable — INC-9001' },
        });
        fireEvent.click(screen.getByTestId('bg-confirm'));
        await waitFor(() =>
            expect(mockActivate).toHaveBeenCalledWith('Lakera unreachable — INC-9001', undefined),
        );
    });
});

describe('BreakGlassPanel — active state is impossible to miss', () => {
    beforeEach(() => {
        mockFetch.mockResolvedValue(state({ active: true, current: window_() }));
    });

    it('states plainly that checks are NOT blocking', async () => {
        render(<BreakGlassPanel />);
        const box = await screen.findByTestId('break-glass-active');
        expect(box).toHaveTextContent('NOT blocking');
    });

    it('shows who declared it, why, and how long is left', async () => {
        // The three properties that separate this from a hardcoded fail-open.
        // If any one stops rendering, the control loses its justification.
        render(<BreakGlassPanel />);
        await screen.findByTestId('break-glass-active');
        expect(screen.getByTestId('bg-who')).toHaveTextContent('alice');
        expect(screen.getByTestId('bg-reason')).toHaveTextContent('INC-4471');
        expect(screen.getByTestId('bg-remaining')).toHaveTextContent('45m');
    });

    it('renders an expired countdown as expired rather than a negative number', async () => {
        mockFetch.mockResolvedValue(
            state({ active: true, current: window_({ remaining_seconds: 0 }) }),
        );
        render(<BreakGlassPanel />);
        expect(await screen.findByTestId('bg-remaining')).toHaveTextContent('expired');
    });

    it('restores enforcement after confirmation', async () => {
        render(<BreakGlassPanel />);
        await screen.findByTestId('break-glass-active');
        fireEvent.click(screen.getByRole('button', { name: /restore enforcement/i }));
        await waitFor(() => expect(mockDeactivate).toHaveBeenCalled());
    });

    it('does not restore enforcement if the operator cancels', async () => {
        _confirm.mockResolvedValue(false);
        render(<BreakGlassPanel />);
        await screen.findByTestId('break-glass-active');
        fireEvent.click(screen.getByRole('button', { name: /restore enforcement/i }));
        await waitFor(() => expect(_confirm).toHaveBeenCalled());
        expect(mockDeactivate).not.toHaveBeenCalled();
    });
});

describe('BreakGlassPanel — history and failure', () => {
    it('lists past windows so a repeated outage is visible', async () => {
        mockFetch.mockResolvedValue(
            state({
                history: [
                    window_({ id: 'gbg-old', is_active: false, reason: 'earlier outage' }),
                ],
            }),
        );
        render(<BreakGlassPanel />);
        expect(await screen.findByTestId('bg-history')).toHaveTextContent('earlier outage');
    });

    it('a failed read does not render as "enforcement is on": it says the state could not be loaded', async () => {
        // The green idle state after a failed read would assert something
        // the page does not know; so would an empty panel.
        mockFetch.mockRejectedValue(new Error('control plane unreachable'));
        render(<BreakGlassPanel />);
        const alert = await screen.findByRole('alert');
        expect(alert).toHaveTextContent('Failed to load the break-glass state: control plane unreachable');
        expect(screen.queryByTestId('break-glass-idle')).not.toBeInTheDocument();
        expect(screen.queryByTestId('break-glass-active')).not.toBeInTheDocument();

        mockFetch.mockResolvedValue(state());
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        expect(await screen.findByTestId('break-glass-idle')).toBeInTheDocument();
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('a refresh that fails keeps the open window and marks it as stale, in words', async () => {
        mockFetch.mockResolvedValueOnce(state({ active: true, current: window_() }));
        mockFetch.mockRejectedValue(new Error('timeout'));
        vi.useFakeTimers({ shouldAdvanceTime: true });
        try {
            render(<BreakGlassPanel />);
            expect(await screen.findByTestId('break-glass-active')).toBeInTheDocument();
            await vi.advanceTimersByTimeAsync(30_000);
            const mark = await screen.findByRole('status');
            expect(mark).toHaveTextContent(/Could not refresh; showing what was loaded at \d\d:\d\d/);
            expect(mark).toHaveTextContent('Failed to load the break-glass state: timeout');
            expect(screen.getByTestId('break-glass-active')).toBeInTheDocument();
            expect(_toast.error).not.toHaveBeenCalled();
        } finally {
            vi.useRealTimers();
        }
    });
});
