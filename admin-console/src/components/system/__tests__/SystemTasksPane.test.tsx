/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemTasksPane.
 *
 * Coverage:
 *   - Lists every scheduler returned by the API with its state badge,
 *     last-run timestamp, and pause metadata.
 *   - Trigger/Pause/Resume buttons render per row with the right
 *     visibility based on the row's `paused` flag (a paused row
 *     shows Resume, a running row shows Pause).
 *   - Without `system:operate` every action button is disabled and
 *     tooltips advertise the required permission.
 *   - With `system:operate`, clicking an action opens a confirm
 *     modal that includes a reason textarea. Submitting calls the
 *     correct client method and refreshes the list.
 *   - A failed API call surfaces in the modal without closing it,
 *     so the operator can retry without re-typing the reason.
 *   - Cancel closes the modal without firing any client call.
 *   - The empty-list case renders a friendly placeholder rather
 *     than an empty <table>.
 *
 * Test doubles:
 *   - useAuth is mocked so `hasPermission` is a controllable spy.
 *   - useGateway is mocked so the four client methods are spies
 *     that return controllable Promises.
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
// The administrator as `/v1/admin/users/me` answers: whether it operates the installation, and whether the installation has a system administrator.
let mockUser: { is_installation_operator?: boolean; installation_has_system_admin?: boolean } | null = null;
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: mockHasPermission, user: mockUser }),
}));

const mockListSystemTasks = vi.fn();
const mockTriggerSystemTask = vi.fn();
const mockPauseSystemTask = vi.fn();
const mockResumeSystemTask = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listSystemTasks: mockListSystemTasks,
            triggerSystemTask: mockTriggerSystemTask,
            pauseSystemTask: mockPauseSystemTask,
            resumeSystemTask: mockResumeSystemTask,
        },
    }),
}));

import SystemTasksPane from '../SystemTasksPane';
import { SchedulerListResponse } from '../../../lib/systemOpsTypes';

const grant = (perms: string[]) =>
    mockHasPermission.mockImplementation((p: string) => perms.includes(p));

const sampleResponse: SchedulerListResponse = {
    items: [
        {
            scheduler_name: 'audit_export',
            paused: false,
            paused_at: null,
            paused_by: null,
            resumed_at: null,
            resumed_by: null,
            last_run_at: '2026-06-07T10:00:00Z',
            last_run_result: null,
            next_run_eta: null,
            state: 'running',
        },
        {
            scheduler_name: 'kb_uploader',
            paused: true,
            paused_at: '2026-06-07T09:00:00Z',
            paused_by: 'admin-1',
            resumed_at: null,
            resumed_by: null,
            last_run_at: '2026-06-07T08:00:00Z',
            last_run_result: null,
            next_run_eta: null,
            state: 'paused',
        },
    ],
};

describe('SystemTasksPane', () => {
    beforeEach(() => {
        mockHasPermission.mockReset();
        mockUser = { is_installation_operator: true, installation_has_system_admin: false };
        mockListSystemTasks.mockReset();
        mockTriggerSystemTask.mockReset();
        mockPauseSystemTask.mockReset();
        mockResumeSystemTask.mockReset();
        mockListSystemTasks.mockResolvedValue(sampleResponse);
    });

    it("has the columns the contract gives a scheduler, and a paused scheduler's reason", async () => {
        grant(['system:read']);
        mockListSystemTasks.mockResolvedValue({
            items: sampleResponse.items.map((item) => (item.paused ? { ...item, pause_reason: 'Vector store migration' } : { ...item, pause_reason: null })),
        });
        render(<SystemTasksPane />);
        const table = await screen.findByTestId('system-tasks-table');
        const headers = Array.from(table.querySelectorAll('th')).map((th) => th.textContent);
        expect(headers).toEqual(['Scheduler', 'State', 'Last run', 'Paused at', 'Paused by', 'Reason', 'Actions']);
        const row = await screen.findByTestId('system-tasks-row-kb_uploader');
        expect(row.querySelectorAll('td')).toHaveLength(7);
        expect(row).toHaveTextContent('Vector store migration');
    });

    it('renders one row per scheduler with state badge and metadata', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemTasksPane />);

        // First fetch lands.
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        const auditRow = screen.getByTestId(
            'system-tasks-row-audit_export',
        );
        const kbRow = screen.getByTestId('system-tasks-row-kb_uploader');
        expect(auditRow).toBeDefined();
        expect(kbRow).toBeDefined();

        // State badge text matches the wire status.
        expect(
            within(auditRow).getByTestId('system-tasks-state-running'),
        ).toBeDefined();
        expect(
            within(kbRow).getByTestId('system-tasks-state-paused'),
        ).toBeDefined();

        // Running row exposes Pause + Trigger, NOT Resume.
        expect(
            within(auditRow).getByTestId(
                'system-tasks-action-pause-audit_export',
            ),
        ).toBeDefined();
        expect(
            within(auditRow).getByTestId(
                'system-tasks-action-trigger-audit_export',
            ),
        ).toBeDefined();
        expect(
            within(auditRow).queryByTestId(
                'system-tasks-action-resume-audit_export',
            ),
        ).toBeNull();

        // Paused row exposes Resume + Trigger, NOT Pause.
        expect(
            within(kbRow).getByTestId(
                'system-tasks-action-resume-kb_uploader',
            ),
        ).toBeDefined();
        expect(
            within(kbRow).queryByTestId(
                'system-tasks-action-pause-kb_uploader',
            ),
        ).toBeNull();
    });

    it('disables every action button when the user lacks system:operate', async () => {
        grant(['system:read']); // read-only auditor
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        const trigger = screen.getByTestId(
            'system-tasks-action-trigger-audit_export',
        );
        const pause = screen.getByTestId(
            'system-tasks-action-pause-audit_export',
        );
        expect(trigger.hasAttribute('disabled')).toBe(true);
        expect(pause.hasAttribute('disabled')).toBe(true);
        expect(trigger.getAttribute('title')).toContain(
            'system:operate',
        );
    });

    it('opens the confirm modal on Trigger click and submits with the typed reason', async () => {
        grant(['system:read', 'system:operate']);
        mockTriggerSystemTask.mockResolvedValue({});
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(
                'system-tasks-action-trigger-audit_export',
            ),
        );

        const modal = screen.getByTestId('system-tasks-confirm-modal');
        expect(modal).toBeDefined();
        // Modal headline names both the action and the scheduler so
        // the operator can't fat-finger the wrong one.
        expect(modal.textContent).toContain('Trigger now');
        expect(modal.textContent).toContain('audit_export');

        const reason = screen.getByTestId(
            'system-tasks-confirm-reason',
        );
        fireEvent.change(reason, {
            target: { value: 'manual backfill after outage' },
        });

        fireEvent.click(
            screen.getByTestId('system-tasks-confirm-submit'),
        );

        await waitFor(() =>
            expect(mockTriggerSystemTask).toHaveBeenCalledWith({
                scheduler_name: 'audit_export',
                reason: 'manual backfill after outage',
            }),
        );
        // After success, the modal closes and the list re-fetches.
        await waitFor(() =>
            expect(
                screen.queryByTestId('system-tasks-confirm-modal'),
            ).toBeNull(),
        );
        // Once on mount + once after action.
        expect(mockListSystemTasks).toHaveBeenCalledTimes(2);
    });

    it('routes Resume click to resumeSystemTask', async () => {
        grant(['system:read', 'system:operate']);
        mockResumeSystemTask.mockResolvedValue({});
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(
                'system-tasks-action-resume-kb_uploader',
            ),
        );
        fireEvent.click(
            screen.getByTestId('system-tasks-confirm-submit'),
        );

        await waitFor(() =>
            expect(mockResumeSystemTask).toHaveBeenCalledWith({
                scheduler_name: 'kb_uploader',
            }),
        );
        expect(mockPauseSystemTask).not.toHaveBeenCalled();
        expect(mockTriggerSystemTask).not.toHaveBeenCalled();
    });

    it('keeps the modal open and surfaces the error when the API rejects', async () => {
        grant(['system:read', 'system:operate']);
        mockPauseSystemTask.mockRejectedValue(new Error('boom'));
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(
                'system-tasks-action-pause-audit_export',
            ),
        );
        fireEvent.click(
            screen.getByTestId('system-tasks-confirm-submit'),
        );

        await waitFor(() =>
            expect(
                screen.getByTestId('system-tasks-confirm-error'),
            ).toBeDefined(),
        );
        // Modal still mounted so the operator can retry.
        expect(
            screen.getByTestId('system-tasks-confirm-modal'),
        ).toBeDefined();
    });

    it('says in a plain sentence that the Control Plane did not accept the request, and that nothing changed', async () => {
        grant(['system:read', 'system:operate']);
        // What the API client throws for a 422: the status, and the
        // validation errors joined into the message.
        mockPauseSystemTask.mockRejectedValue(
            Object.assign(new Error('body → scheduler_name: Extra inputs are not permitted'), { status: 422 }),
        );
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        fireEvent.click(screen.getByTestId('system-tasks-action-pause-audit_export'));
        fireEvent.click(screen.getByTestId('system-tasks-confirm-submit'));

        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-confirm-error').textContent).toBe(
                'The Control Plane did not accept this request, so audit_export was not changed. '
                + 'This is a fault in the Control Plane, not in what you entered: report it to whoever runs this installation. '
                + 'What it answered: body → scheduler_name: Extra inputs are not permitted',
            ),
        );
        expect(screen.getByTestId('system-tasks-confirm-modal')).toBeDefined();
    });

    it('where the installation has a system administrator, a tenant administrator is offered no action, whatever its role holds', async () => {
        grant(['system:read', 'system:operate']);
        mockUser = { is_installation_operator: false, installation_has_system_admin: true };
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        for (const id of ['system-tasks-action-trigger-audit_export', 'system-tasks-action-pause-audit_export', 'system-tasks-action-resume-kb_uploader']) {
            const button = screen.getByTestId(id);
            expect(button.hasAttribute('disabled')).toBe(true);
            expect(button.getAttribute('title')).toBe('For the system administrator: a scheduler runs once for the whole installation');
        }
        expect(screen.getByTestId('system-tasks-operator-note').textContent).toBe(
            'A scheduler runs once for the whole installation, so triggering, pausing and resuming one is for the system administrator.',
        );
    });

    it('the system administrator is offered the actions', async () => {
        grant(['system:read', 'system:operate']);
        mockUser = { is_installation_operator: true, installation_has_system_admin: true };
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        expect(screen.getByTestId('system-tasks-action-trigger-audit_export').hasAttribute('disabled')).toBe(false);
        expect(screen.queryByTestId('system-tasks-operator-note')).toBeNull();
    });

    it('in an installation with no system administrator, the tenant administrator who holds system:operate is offered the actions', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        expect(screen.getByTestId('system-tasks-action-trigger-audit_export').hasAttribute('disabled')).toBe(false);
        expect(screen.queryByTestId('system-tasks-operator-note')).toBeNull();
    });

    it('says so when the session does not say who operates the installation, and offers no action', async () => {
        grant(['system:read', 'system:operate']);
        mockUser = {};
        render(<SystemTasksPane />);
        await waitFor(() => expect(screen.getByTestId('system-tasks-table')).toBeDefined());
        expect(screen.getByTestId('system-tasks-action-trigger-audit_export').hasAttribute('disabled')).toBe(true);
    });

    it('the refusal of an administrator who is not the installation\'s operator is shown as the Control Plane words it', async () => {
        grant(['system:read', 'system:operate']);
        mockPauseSystemTask.mockRejectedValue(Object.assign(new Error('System administrator access required: this acts on the whole installation'), { status: 403 }));
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        fireEvent.click(screen.getByTestId('system-tasks-action-pause-audit_export'));
        fireEvent.click(screen.getByTestId('system-tasks-confirm-submit'));
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-confirm-error').textContent).toBe('System administrator access required: this acts on the whole installation'),
        );
        expect(mockPauseSystemTask).toHaveBeenCalledWith({ scheduler_name: 'audit_export' });
    });

    it('any other refusal is shown as the Control Plane words it', async () => {
        grant(['system:read', 'system:operate']);
        mockPauseSystemTask.mockRejectedValue(Object.assign(new Error("Unknown scheduler 'audit_export'"), { status: 404 }));
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );
        fireEvent.click(screen.getByTestId('system-tasks-action-pause-audit_export'));
        fireEvent.click(screen.getByTestId('system-tasks-confirm-submit'));
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-confirm-error').textContent).toBe("Unknown scheduler 'audit_export'"),
        );
    });

    it('cancel closes the modal without invoking any client method', async () => {
        grant(['system:read', 'system:operate']);
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-table')).toBeDefined(),
        );

        fireEvent.click(
            screen.getByTestId(
                'system-tasks-action-trigger-audit_export',
            ),
        );
        expect(
            screen.getByTestId('system-tasks-confirm-modal'),
        ).toBeDefined();

        fireEvent.click(
            screen.getByTestId('system-tasks-confirm-cancel'),
        );
        await waitFor(() =>
            expect(
                screen.queryByTestId('system-tasks-confirm-modal'),
            ).toBeNull(),
        );
        expect(mockTriggerSystemTask).not.toHaveBeenCalled();
    });

    it('renders an empty-state row when the API returns no schedulers', async () => {
        grant(['system:read', 'system:operate']);
        mockListSystemTasks.mockResolvedValue({ items: [] });
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-empty')).toBeDefined(),
        );
    });

    it('surfaces a top-level error when the initial fetch fails', async () => {
        grant(['system:read']);
        mockListSystemTasks.mockRejectedValue(new Error('network down'));
        render(<SystemTasksPane />);
        await waitFor(() =>
            expect(screen.getByTestId('system-tasks-error')).toBeDefined(),
        );
        expect(
            screen.getByTestId('system-tasks-error').textContent,
        ).toContain('network down');
    });
});
