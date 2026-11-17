/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

const getEvidenceRecord = vi.fn();
const listEvidenceRecords = vi.fn();

const gateway = { gatewayClient: { getEvidenceRecord, listEvidenceRecords } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import ChainPanel from '../ChainPanel';

const detail = (record_id: string, verdict: string, chain: unknown, root_task_id: string | null = 'run-1') => ({
    record_id, verdict, chain, root_task_id, surface: 'mcp', effect_type: 'tool.call', timestamp: '2026-09-19T10:00:00Z',
    log_id: 'log-1', seq: 1, kind: 'decide', kid: 'k', log: { log_id: 'log-1', seq: 1, tree_size_now: 5 }, record: {},
});

beforeEach(() => {
    vi.clearAllMocks();
});

describe('ChainPanel', () => {
    it('shows the parent from the chain block and the children of the run', async () => {
        getEvidenceRecord.mockResolvedValueOnce(detail('rec-plan-exec', 'executed', { parent_record_id: 'rec-plan', relation: 'confirms' }));
        listEvidenceRecords.mockResolvedValueOnce({
            items: [
                detail('rec-plan', 'planned', null),
                detail('rec-plan-exec', 'executed', { parent_record_id: 'rec-plan', relation: 'confirms' }),
                detail('rec-assess', 'assessment', { parent_record_id: 'rec-plan-exec', relation: 'assesses' }),
                detail('rec-other', 'executed', null),
            ],
            total: 4, limit: 200, offset: 0,
        });
        const onOpenRecord = vi.fn();
        render(<ChainPanel recordId="rec-plan-exec" onOpenRecord={onOpenRecord} onClose={vi.fn()} />);
        const parent = await screen.findByTestId('chain-parent');
        expect(listEvidenceRecords).toHaveBeenCalledWith({ root_task_id: 'run-1', limit: 200 });
        expect(parent).toHaveTextContent('confirms the planned record');
        expect(parent).toHaveTextContent('planned');
        expect(screen.getByTestId('chain-self')).toHaveTextContent('executed');
        expect(screen.getByTestId('chain-child-rec-assess')).toHaveTextContent('assesses');
        expect(screen.queryByTestId('chain-child-rec-other')).toBeNull();
        fireEvent.click(screen.getByTestId('record-link-rec-plan'));
        expect(onOpenRecord).toHaveBeenCalledWith('rec-plan');
    });

    it('marks an open half and a parent it cannot fetch', async () => {
        getEvidenceRecord
            .mockResolvedValueOnce(detail('rec-await', 'awaiting_human', { parent_record_id: 'rec-gone', relation: 'supersedes' }))
            .mockRejectedValueOnce(new Error('pruned'));
        listEvidenceRecords.mockResolvedValueOnce({ items: [detail('rec-await', 'awaiting_human', { parent_record_id: 'rec-gone', relation: 'supersedes' })], total: 1, limit: 200, offset: 0 });
        render(<ChainPanel recordId="rec-await" onOpenRecord={vi.fn()} onClose={vi.fn()} />);
        expect(await screen.findByTestId('chain-open-half')).toHaveTextContent('open half');
        expect(screen.getByTestId('chain-parent')).toHaveTextContent('not held here');
    });

    it('a record without a run and without a parent starts its chain alone', async () => {
        getEvidenceRecord.mockResolvedValueOnce(detail('rec-solo', 'recorded', null, null));
        const onClose = vi.fn();
        render(<ChainPanel recordId="rec-solo" onOpenRecord={vi.fn()} onClose={onClose} />);
        expect(await screen.findByTestId('chain-no-parent')).toBeInTheDocument();
        expect(screen.getByTestId('chain-no-children')).toBeInTheDocument();
        expect(listEvidenceRecords).not.toHaveBeenCalled();
        fireEvent.click(screen.getByTestId('chain-close'));
        expect(onClose).toHaveBeenCalled();
    });

    it('reports a failure', async () => {
        getEvidenceRecord.mockRejectedValueOnce(new Error('not found'));
        render(<ChainPanel recordId="rec-x" onOpenRecord={vi.fn()} onClose={vi.fn()} />);
        expect(await screen.findByTestId('chain-error')).toHaveTextContent('not found');
    });
});
