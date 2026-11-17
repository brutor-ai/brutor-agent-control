/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A2A task registry (read-only): filters, paging, detail dialog.
 *
 * Pinned: the filters work as labelled. The context box says "press Enter to
 * apply", so a keystroke must not query the server with a partial id for an
 * exact match. A failed push-config fetch must not read as "none registered",
 * which on an evidence screen is a false statement.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gatewayClient = {
    listA2ATasks: vi.fn(),
    getA2ATask: vi.fn(),
    listA2ATaskPushConfigs: vi.fn(),
};
const auth = { gatewayClient };
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));

import A2ATasksTab from '../A2ATasksTab';

const CONTEXT = 'Exact match — press Enter to apply';

const TASK = {
    id: 'task-1',
    tenant_id: 't-1',
    state: 'TASK_STATE_COMPLETED',
    context_id: 'ctx-1',
    resource_group_id: 'abcdefgh-1234',
    status_timestamp: '2026-07-01T00:00:00Z',
};
const TASK_2 = { ...TASK, id: 'task-2', state: 'TASK_STATE_FAILED', context_id: null, resource_group_id: null };

const DETAIL = {
    ...TASK,
    task_json: {
        status: { message: { role: 'agent', text: 'all done' } },
        history: [
            { role: 'USER', messageId: 'm-1', text: 'hello' },
            { text: 'no role' },
        ],
        artifacts: [{ artifactId: 'art-1', name: 'report.pdf' }, { artifactId: 'art-2' }],
        metadata: { source: 'portal' },
    },
};

function lastQuery(): unknown {
    const calls = gatewayClient.listA2ATasks.mock.calls;
    return calls[calls.length - 1][0];
}

async function load() {
    render(<A2ATasksTab />);
    await screen.findByText('task-1');
}

async function openDetail() {
    await load();
    fireEvent.click(screen.getByText('task-1'));
    await screen.findByText('Task Detail');
    await screen.findByText('Status message');
}

function tab(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}

describe('A2ATasksTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gatewayClient.listA2ATasks.mockResolvedValue({ tasks: [TASK, TASK_2], total: 2 });
        gatewayClient.getA2ATask.mockResolvedValue(DETAIL);
        gatewayClient.listA2ATaskPushConfigs.mockResolvedValue({ configs: [] });
    });

    afterEach(() => cleanup());

    describe('list', () => {
        it('loads the first page and renders state, context and group', async () => {
            await load();
            expect(lastQuery()).toEqual({ state: undefined, context_id: undefined, limit: 50, offset: 0 });
            expect(screen.getByText('2 tasks matching filters')).toBeTruthy();
            expect(screen.getByText('completed')).toBeTruthy();
            expect(screen.getByText('failed')).toBeTruthy();
            expect(screen.getByText('ctx=ctx-1')).toBeTruthy();
            expect(screen.getByText('rg=abcdefgh…')).toBeTruthy();
            expect(screen.getByText(/Showing 1–2\s+of 2/)).toBeTruthy();
        });

        it('shows the empty state', async () => {
            gatewayClient.listA2ATasks.mockResolvedValue({ tasks: [], total: 0 });
            render(<A2ATasksTab />);
            expect(await screen.findByText('No tasks found')).toBeTruthy();
        });

        it('shows a load failure', async () => {
            gatewayClient.listA2ATasks.mockRejectedValue(new Error('gateway down'));
            render(<A2ATasksTab />);
            expect(await screen.findByText('gateway down')).toBeTruthy();
        });

        it('refreshes', async () => {
            await load();
            fireEvent.click(screen.getByTitle('Refresh'));
            await waitFor(() => expect(gatewayClient.listA2ATasks).toHaveBeenCalledTimes(2));
        });
    });

    describe('filters', () => {
        it('filters by state and goes back to the first page', async () => {
            gatewayClient.listA2ATasks.mockResolvedValue({ tasks: [TASK], total: 120 });
            await load();
            fireEvent.click(screen.getByRole('button', { name: 'Next' }));
            await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 50 }));
            await screen.findByText('task-1');

            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'TASK_STATE_FAILED' } });
            await waitFor(() =>
                expect(lastQuery()).toEqual({ state: 'TASK_STATE_FAILED', context_id: undefined, limit: 50, offset: 0 }),
            );
        });

        it('does not query per keystroke: the context filter applies on Enter, trimmed', async () => {
            await load();
            const box = screen.getByPlaceholderText(CONTEXT);
            fireEvent.change(box, { target: { value: '  ctx' } });
            fireEvent.change(box, { target: { value: '  ctx-9  ' } });
            await new Promise((r) => setTimeout(r, 20));
            expect(gatewayClient.listA2ATasks).toHaveBeenCalledTimes(1);

            fireEvent.keyDown(box, { key: 'a' });
            expect(gatewayClient.listA2ATasks).toHaveBeenCalledTimes(1);

            fireEvent.keyDown(box, { key: 'Enter' });
            await waitFor(() =>
                expect(lastQuery()).toEqual({ state: undefined, context_id: 'ctx-9', limit: 50, offset: 0 }),
            );
            expect(gatewayClient.listA2ATasks).toHaveBeenCalledTimes(2);
        });

        it('applying a context filter goes back to the first page in one request', async () => {
            gatewayClient.listA2ATasks.mockResolvedValue({ tasks: [TASK], total: 120 });
            await load();
            fireEvent.click(screen.getByRole('button', { name: 'Next' }));
            await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 50 }));
            await screen.findByText('task-1');
            const before = gatewayClient.listA2ATasks.mock.calls.length;

            const box = screen.getByPlaceholderText(CONTEXT);
            fireEvent.change(box, { target: { value: 'ctx-9' } });
            fireEvent.keyDown(box, { key: 'Enter' });

            await waitFor(() => expect(lastQuery()).toEqual({ state: undefined, context_id: 'ctx-9', limit: 50, offset: 0 }));
            expect(gatewayClient.listA2ATasks.mock.calls.length).toBe(before + 1);
        });

        it('clear resets both filters and the box', async () => {
            await load();
            const box = screen.getByPlaceholderText<HTMLInputElement>(CONTEXT);
            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'TASK_STATE_WORKING' } });
            fireEvent.change(box, { target: { value: 'ctx-9' } });
            fireEvent.keyDown(box, { key: 'Enter' });
            await waitFor(() => expect(lastQuery()).toMatchObject({ state: 'TASK_STATE_WORKING', context_id: 'ctx-9' }));

            fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
            await waitFor(() => expect(lastQuery()).toEqual({ state: undefined, context_id: undefined, limit: 50, offset: 0 }));
            expect(screen.getByPlaceholderText<HTMLInputElement>(CONTEXT).value).toBe('');
            expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull();
        });
    });

    describe('paging', () => {
        it('moves by the page size and stops at the ends', async () => {
            gatewayClient.listA2ATasks.mockResolvedValue({ tasks: [TASK], total: 60 });
            await load();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Previous' }).disabled).toBe(true);

            fireEvent.click(screen.getByRole('button', { name: 'Next' }));
            await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 50 }));
            await screen.findByText(/Showing 51–51\s+of 60/);
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Next' }).disabled).toBe(true);

            fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
            await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 0 }));
        });
    });

    describe('detail', () => {
        it('opens with the task identity and status message', async () => {
            await openDetail();
            expect(gatewayClient.getA2ATask).toHaveBeenCalledWith('task-1');
            expect(screen.getByText('t-1')).toBeTruthy();
            expect(screen.getByText('abcdefgh-1234')).toBeTruthy();
            expect(screen.getByText(/all done/)).toBeTruthy();
        });

        it('reports a task that cannot be loaded and leaves no dialog', async () => {
            gatewayClient.getA2ATask.mockRejectedValue(new Error('gone'));
            await load();
            fireEvent.click(screen.getByText('task-1'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load failed', 'gone'));
            await waitFor(() => expect(screen.queryByText('Task Detail')).toBeNull());
        });

        it('shows history, numbering entries and defaulting a missing role', async () => {
            await openDetail();
            tab('History');
            expect(screen.getByText(/#1 · role=USER/)).toBeTruthy();
            expect(screen.getByText('m-1')).toBeTruthy();
            expect(screen.getByText(/#2 · role=UNKNOWN/)).toBeTruthy();
        });

        it('shows artifacts', async () => {
            await openDetail();
            tab('Artifacts');
            expect(screen.getByText(/name=report\.pdf/)).toBeTruthy();
            expect(screen.getByText('art-2')).toBeTruthy();
        });

        it('shows metadata and the raw JSON', async () => {
            await openDetail();
            tab('Metadata');
            expect(screen.getByText(/"source": "portal"/)).toBeTruthy();
            tab('Raw JSON');
            expect(screen.getByText(/"messageId": "m-1"/)).toBeTruthy();
        });

        it('says when a task has none of those', async () => {
            gatewayClient.getA2ATask.mockResolvedValue({ ...TASK, task_json: {} });
            await load();
            fireEvent.click(screen.getByText('task-1'));
            await screen.findByText('Task Detail');
            await screen.findByRole('button', { name: 'History' });
            expect(screen.queryByText('Status message')).toBeNull();

            tab('History');
            expect(screen.getByText('No history entries.')).toBeTruthy();
            tab('Artifacts');
            expect(screen.getByText('No artifacts.')).toBeTruthy();
            tab('Metadata');
            expect(screen.getByText('No metadata.')).toBeTruthy();
        });

        it('fetches push configs only when that tab opens, and only once', async () => {
            gatewayClient.listA2ATaskPushConfigs.mockResolvedValue({
                configs: [{ id: 'pc-1', created_at: '2026-07-01T00:00:00Z', config_json: { url: 'https://hook.test' } }],
            });
            await openDetail();
            expect(gatewayClient.listA2ATaskPushConfigs).not.toHaveBeenCalled();

            tab('Push Configs');
            expect(await screen.findByText('pc-1')).toBeTruthy();
            expect(gatewayClient.listA2ATaskPushConfigs).toHaveBeenCalledWith('task-1');
            expect(screen.getByText(/"url": "https:\/\/hook\.test"/)).toBeTruthy();

            tab('Status');
            tab('Push Configs');
            expect(gatewayClient.listA2ATaskPushConfigs).toHaveBeenCalledTimes(1);
        });

        it('says none are registered only when the fetch succeeded', async () => {
            await openDetail();
            tab('Push Configs');
            expect(await screen.findByText('No push-notification configs registered for this task.')).toBeTruthy();
        });

        it('shows a failed push-config fetch as a failure, not as none registered', async () => {
            gatewayClient.listA2ATaskPushConfigs.mockRejectedValue(new Error('forbidden'));
            await openDetail();
            tab('Push Configs');
            expect(await screen.findByText('forbidden')).toBeTruthy();
            expect(screen.queryByText('No push-notification configs registered for this task.')).toBeNull();
        });

        it('closes', async () => {
            await openDetail();
            fireEvent.click(screen.getByRole('button', { name: 'Close' }));
            expect(screen.queryByText('Task Detail')).toBeNull();
        });
    });
});
