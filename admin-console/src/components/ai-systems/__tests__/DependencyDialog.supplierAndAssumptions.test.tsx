/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Dependency dialog:
 *
 *   * supplier_legal_entity is REQUIRED for an external dependency (the
 *     server 422s otherwise) and optional for a shared service;
 *   * the assumptions form mirrors the server's STRICT schema — only its
 *     keys are sent, and ranges are checked before a round trip;
 *   * the separability claim's seal is visible: sealed record + time, and a
 *     save whose seal FAILED keeps the dialog open saying so (the edge is
 *     saved; the seal is retried on the next save).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchAISystems = vi.fn();
const mockListAgentCards = vi.fn();
const mockFetchContracts = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockToastSuccess = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystems: mockFetchAISystems,
        listAgentCards: mockListAgentCards,
        fetchAISystemContracts: mockFetchContracts,
        createAISystemDependency: mockCreate,
        updateAISystemDependency: mockUpdate,
    }),
}));
vi.mock('react-hot-toast', () => ({
    default: {
        success: (...a: unknown[]) => {
            mockToastSuccess(...a);
        },
        error: vi.fn(),
    },
}));

import { DependencyDialog } from '../DependencyDialog';
import { ADDRESS_EDGE, BUREAU_EDGE } from './fixtures/compositionPayload';

const onSaved = vi.fn();
const onCancel = vi.fn();

function open(props: Partial<React.ComponentProps<typeof DependencyDialog>> = {}) {
    render(
        <DependencyDialog aiSystemId="sys-demo" aiSystemName="Brutor Demo System" onSaved={onSaved} onCancel={onCancel} {...props} />,
    );
}
const save = () => fireEvent.click(screen.getByTestId('dependency-save'));
const type = (testId: string, value: string) => fireEvent.change(screen.getByTestId(testId), { target: { value } });

async function externalDraft() {
    open();
    fireEvent.click(screen.getByTestId('dependency-role-external'));
    await screen.findByRole('option', { name: 'Credit Bureau Agent' });
    type('dependency-card', 'card-bureau');
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-fraud', name: 'fraud', display_name: 'Fraud Screener' }] });
    mockListAgentCards.mockResolvedValue({ cards: [{ id: 'card-bureau', name: 'Credit Bureau Agent' }], total: 1 });
    mockFetchContracts.mockResolvedValue({ contracts: [], total: 0, config_drifted: false });
    mockCreate.mockResolvedValue({ separability_seal: null });
    mockUpdate.mockResolvedValue({ separability_seal: null });
});

describe('supplier legal entity (Art 25(4))', () => {
    it('is required for an external dependency — the form never reaches the API without it', async () => {
        await externalDraft();
        save();
        expect(await screen.findByTestId('dependency-error-supplier_legal_entity')).toHaveTextContent('Art 25(4)');
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('is optional for a shared service, and sent trimmed when given', async () => {
        open({ prefill: { system_id: 'sys-fraud', agent_card_id: null, name: 'Fraud Screener' } });
        expect(screen.getByTestId('dependency-supplier')).toHaveAttribute('placeholder', 'Only if another legal entity provides this service');
        type('dependency-supplier', '  Group Services Ltd ');
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({ role: 'shared_service', supplier_legal_entity: 'Group Services Ltd' });
    });
});

describe('assumptions — the strict schema, mirrored', () => {
    it('sends exactly the typed keys: deduped classes, numbers, boolean, canonical evidence order', async () => {
        await externalDraft();
        type('dependency-supplier', 'Nordic Credit Bureau AB');
        type('assumption-data-classes', 'personal_data, financial, personal_data,');
        type('assumption-retention', ' 30 days, then deleted ');
        type('assumption-oversight', 'yes');
        type('assumption-availability', '99.9');
        type('assumption-latency', '800');
        fireEvent.click(screen.getByTestId('assumption-evidence-contract'));
        fireEvent.click(screen.getByTestId('assumption-evidence-attestation'));
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({
            supplier_legal_entity: 'Nordic Credit Bureau AB',
            assumptions: {
                data_classes_shared: ['personal_data', 'financial'],
                retention_at_delegate: '30 days, then deleted',
                human_oversight_at_delegate: true,
                expected_availability_pct: 99.9,
                expected_p95_latency_ms: 800,
                evidence_provided: ['attestation', 'contract'],
            },
        });
    });

    it('an empty form states nothing — assumptions is {}', async () => {
        await externalDraft();
        type('dependency-supplier', 'Acme AB');
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect((mockCreate.mock.calls[0][1] as { assumptions: unknown }).assumptions).toEqual({});
    });

    it.each([
        ['assumption-availability', '100.5', 'assumption-error-expected_availability_pct'],
        ['assumption-availability', '-1', 'assumption-error-expected_availability_pct'],
        ['assumption-latency', '12.5', 'assumption-error-expected_p95_latency_ms'],
        ['assumption-latency', '-3', 'assumption-error-expected_p95_latency_ms'],
    ])('%s = %s is refused before a round trip', async (field, value, errorId) => {
        await externalDraft();
        type('dependency-supplier', 'Acme AB');
        type(field, value);
        save();
        expect(await screen.findByTestId(errorId)).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('editing pre-fills the form from the edge and sends the keys of the schema', async () => {
        open({ edge: BUREAU_EDGE });
        expect(screen.getByTestId('assumption-data-classes')).toHaveValue('personal_data, financial');
        expect(screen.getByTestId('assumption-availability')).toHaveValue(99.5);
        expect(screen.getByTestId('assumption-evidence-contract')).toBeChecked();
        expect(screen.getByTestId('assumption-evidence-signed_agent_card')).toBeChecked();
        expect(screen.queryByTestId('assumption-dropped')).toBeNull();
        expect(screen.getByTestId('dependency-supplier')).toHaveValue('Nordic Credit Bureau AB');
        save();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        const patch = mockUpdate.mock.calls[0][2] as { assumptions: Record<string, unknown> };
        expect(patch.assumptions).toEqual({
            data_classes_shared: ['personal_data', 'financial'],
            expected_availability_pct: 99.5,
            evidence_provided: ['signed_agent_card', 'contract'],
        });
    });

    it('a shared service has no assumptions form', () => {
        open({ edge: ADDRESS_EDGE });
        expect(screen.queryByTestId('dependency-assumptions')).toBeNull();
    });
});

describe('sealed separability', () => {
    it('shows the sealed record and when it was sealed', () => {
        open({ edge: ADDRESS_EDGE });
        const line = screen.getByTestId('dependency-separability-sealed');
        expect(line).toHaveTextContent('rec-sep-address-0001');
        expect(line).toHaveTextContent('Changing the rationale seals a new record');
    });

    it('a changed rationale is a new claim — no longer shown as sealed', () => {
        open({ edge: ADDRESS_EDGE });
        type('dependency-rationale', 'A different claim');
        expect(screen.queryByTestId('dependency-separability-sealed')).toBeNull();
        expect(screen.getByTestId('dependency-separability-unsealed')).toHaveTextContent('sealed as a declaration record when you save');
    });

    it('an unsealed claim on file says the save retries it', () => {
        open({ edge: { ...ADDRESS_EDGE, separability_record_id: null, separability_sealed_at: null } });
        expect(screen.getByTestId('dependency-separability-unsealed')).toHaveTextContent('Saving retries it');
    });

    it('a save whose seal FAILED stays open, says why, and closes as saved', async () => {
        mockUpdate.mockResolvedValue({
            ...ADDRESS_EDGE,
            separability_record_id: null,
            separability_seal: { sealed: false, record_id: null, seal_error: 'core_unreachable', seal_detail: 'The core did not answer.' },
        });
        open({ edge: { ...ADDRESS_EDGE, separability_record_id: null, separability_sealed_at: null } });
        save();
        const alert = await screen.findByTestId('dependency-seal-failure');
        expect(alert).toHaveTextContent('not sealed');
        expect(alert).toHaveTextContent('core_unreachable');
        expect(alert).toHaveTextContent('The core did not answer.');
        expect(alert).toHaveTextContent('retried the next time');
        expect(onSaved).not.toHaveBeenCalled();
        expect(screen.queryByTestId('dependency-save')).toBeNull();
        fireEvent.click(screen.getByTestId('dependency-close-saved'));
        expect(onSaved).toHaveBeenCalledTimes(1);
    });

    it('a save that sealed the claim closes with a confirmation', async () => {
        mockUpdate.mockResolvedValue({
            ...ADDRESS_EDGE,
            separability_seal: { sealed: true, record_id: 'rec-new', seal_error: null, seal_detail: null },
        });
        open({ edge: ADDRESS_EDGE });
        type('dependency-rationale', 'A different claim');
        save();
        await waitFor(() => expect(onSaved).toHaveBeenCalled());
        expect(mockToastSuccess).toHaveBeenCalledWith(expect.stringContaining('sealed'));
        expect(screen.queryByTestId('dependency-seal-failure')).toBeNull();
    });
});
