/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The run overlay's arrangement on the Composition tab. No run: one column
 * (graph, then its connection legend), as always. A run applied on a wide
 * enough tab: two columns — the graph with its connection legend on the
 * left, "Sequence — N actions" with its own legend on the right, the list
 * scrolling on its own under a sticky header, and the graph scaled to its
 * column. Narrower than RUN_COLUMNS_MIN_WIDTH it stacks: graph, then
 * sequence.
 *
 * happy-dom has no layout engine, so widths are stubbed (`clientWidth` per
 * test id) and the ResizeObserver is driven by hand. What a real browser
 * does with the classes — no page scroll, no overlap at 1280 — is outside
 * this suite; here we pin the state switch and the structure.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';

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
import {
    GRAPH_MIN_SCALE,
    RUN_COLUMNS_MIN_WIDTH,
    graphFitScale,
    runOverlayArrangement,
} from '../../../lib/compositionSimpleGraph';
import { COMPOSITION_PAYLOAD } from './fixtures/compositionPayload';
import { OVERLAY_RUN } from './fixtures/runOverlay';
import { callsWhileIdle } from '../../../test/idle';

// ─── Layout stubs ────────────────────────────────────────────────────────────

/** clientWidth by data-testid; anything unlisted reports 0 (= unmeasured). */
const widths: Record<string, number> = {};
const observers: { cb: ResizeObserverCallback; self: ResizeObserver }[] = [];

class StubResizeObserver {
    constructor(cb: ResizeObserverCallback) {
        observers.push({ cb, self: this });
    }
    observe() {}
    unobserve() {}
    disconnect() {}
}

const ownClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');

const resizeTo = (next: Record<string, number>) => {
    Object.assign(widths, next);
    act(() => observers.forEach((o) => o.cb([], o.self)));
};

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    for (const k of Object.keys(widths)) delete widths[k];
    observers.length = 0;
    vi.stubGlobal('ResizeObserver', StubResizeObserver);
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
        configurable: true,
        get(this: HTMLElement) {
            return widths[this.getAttribute('data-testid') ?? ''] ?? 0;
        },
    });
    mockFetchComposition.mockResolvedValue(COMPOSITION_PAYLOAD);
    mockFetchRuns.mockResolvedValue({ runs: [], total: 0, limit: 20, offset: 0 });
    mockFetchRun.mockResolvedValue(OVERLAY_RUN);
});

afterEach(() => {
    vi.unstubAllGlobals();
    // Back to the environment's own clientWidth.
    if (ownClientWidth) Object.defineProperty(HTMLElement.prototype, 'clientWidth', ownClientWidth);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
});

async function renderTab(runId: string | null) {
    render(
        <CompositionTab
            aiSystemId="sys-demo"
            windowHours={168}
            setActiveTab={vi.fn()}
            runId={runId}
            onSelectRun={vi.fn()}
        />
    );
    await screen.findByTestId('composition-tab');
    if (runId) await screen.findByTestId('run-sequence');
}

const layout = () => screen.getByTestId('composition-run-layout');
const graphColumn = () => screen.getByTestId('composition-graph-column');
const sequenceColumn = () => screen.getByTestId('composition-sequence-column');
const headers = () =>
    within(screen.getByTestId('run-sequence-table'))
        .getAllByRole('columnheader')
        .map((h) => h.textContent);

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('without a run', () => {
    it('keeps the single column: graph, then its legend; no sequence column, no scaling', async () => {
        await renderTab(null);
        expect(layout()).toHaveAttribute('data-arrangement', 'single');
        expect(layout()).not.toHaveClass('grid');
        expect(screen.queryByTestId('composition-sequence-column')).toBeNull();
        expect(within(graphColumn()).getByTestId('simple-graph-legend')).toBeInTheDocument();
        expect(screen.getByTestId('composition-simple-graph-region')).toHaveAttribute('data-fit', 'false');
    });

    it('stays single even when the tab is wide', async () => {
        widths['composition-run-layout'] = 1400;
        await renderTab(null);
        expect(layout()).toHaveAttribute('data-arrangement', 'single');
        expect(layout()).not.toHaveClass('grid');
    });
});

describe('a run applied on a wide tab', () => {
    beforeEach(async () => {
        widths['composition-run-layout'] = 976; // 1280 viewport − 256 sidebar − 2 × 24 gutter
        await renderTab('run-overlay-1');
    });

    it('splits into two columns: graph + connection legend left, sequence + its legend right', () => {
        expect(layout()).toHaveAttribute('data-arrangement', 'columns');
        expect(layout()).toHaveClass('grid');
        expect(layout().children).toHaveLength(2);
        expect(layout().children[0]).toBe(graphColumn());
        expect(layout().children[1]).toBe(sequenceColumn());

        expect(within(graphColumn()).getByTestId('composition-simple-graph')).toBeInTheDocument();
        expect(within(graphColumn()).getByTestId('simple-graph-legend')).toBeInTheDocument();
        expect(within(graphColumn()).queryByTestId('run-sequence')).toBeNull();
        expect(within(graphColumn()).queryByTestId('run-overlay-legend')).toBeNull();

        expect(within(sequenceColumn()).getByTestId('run-sequence')).toBeInTheDocument();
        expect(within(sequenceColumn()).getByTestId('run-overlay-legend')).toBeInTheDocument();
        expect(within(sequenceColumn()).queryByTestId('simple-graph-legend')).toBeNull();
        // The legend sits under the list, not inside its scroll box.
        const scroll = screen.getByTestId('run-sequence-scroll');
        expect(scroll).not.toContainElement(screen.getByTestId('run-overlay-legend'));
        expect(
            screen.getByTestId('run-sequence').compareDocumentPosition(screen.getByTestId('run-overlay-legend')) &
                Node.DOCUMENT_POSITION_FOLLOWING
        ).toBeTruthy();
    });

    it('the list scrolls on its own under a sticky "Sequence — N actions" header', () => {
        const header = screen.getByTestId('run-sequence-header');
        expect(header).toHaveTextContent('Sequence — 11 actions');
        expect(header).toHaveAttribute('data-sticky', 'true');
        expect(header).toHaveClass('sticky', 'top-0');
        const scroll = screen.getByTestId('run-sequence-scroll');
        expect(scroll).toHaveClass('scroll-region');
        expect(scroll).toContainElement(header);
        expect(scroll).toContainElement(screen.getByTestId('run-sequence-table'));
        // The column takes the graph column's height (absolute body in a
        // stretched grid cell) with a floor, so a long run cannot push the
        // graph off screen.
        expect(sequenceColumn()).toHaveClass('relative', 'min-h-[32rem]');
        // …and while the page scrolls past a tall graph, the list and its
        // legend stay in view, at most a viewport tall.
        expect(screen.getByTestId('composition-sequence-body')).toHaveClass('sticky', 'max-h-[calc(100vh-2rem)]');
        expect(screen.getByTestId('run-sequence')).toHaveAttribute('data-layout', 'column');
    });

    it('fits the list to its column: turn and kind fold into the target’s second line', () => {
        expect(headers()).toEqual(['#', 'Target', 'Outcome', 'Duration']);
        const detail = screen.getByTestId('run-seq-detail-1');
        expect(detail).toHaveTextContent('MCP tool');
        expect(detail).toHaveTextContent(/turn \S+/);
    });

    it('scales the graph to its column instead of scrolling the page', () => {
        const graph = screen.getByTestId('composition-simple-graph');
        const layoutW = parseFloat(graph.style.width);
        expect(screen.getByTestId('composition-simple-graph-region')).toHaveAttribute('data-fit', 'true');
        const column = Math.round(layoutW * 0.7);
        resizeTo({ 'composition-simple-graph-region': column });
        const sizer = screen.getByTestId('composition-simple-graph-sizer');
        const scale = Number(sizer.getAttribute('data-scale'));
        expect(scale).toBeCloseTo(column / layoutW, 5);
        expect(parseFloat(sizer.style.width)).toBeCloseTo(column, 0);
        expect(graph.style.transform).toBe(`scale(${scale})`);
        // The geometry stays in layout px.
        expect(parseFloat(graph.style.width)).toBe(layoutW);
    });

    it('hover sync still works across the columns, both ways', () => {
        const chip = screen.getByTestId('run-chip-mcp:mcp-core');
        const badge3 = within(chip).getAllByTestId(/^run-badge-/)[1];
        fireEvent.mouseEnter(screen.getByTestId('run-seq-row-3'));
        expect(badge3).toHaveAttribute('data-active', 'true');
        fireEvent.mouseLeave(screen.getByTestId('run-seq-row-3'));
        expect(badge3).toHaveAttribute('data-active', 'false');
        act(() => screen.getByTestId('run-seq-row-5a').focus());
        expect(within(screen.getByTestId('run-inset-dep:dep-fraud')).getByTestId('run-badge-5a')).toHaveAttribute(
            'data-active',
            'true'
        );
        fireEvent.mouseEnter(within(chip).getAllByTestId(/^run-badge-/)[2]);
        expect(screen.getByTestId('run-seq-row-7')).toHaveAttribute('data-active', 'true');
    });
});

describe('a run applied on a narrow tab', () => {
    beforeEach(async () => {
        widths['composition-run-layout'] = RUN_COLUMNS_MIN_WIDTH - 1;
        await renderTab('run-overlay-1');
    });

    it('stacks: graph + its legend, then the full list + its legend; no sticky header, no own scroll box', () => {
        expect(layout()).toHaveAttribute('data-arrangement', 'stacked');
        expect(layout()).not.toHaveClass('grid');
        expect(within(graphColumn()).getByTestId('simple-graph-legend')).toBeInTheDocument();
        expect(within(sequenceColumn()).getByTestId('run-overlay-legend')).toBeInTheDocument();
        expect(screen.getByTestId('run-sequence')).toHaveAttribute('data-layout', 'full');
        expect(screen.queryByTestId('run-sequence-scroll')).toBeNull();
        expect(screen.getByTestId('run-sequence-header')).toHaveAttribute('data-sticky', 'false');
        expect(screen.getByTestId('run-sequence-header')).toHaveTextContent('Sequence — 11 actions');
        expect(headers()).toEqual(['#', 'Turn', 'Kind', 'Target', 'Outcome', 'Duration']);
    });

    it('switches to columns when the tab widens, and back', () => {
        resizeTo({ 'composition-run-layout': RUN_COLUMNS_MIN_WIDTH });
        expect(layout()).toHaveAttribute('data-arrangement', 'columns');
        expect(layout()).toHaveClass('grid');
        resizeTo({ 'composition-run-layout': 700 });
        expect(layout()).toHaveAttribute('data-arrangement', 'stacked');
        expect(layout()).not.toHaveClass('grid');
    });

    it('hover sync works stacked too', () => {
        fireEvent.mouseEnter(screen.getByTestId('run-seq-row-6'));
        expect(within(screen.getByTestId('run-chip-model:llm-sonnet')).getByTestId('run-badge-6')).toHaveAttribute(
            'data-active',
            'true'
        );
    });
});

describe('the arrangement and fit rules', () => {
    it('columns from RUN_COLUMNS_MIN_WIDTH up, and when unmeasured; stacked below', () => {
        expect(runOverlayArrangement(null)).toBe('columns');
        expect(runOverlayArrangement(RUN_COLUMNS_MIN_WIDTH)).toBe('columns');
        expect(runOverlayArrangement(RUN_COLUMNS_MIN_WIDTH - 1)).toBe('stacked');
        // 1280 with the sidebar must be two columns.
        expect(runOverlayArrangement(1280 - 256 - 48)).toBe('columns');
    });

    it('never scales up, never below the floor, and leaves unknown room alone', () => {
        expect(graphFitScale(null, 900)).toBe(1);
        expect(graphFitScale(1200, 900)).toBe(1);
        expect(graphFitScale(600, 900)).toBeCloseTo(600 / 900, 5);
        expect(graphFitScale(100, 900)).toBe(GRAPH_MIN_SCALE);
    });
});

describe('CompositionTab — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        await renderTab(null);
        expect(await callsWhileIdle(Object.values(stableGateway))).toBe(0);
    });
});
