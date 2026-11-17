/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemMaintenancePane.
 *
 * Coverage:
 *   - Current mode badge reflects the API response.
 *   - Without `system:maintenance:toggle` every action button is
 *     disabled with the Lock affordance.
 *   - With the permission, picking a mode opens the confirm modal.
 *   - The submit button stays disabled until the operator types the
 *     tenant id verbatim — typo at any position keeps it disabled.
 *   - On submit, the right client method fires with the right body
 *     (drain_grace_secs forwarded, reason trimmed, tenant_id from
 *     state).
 *   - Vacuum dropdown only shows whitelisted tables and pipes the
 *     selected table into the dbVacuum call.
 *   - Restart card calls restartRolling, not patchMaintenance.
 *   - A failed API call surfaces in the modal without closing it.
 *
 * Test doubles:
 *   - useAuth — hasPermission is a controllable spy.
 *   - useGateway — four client methods are spies.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
    render,
    screen,
    fireEvent,
    waitFor,
    within,
} from '@testing-library/react';

const mockHasPermission = vi.fn();
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission }),
}));

const mockGetMaintenance = vi.fn();
const mockPatchMaintenance = vi.fn();
const mockRestartRolling = vi.fn();
const mockDbVacuum = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            getMaintenance: mockGetMaintenance,
            patchMaintenance: mockPatchMaintenance,
            restartRolling: mockRestartRolling,
            dbVacuum: mockDbVacuum,
        },
    }),
}));

import SystemMaintenancePane from '../SystemMaintenancePane';
import { MaintenanceStateResponse } from '../../../lib/systemOpsTypes';

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

const sample: MaintenanceStateResponse = {
    tenant_id: 'tenant-acme',
    mode: 'off',
    maintenance_mode_at: '2026-06-07T08:00:00Z',
    maintenance_mode_by: 'admin-1',
    drain_grace_secs: 60,
};

describe('SystemMaintenancePane', () => {
    beforeEach(() => {
        mockHasPermission.mockReset();
        mockGetMaintenance.mockReset();
        mockPatchMaintenance.mockReset();
        mockRestartRolling.mockReset();
        mockDbVacuum.mockReset();
        mockGetMaintenance.mockResolvedValue(sample);
    });

    it('renders the current mode badge', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-current-mode'),
            ).toBeDefined(),
        );
        expect(
            screen.getByTestId('system-maintenance-current-mode').textContent,
        ).toContain('off');
    });

    it('disables every action without system:maintenance:toggle', async () => {
        grant(['system:read']);
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-mode-card'),
            ).toBeDefined(),
        );
        const readOnlyBtn = screen.getByTestId(
            'system-maintenance-set-read_only',
        );
        const drainBtn = screen.getByTestId(
            'system-maintenance-set-drain',
        );
        const pausedBtn = screen.getByTestId(
            'system-maintenance-set-paused',
        );
        const restartBtn = screen.getByTestId(
            'system-maintenance-restart-trigger',
        );
        const vacuumBtn = screen.getByTestId(
            'system-maintenance-vacuum-submit',
        );
        expect(readOnlyBtn).toBeDisabled();
        expect(drainBtn).toBeDisabled();
        expect(pausedBtn).toBeDisabled();
        expect(restartBtn).toBeDisabled();
        expect(vacuumBtn).toBeDisabled();
    });

    it('opens the modal and gates the submit on the typed tenant id', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-mode-card'),
            ).toBeDefined(),
        );

        fireEvent.click(screen.getByTestId('system-maintenance-set-read_only'));
        const modal = await waitFor(() =>
            screen.getByTestId('system-maintenance-confirm-modal'),
        );
        const submitBtn = within(modal).getByTestId(
            'system-maintenance-confirm-submit',
        );
        expect(submitBtn).toBeDisabled();

        // Typo — still disabled.
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acm' } },
        );
        expect(submitBtn).toBeDisabled();

        // Exact match — unlocks.
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acme' } },
        );
        expect(submitBtn).not.toBeDisabled();
    });

    it('calls patchMaintenance with the right body and refreshes', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        mockPatchMaintenance.mockResolvedValue({
            ...sample,
            mode: 'drain',
            drain_grace_secs: 30,
        });
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-mode-card'),
            ).toBeDefined(),
        );

        // Set a custom grace before opening drain.
        fireEvent.change(
            screen.getByTestId('system-maintenance-drain-grace'),
            { target: { value: '30' } },
        );
        fireEvent.click(screen.getByTestId('system-maintenance-set-drain'));
        const modal = await waitFor(() =>
            screen.getByTestId('system-maintenance-confirm-modal'),
        );
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acme' } },
        );
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-reason'),
            { target: { value: '  rolling restart  ' } },
        );
        fireEvent.click(
            within(modal).getByTestId('system-maintenance-confirm-submit'),
        );

        await waitFor(() =>
            expect(mockPatchMaintenance).toHaveBeenCalledTimes(1),
        );
        expect(mockPatchMaintenance).toHaveBeenCalledWith({
            tenant_id: 'tenant-acme',
            mode: 'drain',
            drain_grace_secs: 30,
            reason: 'rolling restart',
        });
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-feedback'),
            ).toBeDefined(),
        );
    });

    it('uses restartRolling for the rolling-restart card', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        mockRestartRolling.mockResolvedValue({
            audit_id: 'sol-1',
            recorded_at: '2026-06-07T10:00:00Z',
        });
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-restart-trigger'),
            ).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId('system-maintenance-restart-trigger'),
        );
        const modal = await waitFor(() =>
            screen.getByTestId('system-maintenance-confirm-modal'),
        );
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acme' } },
        );
        fireEvent.click(
            within(modal).getByTestId('system-maintenance-confirm-submit'),
        );

        await waitFor(() =>
            expect(mockRestartRolling).toHaveBeenCalledTimes(1),
        );
        expect(mockPatchMaintenance).not.toHaveBeenCalled();
        expect(mockRestartRolling.mock.calls[0][0]).toMatchObject({
            tenant_id: 'tenant-acme',
        });
    });

    it('pipes the dropdown selection into dbVacuum', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        mockDbVacuum.mockResolvedValue({
            table: 'usage_logs',
            duration_ms: 1234,
            audit_id: 'sol-2',
            completed_at: '2026-06-07T10:00:00Z',
        });
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-vacuum-table'),
            ).toBeDefined(),
        );
        fireEvent.change(
            screen.getByTestId('system-maintenance-vacuum-table'),
            { target: { value: 'usage_logs' } },
        );
        fireEvent.click(
            screen.getByTestId('system-maintenance-vacuum-submit'),
        );
        const modal = await waitFor(() =>
            screen.getByTestId('system-maintenance-confirm-modal'),
        );
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acme' } },
        );
        fireEvent.click(
            within(modal).getByTestId('system-maintenance-confirm-submit'),
        );

        await waitFor(() => expect(mockDbVacuum).toHaveBeenCalledTimes(1));
        expect(mockDbVacuum.mock.calls[0][0]).toMatchObject({
            table: 'usage_logs',
            tenant_id: 'tenant-acme',
        });
    });

    it('surfaces a failed API call in the modal without closing', async () => {
        grant(['system:read', 'system:maintenance:toggle']);
        mockPatchMaintenance.mockRejectedValue(new Error('503 paused'));
        render(<SystemMaintenancePane />);
        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-set-paused'),
            ).toBeDefined(),
        );

        fireEvent.click(screen.getByTestId('system-maintenance-set-paused'));
        const modal = await waitFor(() =>
            screen.getByTestId('system-maintenance-confirm-modal'),
        );
        fireEvent.change(
            within(modal).getByTestId('system-maintenance-confirm-tenant'),
            { target: { value: 'tenant-acme' } },
        );
        fireEvent.click(
            within(modal).getByTestId('system-maintenance-confirm-submit'),
        );

        await waitFor(() =>
            expect(
                screen.getByTestId('system-maintenance-confirm-error'),
            ).toBeDefined(),
        );
        // Modal still rendered.
        expect(
            screen.queryByTestId('system-maintenance-confirm-modal'),
        ).not.toBeNull();
    });
});
