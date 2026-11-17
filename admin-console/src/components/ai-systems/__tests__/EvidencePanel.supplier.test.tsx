/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Supplier information evidence (EU AI Act Art 25(4)):
 *
 *   * the kind is offered, and choosing it asks for the dependency edge it
 *     covers (only THIS system's edges; external / supplier-entity edges
 *     first) and makes the SHA-256 required — the control plane 422s both;
 *   * other kinds never send a dependency_id (422 otherwise);
 *   * each row says which edge it covers, and a row whose edge was removed
 *     reads "detached" instead of pretending to back a dependency;
 *   * edges that need supplier information and lack it are named.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockFetch = vi.fn();
const mockCreate = vi.fn();
const mockDelete = vi.fn();
const mockDeps = vi.fn();
// Stable, like the real singleton client (the effect depends on it).
const gatewayClient = { fetchAISystemDependencies: mockDeps };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemEvidence: mockFetch,
        createAISystemEvidence: mockCreate,
        deleteAISystemEvidence: mockDelete,
        gatewayClient,
    }),
}));

import EvidencePanel from '../EvidencePanel';
import type { AISystemEvidenceItem } from '../../../lib/aiSystemTypes';
import { ADDRESS_EDGE, BUREAU_EDGE, FRAUD_EDGE } from './fixtures/compositionPayload';

const SHA = 'a'.repeat(64);

function item(overrides: Partial<AISystemEvidenceItem> = {}): AISystemEvidenceItem {
    return { id: 'ev-1', ai_system_id: 'sys-demo', kind: 'impact_assessment', title: 'FRIA', is_expired: false, ...overrides };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue({ evidence: [], total: 0 });
    mockCreate.mockResolvedValue(item());
    mockDeps.mockResolvedValue({ dependencies: [FRAUD_EDGE, ADDRESS_EDGE, BUREAU_EDGE], total: 3 });
});

async function openForm() {
    render(<EvidencePanel aiSystemId="sys-demo" />);
    await waitFor(() => expect(mockDeps).toHaveBeenCalledWith('sys-demo'));
    fireEvent.click(screen.getByTestId('evidence-add-toggle'));
    fireEvent.change(screen.getByTestId('evidence-kind'), { target: { value: 'supplier_information' } });
    fireEvent.change(screen.getByTestId('evidence-title'), { target: { value: 'Bureau MSA 2026' } });
}

describe('attaching supplier information', () => {
    it('offers this system’s edges, those that need it first', async () => {
        await openForm();
        const picker = screen.getByTestId('evidence-dependency');
        const options = within(picker).getAllByRole('option').slice(1);
        expect(options[0]).toHaveValue('dep-bureau');
        expect(options[0]).toHaveTextContent('Credit Bureau Agent · External · Nordic Credit Bureau AB');
        expect(options.map((o) => (o as HTMLOptionElement).value).sort()).toEqual(['dep-address', 'dep-bureau', 'dep-fraud']);
    });

    it('requires the edge AND the hash before Attach is possible', async () => {
        await openForm();
        const attach = screen.getByTestId('evidence-save');
        expect(attach).toBeDisabled();
        fireEvent.change(screen.getByTestId('evidence-dependency'), { target: { value: 'dep-bureau' } });
        expect(attach).toBeDisabled();
        fireEvent.change(screen.getByTestId('evidence-sha256'), { target: { value: SHA } });
        expect(attach).toBeEnabled();
    });

    it('sends the dependency_id and the hash', async () => {
        await openForm();
        fireEvent.change(screen.getByTestId('evidence-dependency'), { target: { value: 'dep-bureau' } });
        fireEvent.change(screen.getByTestId('evidence-sha256'), { target: { value: SHA } });
        fireEvent.click(screen.getByTestId('evidence-save'));
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({
            kind: 'supplier_information',
            title: 'Bureau MSA 2026',
            dependency_id: 'dep-bureau',
            sha256: SHA,
        });
    });

    it('another kind never sends a dependency_id, even after one was picked', async () => {
        await openForm();
        fireEvent.change(screen.getByTestId('evidence-dependency'), { target: { value: 'dep-bureau' } });
        fireEvent.change(screen.getByTestId('evidence-kind'), { target: { value: 'eval_report' } });
        expect(screen.queryByTestId('evidence-dependency')).toBeNull();
        fireEvent.click(screen.getByTestId('evidence-save'));
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).not.toHaveProperty('dependency_id', 'dep-bureau');
        expect((mockCreate.mock.calls[0][1] as Record<string, unknown>).dependency_id).toBeUndefined();
    });

    it('says so when the dependencies could not be loaded', async () => {
        mockDeps.mockRejectedValue(new Error('down'));
        await openForm();
        expect(within(screen.getByTestId('evidence-dependency')).getAllByRole('option')[0]).toHaveTextContent(
            'Dependencies could not be loaded',
        );
    });
});

describe('supplier information rows', () => {
    it('each row names the edge it covers; a removed edge reads detached', async () => {
        mockFetch.mockResolvedValue({
            evidence: [
                item({ id: 'ev-msa', kind: 'supplier_information', title: 'Bureau MSA', dependency_id: 'dep-bureau', sha256: SHA }),
                item({ id: 'ev-old', kind: 'supplier_information', title: 'Old vendor DPA', dependency_id: null, sha256: SHA }),
                item({ id: 'ev-fria', kind: 'impact_assessment', title: 'FRIA' }),
            ],
            total: 3,
        });
        render(<EvidencePanel aiSystemId="sys-demo" />);
        const covers = await screen.findByTestId('evidence-covers-ev-msa');
        await waitFor(() => expect(covers).toHaveTextContent('Covers: Credit Bureau Agent · External · Nordic Credit Bureau AB'));
        expect(covers).toHaveAttribute('data-detached', 'false');
        const detached = screen.getByTestId('evidence-covers-ev-old');
        expect(detached).toHaveAttribute('data-detached', 'true');
        expect(detached).toHaveTextContent('Detached');
        expect(screen.queryByTestId('evidence-covers-ev-fria')).toBeNull();
        expect(screen.getByTestId('evidence-ev-msa')).toHaveTextContent('Supplier information (Art 25(4))');
    });

    it('names the edges that need supplier information and have none unexpired', async () => {
        mockDeps.mockResolvedValue({
            dependencies: [FRAUD_EDGE, { ...ADDRESS_EDGE, supplier_legal_entity: 'Group Address Services Ltd' }, BUREAU_EDGE],
            total: 3,
        });
        mockFetch.mockResolvedValue({
            evidence: [item({ id: 'ev-msa', kind: 'supplier_information', dependency_id: 'dep-bureau', sha256: SHA })],
            total: 1,
        });
        render(<EvidencePanel aiSystemId="sys-demo" />);
        const missing = await screen.findByTestId('evidence-supplier-missing');
        expect(missing).toHaveTextContent('Address Validator · Shared service · Group Address Services Ltd');
        expect(missing).not.toHaveTextContent('Credit Bureau Agent');
        expect(missing).not.toHaveTextContent('Fraud Screener');
    });

    it('an expired agreement does not count as on file', async () => {
        mockFetch.mockResolvedValue({
            evidence: [item({ id: 'ev-msa', kind: 'supplier_information', dependency_id: 'dep-bureau', sha256: SHA, is_expired: true })],
            total: 1,
        });
        render(<EvidencePanel aiSystemId="sys-demo" />);
        expect(await screen.findByTestId('evidence-supplier-missing')).toHaveTextContent('Credit Bureau Agent');
    });
});
