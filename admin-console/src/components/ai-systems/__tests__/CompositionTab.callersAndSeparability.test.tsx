/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composition tab:
 *   * the header says whether the system ENFORCES its accepted callers
 *     (the gateway refuses others) or treats them as advisory;
 *   * separability declarations show on BOTH systems — the ones this system
 *     declares and the ones callers declare on it — with the sealed record,
 *     or plainly "not sealed";
 *   * an external node carries its supplier and the A2A task-state mix seen
 *     at the gateway boundary (from the system's dependency health).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();
const mockFetchContracts = vi.fn();
const mockFetchHealth = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemComposition: mockFetchComposition,
        fetchAISystemContracts: mockFetchContracts,
        fetchSystemHealth: mockFetchHealth,
        fetchAISystems: vi.fn().mockResolvedValue({ systems: [] }),
        listAgentCards: vi.fn().mockResolvedValue({ cards: [] }),
    }),
}));

import CompositionTab from '../CompositionTab';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import type { AISystemCompositionView } from '../../../lib/aiSystemTypes';

const HEALTH = {
    dependency_health: [
        {
            edge_id: 'dep-bureau',
            system_id: null,
            name: 'Credit Bureau Agent',
            role: 'external',
            criticality: 'degradable',
            contributes_to_decision: true,
            status: 'assumed',
            failing_signal: null,
            via_system_id: null,
            via_system_name: null,
            basis: 'assumed',
            caps_root: false,
            note: null,
            fallback_check: null,
            boundary: {
                calls: 15,
                errors: 2,
                error_rate: 0.133,
                p95_latency_ms: 420,
                last_seen_at: null,
                task_states: { failed: 2, completed: 12, input_required: 1 },
            },
            assumptions: {},
        },
    ],
};

async function renderTab(payload: AISystemCompositionView = COMPOSITION_PAYLOAD) {
    mockFetchComposition.mockResolvedValue(payload);
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={vi.fn()} />);
    const tab = await screen.findByTestId('composition-tab');
    // The default view is the Simple graph; these tests read the Diagram.
    fireEvent.click(screen.getByTestId('composition-view-diagram'));
    return tab;
}

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mockFetchContracts.mockResolvedValue({ contracts: [], total: 0, config_drifted: false, active_version: 2 });
    mockFetchHealth.mockResolvedValue(HEALTH);
});

describe('accepted callers — enforced vs advisory', () => {
    it('an enforcing service says the gateway refuses other callers', async () => {
        await renderTab();
        const line = screen.getByTestId('composition-accepted-callers');
        expect(line).toHaveAttribute('data-enforced', 'true');
        expect(line).toHaveTextContent('Accepts only listed callers — enforced');
    });

    it('a non-enforcing one says unaccepted callers are advisory', async () => {
        await renderTab({ ...COMPOSITION_PAYLOAD, system: { ...COMPOSITION_PAYLOAD.system, enforce_accepted_callers: false } });
        const line = screen.getByTestId('composition-accepted-callers');
        expect(line).toHaveAttribute('data-enforced', 'false');
        expect(line).toHaveTextContent('advisory');
    });

    it('an unaccepted caller node says it is refused when enforced', async () => {
        await renderTab();
        fireEvent.click(screen.getByTestId('composition-view-list'));
        expect(screen.getByTestId('composition-list-row-caller:sys-shadow')).toHaveTextContent('no — refused at the gateway');
        expect(screen.getByTestId('composition-list-row-caller:sys-portal')).toHaveTextContent('Accepted here: yes');
    });
});

describe('separability declarations — both systems', () => {
    it('lists the claim this system declares, sealed, and the one declared on it, unsealed', async () => {
        await renderTab();
        const own = screen.getByTestId('separability-dep-address');
        expect(own).toHaveAttribute('data-side', 'declares');
        expect(own).toHaveAttribute('data-sealed', 'true');
        expect(own).toHaveTextContent('Declares Address Validator separable');
        expect(own).toHaveTextContent('rec-sep-address-0001');

        const onMe = screen.getByTestId('separability-dep-portal-demo');
        expect(onMe).toHaveAttribute('data-side', 'declared_on');
        expect(onMe).toHaveTextContent('Declared separable by Broker Portal Assistant');
        expect(onMe).toHaveTextContent('not sealed');
    });

    it('renders nothing when no claim touches the system', async () => {
        await renderTab({ ...COMPOSITION_PAYLOAD, system: { ...COMPOSITION_PAYLOAD.system, separability_declarations: [] } });
        expect(screen.queryByTestId('composition-separability')).toBeNull();
    });

    it('the separable edge node says its claim is sealed', async () => {
        await renderTab();
        const sealedOn = '2026-09-21';
        expect(screen.getByTestId('composition-node-dep:dep-address')).toHaveTextContent(`separable · sealed ${sealedOn}`);
    });
});

describe('external nodes — supplier and boundary task states', () => {
    it('shows the supplier and the task-state mix, most frequent first', async () => {
        await renderTab();
        const bureau = screen.getByTestId('composition-node-dep:dep-bureau');
        expect(bureau).toHaveTextContent('Nordic Credit Bureau AB');
        await waitFor(() => expect(bureau).toHaveTextContent('12 completed · 2 failed · 1 input required'));
        expect(mockFetchHealth).toHaveBeenCalledWith('sys-demo');
    });

    it('a shared-service node gets no task-state fact', async () => {
        await renderTab();
        await waitFor(() => expect(screen.getByTestId('composition-node-dep:dep-bureau')).toHaveTextContent('Task states'));
        expect(within(screen.getByTestId('composition-node-dep:dep-fraud')).queryByText('Task states')).toBeNull();
    });

    it('a failed health load leaves the node without it, not broken', async () => {
        mockFetchHealth.mockRejectedValue(new Error('down'));
        await renderTab();
        const bureau = screen.getByTestId('composition-node-dep:dep-bureau');
        expect(bureau).toHaveTextContent('Nordic Credit Bureau AB');
        expect(bureau).not.toHaveTextContent('Task states');
    });
});
