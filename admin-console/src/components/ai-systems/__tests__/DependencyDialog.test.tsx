/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Dependency declare / edit dialog. The client-side rules
 * mirror the control plane's 422s: degradable needs a fallback, a
 * non-contributing dependency needs a separability rationale, an external
 * dependency needs an agent card, a shared service needs its target system.
 * A form that fails a rule must not reach the API.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchAISystems = vi.fn();
const mockListAgentCards = vi.fn();
const mockFetchContracts = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockFetchOutcomes = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystems: mockFetchAISystems,
        listAgentCards: mockListAgentCards,
        fetchAISystemContracts: mockFetchContracts,
        createAISystemDependency: mockCreate,
        updateAISystemDependency: mockUpdate,
        fetchAISystemOutcomes: mockFetchOutcomes,
    }),
}));

import { DependencyDialog } from '../DependencyDialog';
import { ADDRESS_EDGE } from './fixtures/compositionPayload';

const onSaved = vi.fn();
const onCancel = vi.fn();

function open(props: Partial<React.ComponentProps<typeof DependencyDialog>> = {}) {
    render(
        <DependencyDialog
            aiSystemId="sys-demo"
            aiSystemName="Brutor Demo System"
            onSaved={onSaved}
            onCancel={onCancel}
            {...props}
        />,
    );
}

const save = () => fireEvent.click(screen.getByTestId('dependency-save'));

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchAISystems.mockResolvedValue({
        systems: [
            { id: 'sys-demo', name: 'demo', display_name: 'Brutor Demo System' },
            { id: 'sys-fraud', name: 'fraud', display_name: 'Fraud Screener' },
        ],
    });
    mockListAgentCards.mockResolvedValue({ cards: [{ id: 'card-bureau', name: 'Credit Bureau Agent' }], total: 1 });
    mockFetchContracts.mockResolvedValue({
        contracts: [{ id: 'ctr-fraud-2', version: 2, status: 'active' }],
        total: 1,
        config_drifted: false,
    });
    mockCreate.mockResolvedValue({});
    mockUpdate.mockResolvedValue({});
    // The Fraud Screener's active catalogue (Brutor Demo System).
    mockFetchOutcomes.mockResolvedValue({
        ai_system_id: 'sys-fraud',
        outcomes: null,
        active_outcomes: [
            {
                id: 'screening_completed', label: 'Screening completed', counts_toward_baseline: true,
                variants: [
                    { id: 'clear', label: 'Clear' },
                    { id: 'hit', label: 'Hit' },
                    { id: 'review', label: 'Review' },
                ],
            },
        ],
        active_contract_version: 2,
        reportable_ids: [],
        config_drifted: false,
    });
});

describe('DependencyDialog — validation mirrors the server 422 rules', () => {
    it('a shared service needs its target system', async () => {
        open();
        save();
        expect(await screen.findByTestId('dependency-error-depends_on_system_id')).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('never offers the system itself as a target', async () => {
        open();
        await screen.findByRole('option', { name: 'Fraud Screener' });
        expect(screen.queryByRole('option', { name: 'Brutor Demo System' })).toBeNull();
    });

    it('an external dependency needs an agent card', async () => {
        open();
        fireEvent.click(screen.getByTestId('dependency-role-external'));
        save();
        expect(await screen.findByTestId('dependency-error-agent_card_id')).toBeInTheDocument();
        expect(screen.queryByTestId('dependency-target')).toBeNull();
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('degradable needs a fallback', async () => {
        open();
        await screen.findByRole('option', { name: 'Fraud Screener' });
        fireEvent.change(screen.getByTestId('dependency-target'), { target: { value: 'sys-fraud' } });
        fireEvent.click(screen.getByTestId('dependency-criticality-degradable'));
        save();
        expect(await screen.findByTestId('dependency-error-fallback')).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();

        fireEvent.change(screen.getByTestId('dependency-fallback'), { target: { value: '   ' } });
        expect(screen.getByTestId('dependency-error-fallback')).toBeInTheDocument();
        fireEvent.change(screen.getByTestId('dependency-fallback'), { target: { value: 'Manual review' } });
        expect(screen.queryByTestId('dependency-error-fallback')).toBeNull();
    });

    it('non-contributing needs a separability rationale', async () => {
        open();
        await screen.findByRole('option', { name: 'Fraud Screener' });
        fireEvent.change(screen.getByTestId('dependency-target'), { target: { value: 'sys-fraud' } });
        fireEvent.click(screen.getByTestId('dependency-contributes'));
        save();
        expect(await screen.findByTestId('dependency-error-separability_rationale')).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
    });
});

describe('DependencyDialog — what is sent', () => {
    it('declares a critical shared service, following the active contract', async () => {
        open();
        await screen.findByRole('option', { name: 'Fraud Screener' });
        fireEvent.change(screen.getByTestId('dependency-target'), { target: { value: 'sys-fraud' } });
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
        expect(mockCreate).toHaveBeenCalledWith('sys-demo', {
            role: 'shared_service',
            depends_on_system_id: 'sys-fraud',
            agent_card_id: null,
            criticality: 'critical',
            fallback: null,
            contributes_to_decision: true,
            separability_rationale: null,
            contract_pin: null,
            supplier_legal_entity: null,
            assumptions: {},
            // Not declared unless the operator declares it: no check.
            expected_outcomes: null,
        });
        expect(onSaved).toHaveBeenCalled();
    });

    it('pins to one of the TARGET’s contracts', async () => {
        open({ prefill: { system_id: 'sys-fraud', agent_card_id: null, name: 'Fraud Screener' } });
        fireEvent.click(screen.getByTestId('dependency-pin-pinned'));
        await screen.findByRole('option', { name: 'v2 (active)' });
        expect(mockFetchContracts).toHaveBeenCalledWith('sys-fraud');
        fireEvent.change(screen.getByTestId('dependency-pin'), { target: { value: 'ctr-fraud-2' } });
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({ contract_pin: 'ctr-fraud-2' });
    });

    it('declares an external agent with a trimmed fallback and no pin', async () => {
        open();
        fireEvent.click(screen.getByTestId('dependency-role-external'));
        await screen.findByRole('option', { name: 'Credit Bureau Agent' });
        fireEvent.change(screen.getByTestId('dependency-card'), { target: { value: 'card-bureau' } });
        fireEvent.click(screen.getByTestId('dependency-criticality-degradable'));
        fireEvent.change(screen.getByTestId('dependency-fallback'), { target: { value: '  Manual underwriting ' } });
        fireEvent.change(screen.getByTestId('dependency-supplier'), { target: { value: ' Nordic Credit Bureau AB ' } });
        expect(screen.queryByTestId('dependency-pin')).toBeNull();
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({
            role: 'external',
            agent_card_id: 'card-bureau',
            depends_on_system_id: null,
            criticality: 'degradable',
            fallback: 'Manual underwriting',
            contract_pin: null,
            supplier_legal_entity: 'Nordic Credit Bureau AB',
            assumptions: {},
        });
    });

    it('edit PATCHes only the mutable fields and clears what no longer applies', async () => {
        open({ edge: ADDRESS_EDGE });
        expect(screen.getByTestId('dependency-target')).toBeDisabled();
        fireEvent.click(screen.getByTestId('dependency-criticality-critical'));
        fireEvent.click(screen.getByTestId('dependency-contributes'));
        save();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        const [sysId, edgeId, patch] = mockUpdate.mock.calls[0] as [string, string, Record<string, unknown>];
        expect(sysId).toBe('sys-demo');
        expect(edgeId).toBe('dep-address');
        expect(patch).toEqual({
            criticality: 'critical',
            fallback: null,
            contributes_to_decision: true,
            separability_rationale: null,
            contract_pin: 'ctr-address-2',
            supplier_legal_entity: null,
            assumptions: {},
            expected_outcomes: null,
        });
        expect(patch).not.toHaveProperty('role');
        expect(patch).not.toHaveProperty('depends_on_system_id');
    });

    it('shows the server’s message when it still refuses (409 duplicate)', async () => {
        mockCreate.mockRejectedValue(new Error('Dependency already declared'));
        open({ prefill: { system_id: 'sys-fraud', agent_card_id: null, name: 'Fraud Screener' } });
        save();
        expect(await screen.findByTestId('dependency-server-error')).toHaveTextContent('Dependency already declared');
        expect(onSaved).not.toHaveBeenCalled();
    });
});

describe('DependencyDialog — follow the active contract, or pin', () => {
    const TARGET_CONTRACTS = {
        contracts: [
            { id: 'ctr-address-3', version: 3, status: 'active', is_active: true, is_approved: true },
            { id: 'ctr-address-2', version: 2, status: 'superseded', is_active: false, is_approved: true },
            { id: 'ctr-address-4', version: 4, status: 'rejected', is_active: false, is_approved: false },
            { id: 'ctr-address-5', version: 5, status: 'draft', is_active: false, is_approved: false },
        ],
        total: 4,
        active_version: 3,
        config_drifted: false,
    };

    it('says which version "follow" means today', async () => {
        mockFetchContracts.mockResolvedValue(TARGET_CONTRACTS);
        open({ prefill: { system_id: 'sys-address', agent_card_id: null, name: 'Address Validator' } });
        expect(screen.getByTestId('dependency-pin-follow')).toBeChecked();
        expect(await screen.findByTestId('dependency-active-version')).toHaveTextContent('(currently v3)');
        expect(screen.queryByTestId('dependency-pin')).toBeNull();
    });

    it('shows "pinned vX, target active vY" and flags a pin that is behind', async () => {
        mockFetchContracts.mockResolvedValue(TARGET_CONTRACTS);
        open({ edge: ADDRESS_EDGE });
        expect(screen.getByTestId('dependency-pin-pinned')).toBeChecked();
        const status = await screen.findByTestId('dependency-pin-status');
        expect(status).toHaveTextContent('Pinned v2, target active v3 — this pin is behind');
        fireEvent.change(screen.getByTestId('dependency-pin'), { target: { value: 'ctr-address-3' } });
        expect(screen.getByTestId('dependency-pin-status')).toHaveTextContent('Pinned v3, target active v3 — up to date');
    });

    it('offers only signed contracts to pin to', async () => {
        mockFetchContracts.mockResolvedValue(TARGET_CONTRACTS);
        open({ edge: ADDRESS_EDGE });
        await screen.findByRole('option', { name: 'v3 (active)' });
        const select: HTMLSelectElement = screen.getByTestId('dependency-pin');
        const options = Array.from(select.options).map((o) => o.value);
        expect(options).toEqual(['', 'ctr-address-3', 'ctr-address-2']);
    });

    it('choosing "pin" without a version does not save', async () => {
        mockFetchContracts.mockResolvedValue(TARGET_CONTRACTS);
        open({ prefill: { system_id: 'sys-address', agent_card_id: null, name: 'Address Validator' } });
        fireEvent.click(screen.getByTestId('dependency-pin-pinned'));
        save();
        expect(await screen.findByTestId('dependency-error-contract_pin')).toHaveTextContent('Choose the contract version');
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('switching back to follow clears the pin', async () => {
        mockFetchContracts.mockResolvedValue(TARGET_CONTRACTS);
        open({ edge: ADDRESS_EDGE });
        await screen.findByTestId('dependency-pin-status');
        fireEvent.click(screen.getByTestId('dependency-pin-follow'));
        save();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        expect(mockUpdate.mock.calls[0][2]).toMatchObject({ contract_pin: null });
    });
});

describe('DependencyDialog — the outcomes it expects back', () => {
    async function declareFraudScreener() {
        open();
        await screen.findByRole('option', { name: 'Fraud Screener' });
        fireEvent.change(screen.getByTestId('dependency-target'), { target: { value: 'sys-fraud' } });
    }

    it('offers the target’s declared outcomes and sends the ones ticked, sorted', async () => {
        await declareFraudScreener();
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-on'));
        await screen.findByTestId('dependency-expected-outcome-screening_completed.hit');
        expect(mockFetchOutcomes).toHaveBeenCalledWith('sys-fraud');
        fireEvent.click(screen.getByTestId('dependency-expected-outcome-screening_completed.review'));
        fireEvent.click(screen.getByTestId('dependency-expected-outcome-screening_completed.clear'));
        expect(screen.getByTestId('dependency-expected-outcomes-input')).toHaveValue(
            'screening_completed.clear, screening_completed.review',
        );
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({
            expected_outcomes: ['screening_completed.clear', 'screening_completed.review'],
        });
    });

    it('takes typed ids too, and says which the target does not declare', async () => {
        await declareFraudScreener();
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-on'));
        await screen.findByTestId('dependency-expected-outcome-screening_completed.hit');
        fireEvent.change(screen.getByTestId('dependency-expected-outcomes-input'), {
            target: { value: 'screening_completed.maybe, screening_completed' },
        });
        // `screening_completed` alone covers its variants; `.maybe` is not declared.
        expect(screen.getByTestId('dependency-expected-outcomes-unknown')).toHaveTextContent('screening_completed.maybe');
        expect(screen.getByTestId('dependency-expected-outcomes-unknown')).not.toHaveTextContent('screening_completed,');
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({
            expected_outcomes: ['screening_completed', 'screening_completed.maybe'],
        });
    });

    it('refuses a malformed id before it reaches the API', async () => {
        await declareFraudScreener();
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-on'));
        fireEvent.change(screen.getByTestId('dependency-expected-outcomes-input'), {
            target: { value: 'Screening.Hit' },
        });
        save();
        expect(await screen.findByTestId('dependency-error-expected_outcomes')).toHaveTextContent('Screening.Hit');
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('editing keeps the stored list', async () => {
        open({ edge: { ...ADDRESS_EDGE, expected_outcomes: ['address_validated.valid'] } });
        expect(screen.getByTestId('dependency-expected-outcomes-input')).toHaveValue('address_validated.valid');
        save();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        expect(mockUpdate.mock.calls[0][2]).toMatchObject({ expected_outcomes: ['address_validated.valid'] });
    });

    it('"Not declared" clears a stored list to null', async () => {
        open({ edge: { ...ADDRESS_EDGE, expected_outcomes: ['address_validated.valid'] } });
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-off'));
        expect(screen.queryByTestId('dependency-expected-outcomes-input')).toBeNull();
        save();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        expect(mockUpdate.mock.calls[0][2]).toMatchObject({ expected_outcomes: null });
    });

    it('warns that an empty declared list expects nothing', async () => {
        await declareFraudScreener();
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-on'));
        expect(screen.getByTestId('dependency-expected-outcomes')).toHaveTextContent('every answer would be a finding');
    });

    it('still works when the target’s outcomes cannot be read', async () => {
        mockFetchOutcomes.mockRejectedValue(new Error('404'));
        await declareFraudScreener();
        fireEvent.click(screen.getByTestId('dependency-expected-outcomes-on'));
        await waitFor(() => expect(mockFetchOutcomes).toHaveBeenCalled());
        expect(screen.queryByTestId('dependency-expected-outcomes-declared')).toBeNull();
        fireEvent.change(screen.getByTestId('dependency-expected-outcomes-input'), { target: { value: 'x.y' } });
        save();
        await waitFor(() => expect(mockCreate).toHaveBeenCalled());
        expect(mockCreate.mock.calls[0][1]).toMatchObject({ expected_outcomes: ['x.y'] });
    });
});
