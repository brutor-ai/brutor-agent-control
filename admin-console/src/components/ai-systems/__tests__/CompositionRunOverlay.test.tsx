/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Composition tab's run overlay, rendered: pick a run above the Simple
 * graph and its actions appear as numbered badges on the connectors they
 * travelled, the delegate's own steps nested under the delegation (5a, 5b),
 * untouched items dimmed, undeclared targets ringed, and a sequence list
 * that is the graph's accessible equivalent — hover or focus a row and its
 * badge lights up, and the other way round. Metadata only.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();
const mockFetchRuns = vi.fn();
const mockFetchRun = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemContracts: vi.fn().mockResolvedValue({ contracts: [], total: 0, config_drifted: false, active_version: 2 }),
    fetchSystemHealth: vi.fn().mockResolvedValue({ dependency_health: [] }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemComposition: mockFetchComposition,
        fetchAISystemContracts: stableGateway.fetchAISystemContracts,
        fetchSystemHealth: stableGateway.fetchSystemHealth,
        fetchAISystemRuns: mockFetchRuns,
        fetchRun: mockFetchRun,
    }),
}));

import CompositionTab from '../CompositionTab';
import { COMPOSITION_VIEW_STORAGE_KEY } from '../../../lib/compositionView';
import { RUN_OUTCOME_META } from '../../../lib/compositionConnection';
import type { RunSummary } from '../../../lib/aiSystemTypes';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { OVERLAY_RUN, SENTINEL, crowdedRun } from './fixtures/runOverlay';
import { callsWhileIdle } from '../../../test/idle';

const summary = (id: string, started: string, extra: Partial<RunSummary> = {}): RunSummary => ({
    ...OVERLAY_RUN,
    actions: undefined,
    id,
    started_at: started,
    ...extra,
} as RunSummary);

const RUNS: RunSummary[] = [
    summary('run-overlay-1', '2026-09-29T08:41:30Z'),
    summary('run-older', '2026-09-29T08:30:00Z', {
        terminal_state: 'errored',
        subject_id: null,
        client_label: 'loan-portal',
        action_count: 4,
    }),
];

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mockFetchComposition.mockResolvedValue(COMPOSITION_PAYLOAD);
    mockFetchRuns.mockResolvedValue({ runs: RUNS, total: 2, limit: 20, offset: 0 });
    mockFetchRun.mockResolvedValue(OVERLAY_RUN);
});

async function renderTab(props: { runId?: string | null; onSelectRun?: (id: string | null) => void } = {}) {
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={vi.fn()} {...props} />);
    await screen.findByTestId('composition-tab');
}

const graph = () => screen.getByTestId('composition-simple-graph');
const chip = (itemKey: string) => screen.getByTestId(`run-chip-${itemKey}`);
const chipTexts = (itemKey: string) =>
    within(chip(itemKey))
        .getAllByTestId(/^run-badge-/)
        .map((b) => b.textContent);

async function applyFromPicker(runId = 'run-overlay-1') {
    fireEvent.click(screen.getByTestId('run-picker-toggle'));
    fireEvent.click(await screen.findByTestId(`run-option-${runId}`));
    await screen.findByTestId('run-sequence');
}

describe('the run picker', () => {
    it('lists recent runs newest first — time, subject, outcome, release, actions — and applies one', async () => {
        await renderTab();
        expect(graph()).toHaveAttribute('data-run', '');
        fireEvent.click(screen.getByTestId('run-picker-toggle'));
        const list = await screen.findByTestId('run-picker-list');
        const options = within(list).getAllByRole('option');
        expect(options.map((o) => o.getAttribute('data-testid'))).toEqual(['run-option-run-overlay-1', 'run-option-run-older']);
        expect(options[0]).toHaveTextContent('application APP-1001');
        expect(options[0]).toHaveTextContent('Completed');
        expect(options[0]).toHaveTextContent('loan-intake@1.4.2');
        expect(options[0]).toHaveTextContent('11 actions');
        expect(options[1]).toHaveTextContent('via loan-portal');
        // All time, paged, newest first from the server.
        expect(mockFetchRuns).toHaveBeenCalledWith('sys-demo', expect.objectContaining({ period_hours: 0, offset: 0 }));

        fireEvent.click(options[0]);
        await screen.findByTestId('run-sequence');
        expect(mockFetchRun).toHaveBeenCalledWith('run-overlay-1');
        expect(graph()).toHaveAttribute('data-run', 'run-overlay-1');
        expect(screen.queryByTestId('run-picker-popover')).toBeNull();
    });

    it('searches the server and pages with Load more', async () => {
        mockFetchRuns.mockResolvedValue({ runs: RUNS, total: 45, limit: 20, offset: 0 });
        await renderTab();
        fireEvent.click(screen.getByTestId('run-picker-toggle'));
        await screen.findByTestId('run-option-run-older');
        expect(screen.getByTestId('run-picker-count')).toHaveTextContent('2 of 45 runs');
        fireEvent.click(screen.getByTestId('run-picker-more'));
        await waitFor(() =>
            expect(mockFetchRuns).toHaveBeenLastCalledWith('sys-demo', expect.objectContaining({ limit: 40 }))
        );
        fireEvent.change(screen.getByTestId('run-picker-search'), { target: { value: 'APP-1001' } });
        await waitFor(() =>
            expect(mockFetchRuns).toHaveBeenLastCalledWith('sys-demo', expect.objectContaining({ q: 'APP-1001', limit: 20 }))
        );
    });

    it('is keyboard operable: arrows move, Enter applies, Escape closes', async () => {
        await renderTab();
        fireEvent.click(screen.getByTestId('run-picker-toggle'));
        await screen.findByTestId('run-option-run-older');
        const input = screen.getByTestId('run-picker-search');
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(screen.getByTestId('run-option-run-older')).toHaveAttribute('aria-selected', 'true');
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(mockFetchRun).toHaveBeenCalledWith('run-older'));
    });

    it('Clear returns to the plain graph', async () => {
        await renderTab();
        await applyFromPicker();
        fireEvent.click(screen.getByTestId('run-picker-clear'));
        expect(screen.queryByTestId('run-sequence')).toBeNull();
        expect(screen.queryAllByTestId(/^run-chip-/)).toHaveLength(0);
        expect(graph()).toHaveAttribute('data-run', '');
    });
});

describe('the overlay on the Simple graph', () => {
    beforeEach(async () => {
        await renderTab();
        await applyFromPicker();
    });

    it('numbers each action on the connector it travelled; a reused node lists all its numbers', () => {
        expect(chipTexts('mcp:mcp-core')).toEqual(['1', '3', '7!']);
        expect(chipTexts('mcp:mcp-bureau')).toEqual([`2${RUN_OUTCOME_META.held.glyph}`]);
        expect(chipTexts('skill:skill-affordability')).toEqual(['4']);
        expect(chipTexts('dep:dep-fraud')).toEqual(['5']);
        expect(chipTexts('model:llm-sonnet')).toEqual(['6']);
        expect(chipTexts('kb:kb-policy')).toEqual(['6']);
    });

    it('nests the delegate’s own steps (5a, 5b) under the delegation', () => {
        const inset = screen.getByTestId('run-inset-dep:dep-fraud');
        expect(inset).toHaveTextContent('Inside Fraud Screener');
        expect(within(inset).getAllByTestId(/^run-inset-row-/).map((r) => r.getAttribute('data-testid'))).toEqual([
            'run-inset-row-5a',
            'run-inset-row-5b',
        ]);
        expect(within(inset).getByTestId('run-inset-row-5a')).toHaveTextContent('gpt-5-mini');
        // The delegate's model call is not drawn on the root's line to that model.
        expect(screen.queryByTestId('run-chip-model:llm-mini')).toBeNull();
    });

    it('colours each badge by its outcome through THE status mapping, with a hold marker', () => {
        const badge = (item: string, n: string) =>
            within(chip(item)).getAllByTestId(/^run-badge-/).find((b) => b.getAttribute('data-numbers') === n) as HTMLElement;
        const answered = badge('mcp:mcp-core', '1');
        const error = badge('mcp:mcp-core', '7');
        const held = badge('mcp:mcp-bureau', '2');
        const blocked = within(screen.getByTestId('run-inset-dep:dep-fraud')).getByTestId('run-badge-5b');
        expect([answered, error, blocked, held].map((b) => b.getAttribute('data-status'))).toEqual(['ok', 'degraded', 'down', 'down']);
        expect(answered).toHaveClass('composition-connection-ok');
        expect(error).toHaveClass('composition-connection-degraded');
        expect(blocked).toHaveClass('composition-connection-down');
        expect(held).toHaveClass('composition-connection-down');
        // Never colour alone: a glyph and the outcome in words.
        expect(held.getAttribute('data-glyph')).toBe(RUN_OUTCOME_META.held.glyph);
        expect(blocked.getAttribute('data-glyph')).toBe(RUN_OUTCOME_META.blocked.glyph);
        expect(held.getAttribute('title')).toContain('Held for approval');
        expect(within(screen.getByTestId('run-seq-row-2')).getByText(/Held for approval/)).toBeInTheDocument();
    });

    it('dims the items, lines and lanes the run did not touch', () => {
        for (const key of ['dep:dep-bureau', 'dep:dep-address', 'model:llm-mini']) {
            expect(screen.getByTestId(`simple-graph-item-${key}`)).toHaveAttribute('data-dimmed', 'true');
            expect(screen.getByTestId(`simple-graph-line-${key}`)).toHaveAttribute('data-dimmed', 'true');
        }
        for (const key of ['mcp:mcp-core', 'dep:dep-fraud', 'model:llm-sonnet', 'kb:kb-policy']) {
            expect(screen.getByTestId(`simple-graph-item-${key}`)).toHaveAttribute('data-dimmed', 'false');
        }
        expect(screen.getByTestId('simple-graph-line-system')).toHaveAttribute('data-dimmed', 'false');
        expect(screen.getByTestId('simple-graph-lane-mcp')).toHaveAttribute('data-dimmed', 'false');
    });

    it('highlights a target the declared composition lacks', () => {
        const item = screen.getByTestId('simple-graph-item-run:mcp:srv-shadow-crm');
        expect(item).toHaveAttribute('data-undeclared', 'true');
        expect(item).toHaveClass('composition-run-undeclared');
        expect(item).toHaveTextContent('Shadow CRM');
        expect(item).toHaveTextContent('Not in the declared composition');
        expect(chipTexts('run:mcp:srv-shadow-crm')).toEqual([`8${RUN_OUTCOME_META.blocked.glyph}`]);
    });

    it('lists every action under step headers, and keeps role="img" with a summary', () => {
        const table = screen.getByTestId('run-sequence-table');
        // Beside the graph (the wide arrangement) turn and kind fold into the
        // target's second line; stacked under it they are columns of their own
        // (CompositionRunOverlay.layout.test.tsx).
        expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(
            ['#', 'Target', 'Outcome', 'Duration']
        );
        expect(screen.getByTestId('run-seq-detail-5')).toHaveTextContent('A2A call');
        expect(within(table).getAllByRole('rowheader').map((h) => h.textContent)).toEqual([
            'Step: Gather the application facts',
            'Step: Assess risk and fraud',
            'Step: Decide and record',
        ]);
        const rows = Array.from(table.querySelectorAll('tr[data-run-row]'));
        expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual(
            ['1', '2', '3', '4', '5', '5a', '5b', '6', '7', '8', '9'].map((n) => `run-seq-row-${n}`)
        );
        expect(screen.getByTestId('run-seq-row-5')).toHaveTextContent('screening.fraud_sanctions');
        expect(screen.getByTestId('run-seq-row-5')).toHaveTextContent('1.3 s');
        expect(screen.getByTestId('run-seq-row-4')).toHaveTextContent('affordability-check · affordability.py');
        expect(graph()).toHaveAttribute('role', 'img');
        expect(graph().getAttribute('aria-label')).toMatch(/Run run-overlay-1 applied: 11 actions \(2 inside delegations\)/);
        expect(graph().getAttribute('aria-label')).toMatch(/1 not in the declared composition/);
    });

    it('hovering or focusing a row lights its badge, and hovering a badge lights its row', () => {
        const badge3 = within(chip('mcp:mcp-core')).getAllByTestId(/^run-badge-/)[1];
        expect(badge3).toHaveAttribute('data-active', 'false');
        fireEvent.mouseEnter(screen.getByTestId('run-seq-row-3'));
        expect(badge3).toHaveAttribute('data-active', 'true');
        fireEvent.mouseLeave(screen.getByTestId('run-seq-row-3'));
        expect(badge3).toHaveAttribute('data-active', 'false');

        act(() => screen.getByTestId('run-seq-row-5a').focus());
        expect(within(screen.getByTestId('run-inset-dep:dep-fraud')).getByTestId('run-badge-5a')).toHaveAttribute('data-active', 'true');

        fireEvent.mouseEnter(within(chip('mcp:mcp-core')).getAllByTestId(/^run-badge-/)[2]);
        expect(screen.getByTestId('run-seq-row-7')).toHaveAttribute('data-active', 'true');
        expect(screen.getByTestId('run-seq-row-3')).toHaveAttribute('data-active', 'false');
    });

    it('rows are keyboard navigable with the arrow keys', () => {
        const first = screen.getByTestId('run-seq-row-1');
        act(() => first.focus());
        fireEvent.keyDown(first, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(screen.getByTestId('run-seq-row-2'));
        fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(first);
    });

    it('is metadata only: no argument, prompt, response or error text is rendered', () => {
        const tab = screen.getByTestId('composition-tab');
        expect(tab.textContent).not.toContain(SENTINEL);
        expect(tab.innerHTML).not.toContain(SENTINEL);
    });
});

describe('deep links', () => {
    it('a routed run opens on the Simple graph with that run applied, whatever view was stored', async () => {
        window.localStorage.setItem(COMPOSITION_VIEW_STORAGE_KEY, 'list');
        const onSelectRun = vi.fn();
        await renderTab({ runId: 'run-overlay-1', onSelectRun });
        await screen.findByTestId('run-sequence');
        expect(graph()).toHaveAttribute('data-run', 'run-overlay-1');
        expect(screen.queryByTestId('composition-list')).toBeNull();
        // Controlled: choosing / clearing goes through the route.
        fireEvent.click(screen.getByTestId('run-picker-clear'));
        expect(onSelectRun).toHaveBeenCalledWith(null);
    });
});

describe('a crowded run', () => {
    it('collapses a line’s numbers to a range and the list still shows all 60', async () => {
        mockFetchRun.mockResolvedValue(
            crowdedRun(60, (i) =>
                i >= 7 && i <= 12
                    ? { proxy_type: 'llm', model: 'claude-sonnet-5', target_kind: 'model', target_id: 'llm-sonnet' }
                    : { target_kind: 'mcp', target_id: 'srv-core', capability: 'applications_get' }
            )
        );
        await renderTab({ runId: 'run-crowded', onSelectRun: vi.fn() });
        await screen.findByTestId('run-sequence');
        expect(chipTexts('model:llm-sonnet')).toEqual(['7–12']);
        const core = chipTexts('mcp:mcp-core');
        expect(core).toContain('1–6');
        expect(core).toContain('13–60');
        expect(screen.getByTestId('run-sequence-table').querySelectorAll('tr[data-run-row]')).toHaveLength(60);
        fireEvent.mouseEnter(screen.getByTestId('run-seq-row-9'));
        expect(within(chip('model:llm-sonnet')).getByTestId('run-badge-7–12')).toHaveAttribute('data-active', 'true');
    });
});

describe('CompositionTab — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        await renderTab();
        expect(await callsWhileIdle(Object.values(stableGateway))).toBe(0);
    });
});
