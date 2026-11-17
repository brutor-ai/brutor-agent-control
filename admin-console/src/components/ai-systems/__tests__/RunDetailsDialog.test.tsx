/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Run details → Evidence record: when the rollup carries a
 * `record_id`, the dialog groups it and offers "View record", which opens
 * the same record dialog the ledger uses.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

vi.mock('../RunEvidenceSection', () => ({
    default: (p: { rootTaskId: string }) => <div data-testid="run-evidence-stub">{p.rootTaskId}</div>,
}));

vi.mock('../../compliance/RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));

import RunDetailsDialog from '../RunDetailsDialog';
import type { RunSummary } from '../../../lib/aiSystemTypes';

const RUN = {
    id: 'run-1',
    root_task_id: 'run-1',
    via_system_ids: [],
    terminal_state: 'completed',
    finalized_by: 'client',
} as unknown as RunSummary;

describe('RunDetailsDialog evidence record', () => {
    it('groups record_id and opens the shared record dialog', async () => {
        const fetchRun = vi.fn().mockResolvedValue({ ...RUN, record_id: 'rec-42' });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        const group = await screen.findByTestId('run-details-group-evidence-record');
        expect(group).toHaveTextContent('rec-42');
        fireEvent.click(screen.getByTestId('run-details-view-record'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-42');
    });

    it('mounts the sealed-steps section for the run root', () => {
        render(<RunDetailsDialog run={RUN} onClose={vi.fn()} />);
        expect(screen.getByTestId('run-evidence-stub')).toHaveTextContent('run-1');
    });

    it('omits the group and the button when the run has no record', () => {
        render(<RunDetailsDialog run={RUN} onClose={vi.fn()} />);
        expect(screen.queryByTestId('run-details-group-evidence-record')).toBeNull();
        expect(screen.queryByTestId('run-details-view-record')).toBeNull();
    });
});

describe('RunDetailsDialog implementation', () => {
    it('shows the run’s release with its trust and the normalised client label', async () => {
        const fetchRun = vi.fn().mockResolvedValue({
            ...RUN,
            agent_impl_name: 'screening-agent',
            agent_impl_version: '1.5.0',
            agent_impl_build: null,
            agent_impl_trust: 'declared',
            implementation_fingerprint: `sha256:${'a'.repeat(64)}`,
            client_label: 'python-httpx',
        });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        const group = await screen.findByTestId('run-details-group-implementation');
        const line = screen.getByTestId('run-details-release');
        expect(line).toHaveTextContent('screening-agent@1.5.0');
        expect(screen.getByTestId('trust-badge')).toHaveAttribute('data-trust', 'declared');
        // A run's own release is not judged here: no approval badge.
        expect(screen.queryByTestId('approval-badge')).toBeNull();
        expect(screen.getByTestId('run-details-client')).toHaveTextContent('via client python-httpx');
        expect(group).toHaveTextContent('implementation_fingerprint');
    });

    it('says so when the run declared no release', async () => {
        const fetchRun = vi.fn().mockResolvedValue({ ...RUN, agent_impl_name: null, agent_impl_version: null });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        await screen.findByTestId('run-details-group-implementation');
        expect(screen.getByTestId('run-details-release')).toHaveTextContent('No release declared on this run.');
    });
});

describe('RunDetailsDialog outcome', () => {
    it('groups the three answers and the run class, with the cause in words', async () => {
        const fetchRun = vi.fn().mockResolvedValue({
            ...RUN,
            terminal_state: 'errored',
            run_class: 'failed',
            outcome_id: null,
            outcome_conformance: 'unreported',
            outcome_counts_toward_baseline: null,
            outcome_contract_version: 7,
            error_cause: 'model_unavailable',
        });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        const group = await screen.findByTestId('run-details-group-outcome');
        expect(screen.getByTestId('run-details-class')).toHaveTextContent('Failed');
        expect(screen.getByTestId('run-details-error-cause')).toHaveTextContent('Model unavailable');
        expect(screen.getByTestId('run-field-error_cause')).toHaveTextContent('Model unavailable (model_unavailable)');
        expect(screen.getByTestId('run-field-run_class')).toHaveTextContent('Failed (failed)');
        expect(screen.getByTestId('run-field-outcome_conformance')).toHaveTextContent('unreported');
        expect(screen.getByTestId('run-field-outcome_contract_version')).toHaveTextContent('v7');
        expect(group).toHaveTextContent('Not reported');
    });

    it('marks a declared no-work outcome, never as a success', async () => {
        const fetchRun = vi.fn().mockResolvedValue({
            ...RUN,
            run_class: 'no_work',
            outcome_id: 'no_applications_available',
            outcome_conformance: 'declared',
            outcome_counts_toward_baseline: false,
            outcome_contract_version: 7,
            error_cause: null,
        });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        await screen.findByTestId('run-details-group-outcome');
        const cls = screen.getByTestId('run-details-class');
        expect(cls).toHaveTextContent('No work');
        expect(cls.className).not.toMatch(/emerald|green/);
        expect(screen.getByTestId('outcome-baseline-run-1')).toHaveTextContent('no work');
        expect(screen.getByTestId('outcome-run-1')).toHaveTextContent('no_applications_available');
        expect(screen.queryByTestId('run-details-error-cause')).toBeNull();
        expect(screen.getByTestId('run-field-outcome_counts_toward_baseline')).toHaveTextContent('no');
    });

    it('no longer lists a client-reported outcome', async () => {
        const fetchRun = vi.fn().mockResolvedValue({ ...RUN, run_class: 'success' });
        render(<RunDetailsDialog run={RUN} fetchRun={fetchRun} onClose={vi.fn()} />);
        const when = await screen.findByTestId('run-details-group-when');
        expect(when).not.toHaveTextContent('client_outcome');
    });
});
