/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Member agents show their release in ALL three Composition
 * views: the Diagram and the List as `name@version` + trust badge +
 * approval; the Simple graph as just `name@version` under the system box
 * with a small trust marker (no approval, no other badge).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemContracts: vi.fn().mockResolvedValue({ contracts: [], total: 0, config_drifted: false }),
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
import type { AISystemCompositionView } from '../../../lib/aiSystemTypes';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { callsWhileIdle } from '../../../test/idle';

const setActiveTab = vi.fn();

/** Intake: verified + approved; Scorer: declared + unapproved; a third
 *  member that never declared a release. */
const PAYLOAD: AISystemCompositionView = {
    ...COMPOSITION_PAYLOAD,
    agents: [
        COMPOSITION_PAYLOAD.agents[0],
        {
            ...COMPOSITION_PAYLOAD.agents[1],
            approved_versions: ['1.*'],
            release: {
                name: 'scoring-agent',
                version: '0.9.0',
                build: null,
                trust: 'declared',
                approved: false,
                last_seen_at: '2026-09-27T07:00:00Z',
            },
        },
        {
            id: 'agent-silent',
            name: 'Silent Agent',
            implementation_name: null,
            approved_versions: [],
            require_release: false,
            release: null,
        },
    ],
};

async function renderTab(view: 'simple' | 'diagram' | 'list') {
    mockFetchComposition.mockResolvedValue(PAYLOAD);
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={setActiveTab} />);
    await screen.findByTestId('composition-tab');
    if (view !== 'simple') fireEvent.click(screen.getByTestId(`composition-view-${view}`));
}

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
});

describe.each([
    ['diagram', 'release-'],
    ['list', 'list-release-'],
] as const)('%s view — release, trust and approval per member agent', (view, prefix) => {
    it('shows name@version with its trust badge and approval', async () => {
        await renderTab(view);
        const intake = screen.getByTestId(`${prefix}agent:agent-intake`);
        expect(within(intake).getByTestId('agent-release-label')).toHaveTextContent('loan-intake@1.4.2');
        expect(within(intake).getByTestId('trust-badge')).toHaveAttribute('data-trust', 'verified');
        expect(within(intake).getByTestId('approval-badge')).toHaveAttribute('data-approval', 'approved');

        const scorer = screen.getByTestId(`${prefix}agent:agent-scorer`);
        expect(within(scorer).getByTestId('agent-release-label')).toHaveTextContent('scoring-agent@0.9.0');
        expect(within(scorer).getByTestId('trust-badge')).toHaveAttribute('data-trust', 'declared');
        expect(within(scorer).getByTestId('approval-badge')).toHaveAttribute('data-approval', 'unapproved');
    });

    it('says an agent never declared a release rather than showing nothing', async () => {
        await renderTab(view);
        const silent = screen.getByTestId(`${prefix}agent:agent-silent`);
        expect(within(silent).getByTestId('agent-release-none')).toHaveTextContent('no release declared');
    });
});

describe('Simple graph — just name@version under the system box', () => {
    it('lists each member agent’s release with a small trust marker and nothing more', async () => {
        await renderTab('simple');
        const agents = screen.getByTestId('simple-graph-agents');
        const intake = within(agents).getByTestId('simple-graph-agent-agent:agent-intake');
        expect(intake).toHaveTextContent('loan-intake@1.4.2');
        expect(within(intake).getByTestId('trust-marker')).toHaveAttribute('data-trust', 'verified');
        expect(within(agents).getByTestId('simple-graph-agent-agent:agent-scorer')).toHaveTextContent('scoring-agent@0.9.0');
        // Keep it simple: no approval badge and no full trust badge here.
        expect(within(agents).queryByTestId('approval-badge')).toBeNull();
        expect(within(agents).queryByTestId('trust-badge')).toBeNull();
        // The agents sit inside the system box.
        expect(within(screen.getByTestId('simple-graph-system')).getByTestId('simple-graph-agents')).toBe(agents);
    });

    it('an agent with no release shows its name and no marker', async () => {
        await renderTab('simple');
        const silent = screen.getByTestId('simple-graph-agent-agent:agent-silent');
        expect(silent).toHaveTextContent('Silent Agent');
        expect(within(silent).queryByTestId('trust-marker')).toBeNull();
    });

    it('each release links to the identity’s Implementation section', async () => {
        await renderTab('simple');
        fireEvent.click(within(screen.getByTestId('simple-graph-agent-agent:agent-intake')).getByText('loan-intake@1.4.2'));
        expect(setActiveTab).toHaveBeenLastCalledWith('a2a-identities:agent-intake:implementation');
    });
});

describe('the three views agree on the release', () => {
    it('Simple graph, Diagram and List name the same release for every member agent', async () => {
        const seen: Record<string, string[]> = {};
        for (const view of ['simple', 'diagram', 'list'] as const) {
            await renderTab(view);
            const label = (key: string) => {
                // The label only — the marker's screen-reader word is not part of it.
                if (view === 'simple')
                    return screen.getByTestId(`simple-graph-agent-${key}`).querySelector('.font-mono')?.textContent ?? '';
                const prefix = view === 'diagram' ? 'release-' : 'list-release-';
                const el = screen.getByTestId(`${prefix}${key}`);
                return within(el).queryByTestId('agent-release-label')?.textContent ?? 'none';
            };
            seen[view] = ['agent:agent-intake', 'agent:agent-scorer'].map(label);
            cleanup();
        }
        expect(seen.diagram).toEqual(['loan-intake@1.4.2', 'scoring-agent@0.9.0']);
        expect(seen.list).toEqual(seen.diagram);
        expect(seen.simple).toEqual(seen.diagram);
    });
});

describe('CompositionTab — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        await renderTab('simple');
        expect(await callsWhileIdle(Object.values(stableGateway))).toBe(0);
    });
});
