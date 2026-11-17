/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Video generation jobs: query, sort, cancel, delete, expand.
 *
 * Pinned: filters and sort reach the server (the list is paged there, so a
 * client-side view would be wrong), Cancel is offered only on running jobs and
 * Delete only on finished ones, and the auto-refresh stops once nothing is in
 * flight. A failed detail fetch also left a row saying "Loading detail…" forever.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: toast }));

const gatewayClient = {
    queryVideoGenJobs: vi.fn(),
    getVideoGenStatistics: vi.fn(),
    cancelVideoGenJob: vi.fn(),
    deleteVideoGenJob: vi.fn(),
    getVideoGenJobDetail: vi.fn(),
};
const gateway = { gatewayClient };
const confirm = vi.fn<(o: { title: string }) => Promise<boolean>>();

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import VideoGenJobsTab from '../VideoGenJobsTab';

const RUNNING = {
    id: 'job-running',
    model_name: 'sora-2',
    status: 'in_progress',
    duration_seconds: 8,
    cost_usd: 1.5,
    created_at: '2026-07-01T00:00:00Z',
};
const DONE = {
    id: 'job-done',
    model_name: null,
    model_id: 'model-id-1',
    status: 'succeeded',
    duration_seconds: null,
    cost_usd: undefined,
    created_at: '2026-07-01T00:00:00Z',
    error_preview: 'partial error',
};
const STATS = { total_jobs: 12, succeeded_count: 9, in_flight_count: 1, failed_count: 2, total_cost_usd: 3.25 };
const PAGE = { page: 1, page_size: 20, total: 2, total_pages: 1 };

function lastQuery(): unknown {
    const calls = gatewayClient.queryVideoGenJobs.mock.calls;
    return calls[calls.length - 1][0];
}

function rowOf(id: string): HTMLElement {
    const row = screen.getByText(id).closest('tr');
    if (!row) throw new Error(`no row for ${id}`);
    return row;
}

function sortHeader(name: string): HTMLElement {
    const head = document.querySelector('thead');
    if (!(head instanceof HTMLElement)) throw new Error('no table head');
    const th = within(head).getByText(name).closest('th');
    if (!th) throw new Error(`no header ${name}`);
    return th;
}

async function load() {
    render(<VideoGenJobsTab />);
    await screen.findByText('job-running');
}

describe('VideoGenJobsTab', () => {
    let refreshTick: (() => void) | null;

    beforeEach(() => {
        vi.clearAllMocks();
        confirm.mockResolvedValue(true);
        gatewayClient.queryVideoGenJobs.mockResolvedValue({ jobs: [RUNNING, DONE], pagination: PAGE });
        gatewayClient.getVideoGenStatistics.mockResolvedValue(STATS);
        gatewayClient.cancelVideoGenJob.mockResolvedValue({});
        gatewayClient.deleteVideoGenJob.mockResolvedValue({});
        gatewayClient.getVideoGenJobDetail.mockResolvedValue({
            job: {
                id: 'job-running',
                prompt: 'a cat on a skateboard',
                resolution: '1080p',
                duration_seconds: 8,
                provider_request_id: 'req-9',
                output_file_id: 'file-7',
                actor_id: 'ann',
                error: 'upstream exploded',
            },
        });

        refreshTick = null;
        const realSetInterval = globalThis.setInterval;
        vi.spyOn(globalThis, 'setInterval').mockImplementation(((fn: () => void, ms?: number) => {
            if (ms === 30_000) {
                refreshTick = fn;
                return 4242;
            }
            return realSetInterval(fn, ms);
        }) as unknown as typeof setInterval);
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('queries the last 24 hours, newest first, and shows the stats', async () => {
        await load();
        expect(lastQuery()).toEqual({ page: 1, page_size: 20, sort_by: 'created_at', sort_order: 'desc', hours_ago: 24 });
        expect(gatewayClient.getVideoGenStatistics).toHaveBeenCalledWith({ hours_ago: 24 });
        await screen.findByText('Total');
        expect(screen.getByText('12')).toBeTruthy();
        expect(screen.getByText('$3.2500')).toBeTruthy();
    });

    it('renders a row, falling back to the model id and dashes', async () => {
        await load();
        const running = within(rowOf('job-running'));
        expect(running.getByText('sora-2')).toBeTruthy();
        expect(running.getByText('8s')).toBeTruthy();
        expect(running.getByText('$1.5000')).toBeTruthy();

        const done = within(rowOf('job-done'));
        expect(done.getByText('model-id-1')).toBeTruthy();
        expect(done.getAllByText('—')).toHaveLength(2);
        expect(done.getByLabelText('partial error')).toBeTruthy();
    });

    it('offers Cancel on a running job and Delete on a finished one, never both', async () => {
        await load();
        const running = within(rowOf('job-running'));
        expect(running.getByTitle('Cancel job')).toBeTruthy();
        expect(running.queryByTitle('Delete row')).toBeNull();

        const done = within(rowOf('job-done'));
        expect(done.getByTitle('Delete row')).toBeTruthy();
        expect(done.queryByTitle('Cancel job')).toBeNull();
    });

    it('shows the empty state', async () => {
        gatewayClient.queryVideoGenJobs.mockResolvedValue({ jobs: [], pagination: PAGE });
        render(<VideoGenJobsTab />);
        expect(await screen.findByText('No video generation jobs in the selected range.')).toBeTruthy();
    });

    it('reports a load failure, and keeps going without stats', async () => {
        gatewayClient.queryVideoGenJobs.mockRejectedValue(new Error('gateway down'));
        gatewayClient.getVideoGenStatistics.mockRejectedValue(new Error('no stats'));
        render(<VideoGenJobsTab />);
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to load video jobs: gateway down'));
        expect(screen.queryByText('Total')).toBeNull();
        expect(await screen.findByText('No video generation jobs in the selected range.')).toBeTruthy();
    });

    it('refreshes', async () => {
        await load();
        fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
        await waitFor(() => expect(gatewayClient.queryVideoGenJobs).toHaveBeenCalledTimes(2));
        expect(gatewayClient.getVideoGenStatistics).toHaveBeenCalledTimes(2);
    });

    describe('filters and sort', () => {
        it('sends a status and resets to the first page', async () => {
            await load();
            const [status] = screen.getAllByRole('combobox');
            fireEvent.change(status, { target: { value: 'failed' } });
            await waitFor(() => expect(lastQuery()).toMatchObject({ status: ['failed'], page: 1 }));
        });

        it('sends the window to list and stats, and neither bound for all time', async () => {
            await load();
            const [, hours] = screen.getAllByRole('combobox');

            fireEvent.change(hours, { target: { value: '168' } });
            await waitFor(() => expect(lastQuery()).toMatchObject({ hours_ago: 168 }));
            expect(gatewayClient.getVideoGenStatistics).toHaveBeenLastCalledWith({ hours_ago: 168 });

            fireEvent.change(hours, { target: { value: '' } });
            await waitFor(() => expect(lastQuery()).not.toHaveProperty('hours_ago'));
            expect(gatewayClient.getVideoGenStatistics).toHaveBeenLastCalledWith(undefined);
        });

        it('flips the order on the same column and starts descending on a new one', async () => {
            await load();
            fireEvent.click(sortHeader('Created'));
            await waitFor(() => expect(lastQuery()).toMatchObject({ sort_by: 'created_at', sort_order: 'asc' }));

            fireEvent.click(sortHeader('Cost'));
            await waitFor(() => expect(lastQuery()).toMatchObject({ sort_by: 'cost_usd', sort_order: 'desc' }));

            fireEvent.click(sortHeader('Dur'));
            await waitFor(() => expect(lastQuery()).toMatchObject({ sort_by: 'duration_seconds', sort_order: 'desc' }));
        });
    });

    describe('paging', () => {
        it('moves between pages and stops at the ends', async () => {
            gatewayClient.queryVideoGenJobs.mockImplementation((q: { page: number }) =>
                Promise.resolve({
                    jobs: [DONE],
                    pagination: { page: q.page, page_size: 20, total: 40, total_pages: 2 },
                }),
            );
            render(<VideoGenJobsTab />);
            await screen.findByText('Page 1 / 2 · 40 total');
            const span = screen.getByText('Page 1 / 2 · 40 total');
            const [prev, next] = Array.from(span.parentElement?.querySelectorAll('button') ?? []);
            expect(prev.disabled).toBe(true);

            fireEvent.click(next);
            await screen.findByText('Page 2 / 2 · 40 total');
            expect(lastQuery()).toMatchObject({ page: 2 });
            expect(next.disabled).toBe(true);

            fireEvent.click(prev);
            await screen.findByText('Page 1 / 2 · 40 total');
            expect(lastQuery()).toMatchObject({ page: 1 });
        });
    });

    describe('cancel and delete', () => {
        it('warns about provider billing, cancels, and reloads', async () => {
            await load();
            fireEvent.click(within(rowOf('job-running')).getByTitle('Cancel job'));

            await waitFor(() => expect(gatewayClient.cancelVideoGenJob).toHaveBeenCalledWith('job-running'));
            expect(confirm.mock.calls[0][0].title).toBe('Cancel video generation job?');
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Job cancelled'));
            await waitFor(() => expect(gatewayClient.getVideoGenStatistics).toHaveBeenCalledTimes(2));
            expect(gatewayClient.queryVideoGenJobs).toHaveBeenCalledTimes(2);
        });

        it('does not cancel when declined, and does not expand the row', async () => {
            confirm.mockResolvedValue(false);
            await load();
            fireEvent.click(within(rowOf('job-running')).getByTitle('Cancel job'));
            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(gatewayClient.cancelVideoGenJob).not.toHaveBeenCalled();
            expect(gatewayClient.getVideoGenJobDetail).not.toHaveBeenCalled();
        });

        it('reports a failed cancel', async () => {
            gatewayClient.cancelVideoGenJob.mockRejectedValue(new Error('already done'));
            await load();
            fireEvent.click(within(rowOf('job-running')).getByTitle('Cancel job'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Cancel failed: already done'));
        });

        it('deletes once confirmed and reloads', async () => {
            await load();
            fireEvent.click(within(rowOf('job-done')).getByTitle('Delete row'));
            await waitFor(() => expect(gatewayClient.deleteVideoGenJob).toHaveBeenCalledWith('job-done'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Job deleted'));
            await waitFor(() => expect(gatewayClient.queryVideoGenJobs).toHaveBeenCalledTimes(2));
        });

        it('does not delete when declined, and reports a failed delete', async () => {
            confirm.mockResolvedValueOnce(false);
            await load();
            fireEvent.click(within(rowOf('job-done')).getByTitle('Delete row'));
            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(gatewayClient.deleteVideoGenJob).not.toHaveBeenCalled();

            gatewayClient.deleteVideoGenJob.mockRejectedValue(new Error('locked'));
            fireEvent.click(within(rowOf('job-done')).getByTitle('Delete row'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Delete failed: locked'));
        });
    });

    describe('detail row', () => {
        it('fetches on expand, shows the full detail, collapses, and uses the cache after', async () => {
            await load();
            fireEvent.click(screen.getByText('job-running'));

            expect(await screen.findByText(/a cat on a skateboard/)).toBeTruthy();
            expect(gatewayClient.getVideoGenJobDetail).toHaveBeenCalledWith('job-running');
            expect(screen.getByText('req-9')).toBeTruthy();
            expect(screen.getByText('file-7')).toBeTruthy();
            expect(screen.getByText('upstream exploded')).toBeTruthy();

            fireEvent.click(screen.getByText('job-running'));
            expect(screen.queryByText(/a cat on a skateboard/)).toBeNull();

            fireEvent.click(screen.getByText('job-running'));
            expect(screen.getByText(/a cat on a skateboard/)).toBeTruthy();
            expect(gatewayClient.getVideoGenJobDetail).toHaveBeenCalledTimes(1);
        });

        it('shows loading first, and fills in dashes for what the job lacks', async () => {
            let finish: (v: unknown) => void = () => {};
            gatewayClient.getVideoGenJobDetail.mockReturnValue(new Promise((r) => { finish = r; }));
            await load();
            fireEvent.click(screen.getByText('job-done'));
            expect(await screen.findByText('Loading detail…')).toBeTruthy();

            finish({ job: { id: 'job-done', prompt: 'plain', resolution: null, duration_seconds: null } });
            expect(await screen.findByText('Resolution:')).toBeTruthy();
            expect(screen.queryByText('Error')).toBeNull();
            expect(screen.queryByText('Loading detail…')).toBeNull();
        });

        it('collapses and reports when the detail cannot be fetched', async () => {
            gatewayClient.getVideoGenJobDetail.mockRejectedValue(new Error('gone'));
            await load();
            fireEvent.click(screen.getByText('job-running'));

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Detail fetch failed: gone'));
            await waitFor(() => expect(screen.queryByText('Loading detail…')).toBeNull());
        });
    });

    describe('auto-refresh', () => {
        it('reloads list and stats while a job is in flight', async () => {
            await load();
            await waitFor(() => expect(refreshTick).not.toBeNull());
            const jobs = gatewayClient.queryVideoGenJobs.mock.calls.length;
            const fn = refreshTick;
            if (!fn) throw new Error('not refreshing');

            React.act(() => fn());
            await waitFor(() => expect(gatewayClient.queryVideoGenJobs.mock.calls.length).toBe(jobs + 1));
            expect(gatewayClient.getVideoGenStatistics).toHaveBeenCalledTimes(2);
        });

        it('does not run when every job has finished', async () => {
            gatewayClient.queryVideoGenJobs.mockResolvedValue({ jobs: [DONE], pagination: PAGE });
            render(<VideoGenJobsTab />);
            await screen.findByText('job-done');
            expect(refreshTick).toBeNull();
        });
    });
});
