/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Incident windows. Every window says who declared it,
 * why, its category, how many runs it excluded and whether the declaration
 * is sealed; declare / edit / withdraw reach the right route with the
 * right scope, and a write that saved but did not seal says so.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const mockSystemWindows = vi.fn();
const mockTenantWindows = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockDelete = vi.fn();
const mockFetchAISystems = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemIncidentWindows: mockSystemWindows,
        fetchIncidentWindows: mockTenantWindows,
        createIncidentWindow: mockCreate,
        updateIncidentWindow: mockUpdate,
        deleteIncidentWindow: mockDelete,
        fetchAISystems: mockFetchAISystems,
    }),
}));

import IncidentWindowsPanel from '../IncidentWindowsPanel';
import IncidentWindowsTab from '../../governance/IncidentWindowsTab';
import type { IncidentWindow } from '../../../lib/driftResolutionTypes';
import { toLocalInputValue } from '../../../lib/driftResolution';

const WINDOW: IncidentWindow = {
    id: 'iw-1',
    ai_system_id: 'sys-1',
    ai_system_name: 'Loan pre-screening',
    tenant_wide: false,
    starts_at: '2026-09-27T21:00:00Z',
    ends_at: '2026-09-28T06:40:00Z',
    cause_category: 'provider_outage',
    reason: 'provider credits ran out',
    drift_event_id: 'drift-2',
    declared_by: 'alice',
    declared_at: '2026-09-28T06:45:00Z',
    record_id: 'rec-9',
    sealed_at: '2026-09-28T06:45:01Z',
    runs_excluded: 14,
};

const SEALED = { sealed: true, record_id: 'rec-10' };

describe('IncidentWindowsPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockSystemWindows.mockResolvedValue({ windows: [WINDOW], total: 1 });
        mockTenantWindows.mockResolvedValue({ windows: [{ ...WINDOW, ai_system_id: null, ai_system_name: null, tenant_wide: true }], total: 1 });
        mockCreate.mockResolvedValue({ ...WINDOW, id: 'iw-2', seal: SEALED });
        mockUpdate.mockResolvedValue({ ...WINDOW, seal: SEALED });
        mockDelete.mockResolvedValue({ id: 'iw-1', deleted: true, seal: SEALED });
        mockFetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'loan', display_name: 'Loan pre-screening' }] });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('shows who declared it, why, the category, runs excluded and the sealed record', async () => {
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        const card = await screen.findByTestId('iw-iw-1');
        expect(mockSystemWindows).toHaveBeenCalledWith('sys-1');
        expect(card).toHaveTextContent('Provider outage');
        expect(within(card).getByTestId('iw-reason-text')).toHaveTextContent('provider credits ran out');
        expect(within(card).getByTestId('iw-declared')).toHaveTextContent('declared by a cause-fixed resolution by alice');
        expect(within(card).getByTestId('iw-runs-excluded')).toHaveTextContent('14 run(s) excluded from judgement');
        expect(within(card).getByTestId('iw-sealed')).toHaveTextContent('sealed record rec-9');
    });

    it('an unsealed window says so', async () => {
        mockSystemWindows.mockResolvedValue({ windows: [{ ...WINDOW, record_id: null }], total: 1 });
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('iw-sealed')).toHaveTextContent('not sealed');
    });

    it('declares a window for the system on the system route', async () => {
        const onChanged = vi.fn();
        render(<IncidentWindowsPanel aiSystemId="sys-1" onChanged={onChanged} />);
        fireEvent.click(await screen.findByTestId('iw-declare'));
        expect(screen.getByTestId('iw-save')).toBeDisabled();
        fireEvent.change(screen.getByTestId('iw-start'), { target: { value: toLocalInputValue('2026-09-29T02:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-end'), { target: { value: toLocalInputValue('2026-09-29T04:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-reason'), { target: { value: 'planned migration' } });
        fireEvent.click(screen.getByTestId('iw-save'));
        await waitFor(() =>
            expect(mockCreate).toHaveBeenCalledWith(
                {
                    ai_system_id: 'sys-1',
                    starts_at: '2026-09-29T02:00:00.000Z',
                    ends_at: '2026-09-29T04:00:00.000Z',
                    cause_category: 'maintenance',
                    reason: 'planned migration',
                    resource_type: null,
                    resource_id: null,
                },
                'sys-1',
            ),
        );
        await waitFor(() => expect(mockSystemWindows).toHaveBeenCalledTimes(2));
        expect(onChanged).toHaveBeenCalled();
    });

    it('a resource type needs its id — the control plane takes them as a pair', async () => {
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('iw-declare'));
        fireEvent.change(screen.getByTestId('iw-start'), { target: { value: toLocalInputValue('2026-09-29T02:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-end'), { target: { value: toLocalInputValue('2026-09-29T04:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-reason'), { target: { value: 'model swap' } });
        fireEvent.change(screen.getByTestId('iw-resource-type'), { target: { value: 'model' } });
        expect(screen.getByTestId('iw-save')).toBeDisabled();
        expect(screen.getByTestId('iw-blocked')).toHaveTextContent(/go together/);
        fireEvent.change(screen.getByTestId('iw-resource-id'), { target: { value: 'gpt-5.2' } });
        fireEvent.click(screen.getByTestId('iw-save'));
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][0]).toMatchObject({ resource_type: 'model', resource_id: 'gpt-5.2' });
    });

    it('edits a window by id with the new bounds and reason', async () => {
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('iw-edit'));
        expect(screen.getByTestId('iw-start')).toHaveValue(toLocalInputValue(WINDOW.starts_at));
        fireEvent.change(screen.getByTestId('iw-end'), { target: { value: toLocalInputValue('2026-09-28T07:10:00Z') } });
        fireEvent.change(screen.getByTestId('iw-reason'), { target: { value: 'credits restored 07:10' } });
        fireEvent.click(screen.getByTestId('iw-save'));
        await waitFor(() =>
            expect(mockUpdate).toHaveBeenCalledWith('iw-1', {
                starts_at: '2026-09-27T21:00:00.000Z',
                ends_at: '2026-09-28T07:10:00.000Z',
                cause_category: 'provider_outage',
                reason: 'credits restored 07:10',
                resource_type: null,
                resource_id: null,
            }),
        );
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('withdraws only after confirmation', async () => {
        const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
        vi.stubGlobal('confirm', confirm);
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('iw-delete'));
        expect(mockDelete).not.toHaveBeenCalled();
        fireEvent.click(screen.getByTestId('iw-delete'));
        await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('iw-1'));
        expect(confirm).toHaveBeenCalledTimes(2);
    });

    it('a write that saved but did not seal says so', async () => {
        mockUpdate.mockResolvedValue({ ...WINDOW, seal: { sealed: false, seal_error: 'evidence_disabled' } });
        render(<IncidentWindowsPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('iw-edit'));
        fireEvent.click(screen.getByTestId('iw-save'));
        expect(await screen.findByTestId('iw-seal-notice')).toHaveTextContent('not sealed (evidence_disabled)');
    });

    it('the Governance page says when the systems a window can be scoped to could not be loaded', async () => {
        mockFetchAISystems.mockRejectedValueOnce(new Error('systems down'));
        render(<IncidentWindowsTab />);
        await screen.findByTestId('iw-iw-1');
        const notice = await screen.findByTestId('ai-system-names-failed');
        expect(notice).toHaveTextContent('Failed to load the AI Systems a window can be scoped to: systems down');

        mockFetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'loan', display_name: 'Loan pre-screening' }] });
        fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(screen.queryByTestId('ai-system-names-failed')).toBeNull());
        fireEvent.click(screen.getByTestId('iw-declare'));
        expect(await screen.findByRole('option', { name: 'Loan pre-screening' })).toBeInTheDocument();
    });

    it('the Governance page lists every window in the tenant and declares tenant-wide by default', async () => {
        render(<IncidentWindowsTab />);
        const card = await screen.findByTestId('iw-iw-1');
        expect(mockTenantWindows).toHaveBeenCalled();
        expect(mockSystemWindows).not.toHaveBeenCalled();
        expect(within(card).getByTestId('iw-scope-text')).toHaveTextContent('Every AI System in the tenant');
        fireEvent.click(screen.getByTestId('iw-declare'));
        expect(await screen.findByRole('option', { name: 'Loan pre-screening' })).toBeInTheDocument();
        fireEvent.change(screen.getByTestId('iw-start'), { target: { value: toLocalInputValue('2026-09-29T02:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-end'), { target: { value: toLocalInputValue('2026-09-29T04:00:00Z') } });
        fireEvent.change(screen.getByTestId('iw-reason'), { target: { value: 'provider outage' } });
        fireEvent.click(screen.getByTestId('iw-save'));
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        const [body, scope] = mockCreate.mock.calls[0] as [{ ai_system_id: string | null }, string | undefined];
        expect(body.ai_system_id).toBeNull();
        expect(scope).toBeUndefined();
    });
});
