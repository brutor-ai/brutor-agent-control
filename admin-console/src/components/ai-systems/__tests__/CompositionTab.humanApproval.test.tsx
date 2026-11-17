/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** Declared human approval, in all three Composition views. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

const mockFetchComposition = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemContracts: vi.fn().mockResolvedValue({ contracts: [] }),
    fetchSystemHealth: vi.fn().mockResolvedValue({ dependency_health: [] }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemComposition: mockFetchComposition,
        fetchAISystemContracts: stableGateway.fetchAISystemContracts,
        fetchSystemHealth: stableGateway.fetchSystemHealth,
    }),
}));

import CompositionTab from '../CompositionTab';
import { COMPOSITION_VIEW_STORAGE_KEY } from '../../../lib/compositionView';
import type { AISystemCompositionView } from '../../../lib/aiSystemTypes';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { callsWhileIdle } from '../../../test/idle';

const mcp = COMPOSITION_PAYLOAD.resources.mcp_servers.find((m) => !m.system)!;
const model = COMPOSITION_PAYLOAD.resources.models[0];

const GATED: AISystemCompositionView = {
    ...COMPOSITION_PAYLOAD,
    human_approval: {
        autonomy_requires_approval: true,
        approval_window_seconds: 3600,
        gates: [
            {
                source: 'mcp_capability',
                scope: 'always',
                lane: 'mcp',
                target: 'submit_decision',
                resource_ids: [mcp.id],
                detail: 'tool submit_decision needs approval',
            },
            {
                source: 'argument_policy',
                scope: 'conditional',
                lane: 'a2a',
                target: 'x',
                resource_ids: [model.id],
                detail: 'Loan ceiling: needs approval when the arguments match',
            },
        ],
    },
};

async function renderTab(payload: AISystemCompositionView) {
    mockFetchComposition.mockResolvedValue(payload);
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={vi.fn()} />);
    await screen.findByTestId('composition-tab');
}

describe('Composition tab — human approval', () => {
    beforeEach(() => localStorage.clear());
    afterEach(cleanup);

    it('Simple graph: always and conditional marks read differently, without relying on colour', async () => {
        await renderTab(GATED);
        const always = screen.getByTestId(`simple-graph-approval-mcp:${mcp.id}`);
        const conditional = screen.getByTestId(`simple-graph-approval-model:${model.id}`);
        expect(always.dataset.scope).toBe('always');
        expect(always).toHaveTextContent('Needs approval');
        expect(always.className).toContain('border-solid');
        expect(conditional.dataset.scope).toBe('conditional');
        expect(conditional).toHaveTextContent('Approval if args match');
        expect(conditional.className).toContain('border-dashed');
    });

    it('the tooltip names the gate and how long a person has', async () => {
        await renderTab(GATED);
        expect(screen.getByTestId(`simple-graph-approval-mcp:${mcp.id}`).title).toBe(
            'tool submit_decision needs approval\nA person has 1 h to decide.'
        );
    });

    it('the system says it, and the header states the same sentence', async () => {
        await renderTab(GATED);
        expect(screen.getByTestId('simple-graph-human-approval')).toHaveTextContent(/2 of \d+ resources · side-effecting agent calls/);
        const header = screen.getByTestId('composition-fact-human-approval');
        expect(header).toHaveTextContent(/2 of \d+ resources · side-effecting agent calls/);
        expect(screen.getByTestId('human-approval-legend')).toBeInTheDocument();
    });

    it('the graph is role=img, so its label carries the gates for assistive technology', async () => {
        await renderTab(GATED);
        expect(screen.getByTestId('composition-simple-graph').getAttribute('aria-label')).toMatch(/Human approval: 2 of/);
    });

    it('a system with no gate shows no mark, no fact and no legend', async () => {
        await renderTab(COMPOSITION_PAYLOAD);
        expect(screen.queryByTestId(`simple-graph-approval-mcp:${mcp.id}`)).toBeNull();
        expect(screen.queryByTestId('composition-fact-human-approval')).toBeNull();
        expect(screen.queryByTestId('simple-graph-human-approval')).toBeNull();
        expect(screen.queryByTestId('human-approval-legend')).toBeNull();
    });

    it('the Diagram marks the same resources', async () => {
        localStorage.setItem(COMPOSITION_VIEW_STORAGE_KEY, 'diagram');
        await renderTab(GATED);
        expect(screen.getByTestId(`approval-mcp:${mcp.id}`).dataset.scope).toBe('always');
        expect(screen.getByTestId(`approval-model:${model.id}`).dataset.scope).toBe('conditional');
        expect(screen.getByTestId('human-approval-legend')).toBeInTheDocument();
    });

    it('the List states the gate in words', async () => {
        localStorage.setItem(COMPOSITION_VIEW_STORAGE_KEY, 'list');
        await renderTab(GATED);
        expect(screen.getByTestId('composition-list')).toHaveTextContent(
            'Human approval: tool submit_decision needs approval'
        );
    });
});

describe('CompositionTab — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        await renderTab(GATED);
        expect(await callsWhileIdle(Object.values(stableGateway))).toBe(0);
    });
});
