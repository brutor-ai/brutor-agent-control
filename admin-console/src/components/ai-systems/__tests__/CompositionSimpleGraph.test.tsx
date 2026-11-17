/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Composition tab — the "Simple graph" view,
 * HORIZONTAL: the AI System on the left → the Brutor Gateway in the
 * middle (Policies · Limits · Guardrails, then one row per non-empty lane)
 * → on the right, an icon per thing the system uses, grouped by lane. Every
 * connector line is coloured AND styled by its connection status. It is the
 * default view and the viewer's choice is remembered per browser.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mockFetchComposition = vi.fn();
const mockFetchContracts = vi.fn();
const mockFetchHealth = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemComposition: mockFetchComposition,
        fetchAISystemContracts: mockFetchContracts,
        fetchSystemHealth: mockFetchHealth,
    }),
}));

import CompositionTab from '../CompositionTab';
import { COMPOSITION_VIEW_STORAGE_KEY } from '../../../lib/compositionView';
import { buildCompositionModel } from '../../../lib/composition';
import { CONNECTION_STATUS_META } from '../../../lib/compositionConnection';
import {
    SIMPLE_GRAPH_SIZES,
    buildSimpleGraph,
    layoutSimpleGraph,
} from '../../../lib/compositionSimpleGraph';
import type { AISystemCompositionView, CompositionConnectionStatus } from '../../../lib/aiSystemTypes';
import { COMPOSITION_PAYLOAD, conn } from './fixtures/compositionPayload';

const setActiveTab = vi.fn();

async function renderTab(payload: AISystemCompositionView = COMPOSITION_PAYLOAD) {
    mockFetchComposition.mockResolvedValue(payload);
    render(<CompositionTab aiSystemId="sys-demo" windowHours={168} setActiveTab={setActiveTab} />);
    await screen.findByTestId('composition-tab');
}

const graph = () => screen.getByTestId('composition-simple-graph');
const lanes = () => graph().getAttribute('data-lanes')?.split(',').filter(Boolean) ?? [];
const items = () => screen.getAllByTestId(/^simple-graph-item-/);
/** The captions of a lane's items, top to bottom. */
const itemsOf = (lane: string) =>
    items()
        .filter((el) => el.getAttribute('data-lane') === lane)
        // An item is a link to its page, or plain text where this console has
        // no such page (a knowledge base, with nothing contributed).
        .map((el) => (within(el).queryByRole('button') ?? el.querySelector('.font-medium'))?.textContent);
const num = (el: HTMLElement, attr: string) => Number(el.getAttribute(attr));
const line = (key: string) => screen.getByTestId(`simple-graph-line-${key}`);
const lineTitle = (key: string) => line(key).querySelector('title')?.textContent ?? '';

/** The Brutor Demo System as the owner sees it: 2 models, 2 MCP servers
 *  plus the platform's Agent Skills server, 1 skill, 1 delegation agent. */
const DEMO: AISystemCompositionView = {
    ...COMPOSITION_PAYLOAD,
    resources: { ...COMPOSITION_PAYLOAD.resources, knowledge_bases: [] },
    dependencies: COMPOSITION_PAYLOAD.dependencies.filter((d) => d.id === 'dep-fraud'),
    observed_undeclared: [],
};

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    mockFetchContracts.mockResolvedValue({ contracts: [], total: 0, config_drifted: false, active_version: 2 });
    mockFetchHealth.mockResolvedValue({ dependency_health: [] });
});

describe('Simple graph — the default view, remembered per browser', () => {
    it('is the default, and the view switch reads Simple graph, Diagram, List', async () => {
        await renderTab();
        expect(graph()).toBeInTheDocument();
        expect(screen.queryByTestId('composition-diagram')).toBeNull();
        expect(screen.queryByTestId('composition-list')).toBeNull();
        const buttons = within(screen.getByRole('group', { name: 'Composition view' })).getAllByRole('button');
        expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Simple graph', 'Diagram', 'List']);
        expect(screen.getByTestId('composition-view-simple')).toHaveAttribute('aria-pressed', 'true');
    });

    it('remembers the viewer’s choice across renders', async () => {
        await renderTab();
        fireEvent.click(screen.getByTestId('composition-view-list'));
        expect(window.localStorage.getItem(COMPOSITION_VIEW_STORAGE_KEY)).toBe('list');
        cleanup();

        await renderTab();
        expect(screen.getByTestId('composition-list')).toBeInTheDocument();
        expect(screen.queryByTestId('composition-simple-graph')).toBeNull();

        fireEvent.click(screen.getByTestId('composition-view-simple'));
        expect(window.localStorage.getItem(COMPOSITION_VIEW_STORAGE_KEY)).toBe('simple');
    });

    it('ignores a stored value it does not know, and survives storage that throws', async () => {
        window.localStorage.setItem(COMPOSITION_VIEW_STORAGE_KEY, 'mermaid');
        await renderTab();
        expect(graph()).toBeInTheDocument();
        cleanup();

        const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
            throw new Error('SecurityError');
        });
        const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
            throw new Error('QuotaExceededError');
        });
        try {
            await renderTab();
            expect(graph()).toBeInTheDocument();
            fireEvent.click(screen.getByTestId('composition-view-diagram'));
            expect(screen.getByTestId('composition-diagram')).toBeInTheDocument();
        } finally {
            get.mockRestore();
            set.mockRestore();
        }
    });
});

describe('Simple graph — left to right', () => {
    it('Brutor Demo System: system, gateway rules, four lanes, the names behind them', async () => {
        await renderTab(DEMO);
        const sys = screen.getByTestId('simple-graph-system');
        expect(sys).toHaveTextContent('Brutor Demo System');
        expect(sys).toHaveTextContent('Agent');
        // The member agents' releases under the system icon.
        expect(within(sys).getByTestId('simple-graph-agents')).toHaveTextContent('loan-intake@1.4.2');

        // Guardrails are policies of kind 'guardrail', counted separately.
        expect(screen.getByTestId('simple-graph-count-policies')).toHaveTextContent('Policies 2');
        expect(screen.getByTestId('simple-graph-count-limits')).toHaveTextContent('Limits 2');
        expect(screen.getByTestId('simple-graph-count-guardrails')).toHaveTextContent('Guardrails 1');

        expect(lanes()).toEqual(['llm', 'mcp', 'a2a', 'skills']);
        expect(itemsOf('llm')).toEqual(['Claude Sonnet 5', 'GPT-5 mini']);
        expect(itemsOf('mcp')).toEqual(['Loan Core', 'Bureau Lookup']);
        expect(itemsOf('a2a')).toEqual(['Fraud Screener']);
        expect(itemsOf('skills')).toEqual(['affordability-check']);
    });

    it('is horizontal: the system on the left, the gateway in the middle, what it uses on the right', async () => {
        await renderTab();
        expect(graph()).toHaveAttribute('data-layout', 'horizontal');
        const sys = screen.getByTestId('simple-graph-system');
        const gw = screen.getByTestId('simple-graph-gateway');
        expect(sys).toHaveAttribute('data-column', 'left');
        expect(gw).toHaveAttribute('data-column', 'middle');
        const gwRight = num(gw, 'data-x') + SIMPLE_GRAPH_SIZES.gatewayW;
        expect(num(sys, 'data-x') + SIMPLE_GRAPH_SIZES.systemW).toBeLessThan(num(gw, 'data-x'));
        for (const el of items()) {
            expect(el).toHaveAttribute('data-column', 'right');
            expect(num(el, 'data-x')).toBeGreaterThan(gwRight);
        }
        // Lanes are ROWS inside the gateway, top to bottom in lane order.
        const ys = ['llm', 'mcp', 'a2a', 'skills', 'kb'].map((id) => num(screen.getByTestId(`simple-graph-lane-${id}`), 'data-y'));
        expect([...ys].sort((a, b) => a - b)).toEqual(ys);
    });

    it('the rules box and the lanes sit INSIDE the gateway; the system and the items outside it', async () => {
        await renderTab();
        const gw = screen.getByTestId('simple-graph-gateway');
        expect(gw).toHaveTextContent('Brutor Gateway');
        expect(within(gw).getByTestId('simple-graph-governance')).toBeInTheDocument();
        for (const id of ['llm', 'mcp', 'a2a', 'skills', 'kb']) {
            expect(within(gw).getByTestId(`simple-graph-lane-${id}`)).toBeInTheDocument();
        }
        expect(
            ['LLM Proxy', 'MCP Proxy', 'A2A', 'Skill Executor', 'Knowledge Base'].map((t) => within(gw).getByText(t))
        ).toHaveLength(5);
        expect(within(gw).queryByTestId('simple-graph-system')).toBeNull();
        expect(within(gw).queryAllByTestId(/^simple-graph-item-/)).toHaveLength(0);
    });

    it('a lane is drawn only when something is behind it', async () => {
        await renderTab({
            ...COMPOSITION_PAYLOAD,
            resources: {
                models: COMPOSITION_PAYLOAD.resources.models,
                mcp_servers: [],
                skills: [],
                knowledge_bases: [],
            },
            dependencies: [],
            observed_undeclared: [],
        });
        expect(lanes()).toEqual(['llm']);
        for (const id of ['mcp', 'a2a', 'skills', 'kb']) {
            expect(screen.queryByTestId(`simple-graph-lane-${id}`)).toBeNull();
        }
    });

    it('Knowledge Base appears only when the system has KBs', async () => {
        await renderTab();
        expect(lanes()).toContain('kb');
        expect(itemsOf('kb')).toEqual(['Credit Policy Handbook']);
        cleanup();
        await renderTab(DEMO);
        expect(lanes()).not.toContain('kb');
    });

    it('with nothing bound it still draws the system and the gateway, and says so', async () => {
        await renderTab({
            ...COMPOSITION_PAYLOAD,
            resources: { models: [], mcp_servers: [], skills: [], knowledge_bases: [] },
            dependencies: [],
            observed_undeclared: [],
        });
        expect(lanes()).toEqual([]);
        expect(screen.getByTestId('simple-graph-empty')).toBeInTheDocument();
        // The system → gateway line is still there.
        expect(line('system')).toBeInTheDocument();
        expect(graph()).toHaveAttribute('aria-label', expect.stringContaining('nothing is bound behind it yet'));
    });

    it('leaves the platform’s system MCP servers (Agent Skills) out of the MCP lane', async () => {
        await renderTab();
        expect(screen.queryByTestId('simple-graph-item-mcp:mcp-agent-skills')).toBeNull();
        expect(screen.queryByTestId('simple-graph-line-mcp:mcp-agent-skills')).toBeNull();
        expect(screen.getByTestId('simple-graph-lane-mcp')).toBeInTheDocument();
        cleanup();

        // …and a system whose only MCP binding is the system server has no MCP lane at all.
        await renderTab({
            ...COMPOSITION_PAYLOAD,
            resources: {
                ...COMPOSITION_PAYLOAD.resources,
                mcp_servers: COMPOSITION_PAYLOAD.resources.mcp_servers.filter((m) => m.system),
            },
        });
        expect(lanes()).not.toContain('mcp');
        // The Diagram still lists every binding — only the sketch omits plumbing.
        fireEvent.click(screen.getByTestId('composition-view-diagram'));
        expect(screen.getByTestId('composition-node-mcp:mcp-agent-skills')).toBeInTheDocument();
    });

    it('skills flow INTO the Skill Executor; every other line points at what it reaches', async () => {
        await renderTab();
        expect(screen.getByTestId('simple-graph-lane-skills')).toHaveAttribute('data-direction', 'in');
        expect(line('skill:skill-affordability')).toHaveAttribute('data-arrow', 'from');
        for (const key of ['model:llm-sonnet', 'mcp:mcp-core', 'dep:dep-fraud', 'kb:kb-policy']) {
            expect(line(key)).toHaveAttribute('data-arrow', 'to');
        }
        expect(line('system')).toHaveAttribute('data-arrow', 'to');
    });

    it('links each caption to the item’s page, like the other views', async () => {
        await renderTab();
        fireEvent.click(within(screen.getByTestId('simple-graph-item-model:llm-mini')).getByRole('button'));
        expect(setActiveTab).toHaveBeenLastCalledWith('llm-models:llm-mini');
        fireEvent.click(within(screen.getByTestId('simple-graph-item-dep:dep-fraud')).getByRole('button'));
        expect(setActiveTab).toHaveBeenLastCalledWith('ai-systems:sys-fraud:composition');
        fireEvent.click(within(screen.getByTestId('simple-graph-item-skill:skill-affordability')).getByRole('button'));
        expect(setActiveTab).toHaveBeenLastCalledWith('agent-skills');
        fireEvent.click(within(screen.getByTestId('simple-graph-count-guardrails')).getByRole('button'));
        expect(setActiveTab).toHaveBeenLastCalledWith('guardrails');
    });
});

describe('Simple graph — icons, not boxes', () => {
    it('each kind has its platform icon: MCP icon, brain for models, agent icon for agents', async () => {
        await renderTab();
        const icon = (key: string) => screen.getByTestId(`simple-graph-icon-${key}`);
        expect(icon('system')).toHaveAttribute('data-icon', 'bot');
        expect(icon('model:llm-sonnet')).toHaveAttribute('data-icon', 'brain');
        expect(icon('mcp:mcp-core')).toHaveAttribute('data-icon', 'mcp');
        // The sidebar's MCP Servers glyph itself, not a look-alike.
        expect(icon('mcp:mcp-core').querySelector('svg title')?.textContent).toBe('MCP');
        expect(icon('dep:dep-fraud')).toHaveAttribute('data-icon', 'bot');
        expect(icon('skill:skill-affordability')).toHaveAttribute('data-icon', 'sparkles');
        expect(icon('kb:kb-policy')).toHaveAttribute('data-icon', 'book-open');
        // Large: ~40px, the unit of the graph.
        expect(icon('model:llm-sonnet').style.width).toBe(`${SIMPLE_GRAPH_SIZES.icon}px`);
    });

    it('items are icon + caption + type, with no card box around them', async () => {
        await renderTab();
        for (const el of items()) {
            expect(el.className).not.toMatch(/\bborder|composition-node|rounded-(md|lg|xl)|\bbg-/);
            for (const child of Array.from(el.querySelectorAll('*'))) {
                expect(child.getAttribute('class') ?? '').not.toMatch(/\bborder(-|\b)|composition-node/);
            }
        }
        const sonnet = screen.getByTestId('simple-graph-item-model:llm-sonnet');
        expect(sonnet).toHaveTextContent('Claude Sonnet 5');
        expect(sonnet).toHaveTextContent('Model');
    });

    it('tints icons by category; external agents keep a dashed ring and say "assumed, not tested"', async () => {
        await renderTab();
        const sonnet = screen.getByTestId('simple-graph-icon-model:llm-sonnet');
        expect(sonnet).toHaveClass('composition-cat-ai-resource');
        const bureau = screen.getByTestId('simple-graph-item-dep:dep-bureau');
        expect(bureau).toHaveAttribute('data-dashed', 'true');
        expect(screen.getByTestId('simple-graph-icon-dep:dep-bureau')).toHaveAttribute('data-dashed', 'true');
        expect(bureau).toHaveTextContent('Assumed, not tested');
        expect(screen.getByTestId('simple-graph-icon-dep:dep-fraud')).toHaveAttribute('data-dashed', 'false');
        expect(screen.getByTestId('simple-graph-item-dep:dep-fraud')).not.toHaveTextContent('Assumed');
        // Lanes and the system are neutral.
        expect(screen.getByTestId('simple-graph-lane-llm').className).not.toMatch(/composition-cat-/);
        expect(screen.getByTestId('simple-graph-icon-system').className).not.toMatch(/composition-cat-/);
    });
});

describe('Simple graph — connector lines carry the connection status', () => {
    /** Status → a line in the fixture that has it. */
    const BY_STATUS: Record<CompositionConnectionStatus, string> = {
        ok: 'model:llm-sonnet',
        degraded: 'model:llm-mini',
        down: 'mcp:mcp-core',
        idle: 'kb:kb-policy',
    };

    it('one line per item plus the system → gateway line', async () => {
        await renderTab();
        const all = within(screen.getByTestId('simple-graph-lines')).getAllByTestId(/^simple-graph-line-/);
        expect(all).toHaveLength(items().length + 1);
    });

    it.each(Object.entries(BY_STATUS))('%s: its own colour AND its own line style', async (status, key) => {
        await renderTab();
        const s = status as CompositionConnectionStatus;
        const meta = CONNECTION_STATUS_META[s];
        const g = line(key);
        expect(g).toHaveAttribute('data-status', s);
        expect(g).toHaveClass(`composition-connection-${s}`);
        const stroke = g.querySelectorAll('path')[1];
        expect(stroke).toHaveClass('composition-connection-stroke');
        expect(stroke.getAttribute('stroke-dasharray')).toBe(meta.dash);
        expect(g).toHaveAttribute('data-dash', meta.dash ?? 'solid');
        // Only `down` carries the cross glyph.
        expect(within(g).queryByTestId('simple-graph-glyph-cross') !== null).toBe(s === 'down');
    });

    it('the four styles are all different, so colour is never the only signal', () => {
        const styles = Object.values(CONNECTION_STATUS_META).map((m) => `${m.dash ?? 'solid'}|${m.glyph ?? ''}`);
        expect(new Set(styles).size).toBe(4);
    });

    it('idle (no calls) is grey and dashed — never the green of ok', async () => {
        await renderTab();
        const idle = line('kb:kb-policy');
        expect(idle).toHaveClass('composition-connection-idle');
        expect(idle).not.toHaveClass('composition-connection-ok');
        // The tokens themselves: idle is grey, in both themes, and never equals ok.
        const css = readFileSync(resolve(__dirname, '../../../styles/globals.css'), 'utf8');
        const blocks = [...css.matchAll(/--connection-ok:\s*(#[0-9a-f]+);[\s\S]*?--connection-idle:\s*(#[0-9a-f]+);/gi)];
        expect(blocks).toHaveLength(2);
        for (const [, ok, idleColour] of blocks) {
            expect(idleColour).not.toBe(ok);
            const [r, gr, b] = [1, 3, 5].map((i) => parseInt(idleColour.slice(i, i + 2), 16));
            expect(Math.max(r, gr, b) - Math.min(r, gr, b)).toBeLessThan(24); // grey: no hue
        }
    });

    it('every line’s tooltip carries the reason', async () => {
        await renderTab();
        expect(lineTitle('model:llm-sonnet')).toBe('Claude Sonnet 5 — OK: 12 calls, 0 errors');
        expect(lineTitle('model:llm-mini')).toBe('GPT-5 mini — Degraded: 3 of 20 calls failed');
        expect(lineTitle('mcp:mcp-core')).toBe('Loan Core — Down: Last 3 calls blocked by policy');
        expect(lineTitle('kb:kb-policy')).toBe('Credit Policy Handbook — No calls: No calls in the last 7 days');
        expect(lineTitle('system')).toBe('Brutor Demo System → Brutor Gateway — OK: Health: healthy · 57 calls, 4 errors');
    });

    it('the system → gateway line is the system’s own status', async () => {
        await renderTab({ ...COMPOSITION_PAYLOAD, system_status: conn('down', 'Health: silent · no calls in the last 7 days') });
        expect(line('system')).toHaveAttribute('data-status', 'down');
        expect(within(line('system')).getByTestId('simple-graph-glyph-cross')).toBeInTheDocument();
        expect(lineTitle('system')).toContain('Health: silent');
        cleanup();
        await renderTab({ ...COMPOSITION_PAYLOAD, system_status: conn('idle', 'Health: learning · no calls in the last 7 days') });
        expect(line('system')).toHaveAttribute('data-status', 'idle');
    });

    it('the legend lists all four statuses with their meaning and line style, outside the image', async () => {
        await renderTab();
        const legend = screen.getByTestId('simple-graph-legend');
        expect(graph()).not.toContainElement(legend);
        for (const s of ['ok', 'degraded', 'down', 'idle'] as const) {
            const row = within(legend).getByTestId(`simple-graph-legend-${s}`);
            expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].label);
            expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].meaning);
            expect(row).toHaveTextContent(CONNECTION_STATUS_META[s].style);
        }
        expect(within(legend).getByTestId('simple-graph-legend-idle')).toHaveTextContent('not the same as all good');
        expect(within(legend).getByTestId('simple-graph-legend-external')).toHaveTextContent('assumed, not tested');
    });

    it('is an image whose one-sentence summary includes every lane’s statuses', async () => {
        await renderTab(DEMO);
        expect(graph()).toHaveAttribute('role', 'img');
        expect(graph().getAttribute('aria-label')).toBe(
            // The member agents' releases are in the sentence:
            // role="img" hides the lines under the system from AT.
            'Brutor Demo System (Agent), run by loan-intake@1.4.2 (verified), scoring-agent@0.9.0 (declared), ' +
                'calls through the Brutor Gateway (Policies 2, Limits 2, Guardrails 1; connection to the gateway ok): ' +
                'LLM Proxy reaches 2 models (1 ok, 1 degraded); MCP Proxy reaches 2 MCP servers (1 down, 1 no calls); ' +
                'A2A reaches 1 agent (1 degraded); Skill Executor loads 1 skill (1 ok).'
        );
    });
});

describe('Simple graph — scale', () => {
    it('many items grow the right column downwards without overlap; only the region scrolls sideways', async () => {
        const many = Array.from({ length: 40 }, (_, i) => ({
            id: `m${i}`,
            name: `Model ${i} with a rather long display name`,
            inherited_from: null,
            connection: conn(i % 2 ? 'ok' : 'idle', 'x', i % 2),
        }));
        const payload = { ...COMPOSITION_PAYLOAD, resources: { ...COMPOSITION_PAYLOAD.resources, models: many } };
        await renderTab(payload);
        expect(itemsOf('llm')).toHaveLength(40);
        const cells = items()
            .map((el) => ({ y: num(el, 'data-y'), h: num(el, 'data-h'), x: num(el, 'data-x') }))
            .sort((a, b) => a.y - b.y);
        for (let i = 1; i < cells.length; i++) {
            expect(cells[i].y).toBeGreaterThanOrEqual(cells[i - 1].y + cells[i - 1].h);
            expect(cells[i].x).toBe(cells[0].x); // one column: it grows vertically, not sideways
        }
        const region = screen.getByTestId('composition-simple-graph-region');
        expect(region).toHaveClass('overflow-x-auto', 'max-w-full');

        // The width does not depend on the item count; the height does.
        const few = layoutSimpleGraph(buildSimpleGraph(COMPOSITION_PAYLOAD, buildCompositionModel(COMPOSITION_PAYLOAD)));
        const lots = layoutSimpleGraph(buildSimpleGraph(payload, buildCompositionModel(payload)));
        expect(lots.width).toBe(few.width);
        expect(lots.height).toBeGreaterThan(few.height + 30 * SIMPLE_GRAPH_SIZES.itemH);
        expect(graph().style.width).toBe(`${lots.width}px`);
        // The gateway spans every lane, however tall.
        const last = lots.lanes[lots.lanes.length - 1];
        expect(lots.gateway.y + lots.gateway.h).toBeGreaterThanOrEqual(last.box.y + last.box.h);
    });

    it('the model is built from the same nodes the Diagram and List render', () => {
        const model = buildCompositionModel(COMPOSITION_PAYLOAD);
        const g = buildSimpleGraph(COMPOSITION_PAYLOAD, model);
        const byKey = new Map(model.sections.flatMap((s) => s.nodes).map((n) => [n.key, n]));
        for (const item of g.lanes.flatMap((l) => l.items)) {
            const node = byKey.get(item.key);
            expect(node).toBeTruthy();
            expect(item.label).toBe(node?.label);
            expect(item.link).toBe(node?.link);
            expect(item.connection).toBe(node?.connection);
        }
        // Delegation lane = every delegation node (declared, granted, observed).
        const delegation = model.sections.find((s) => s.id === 'delegation')?.nodes.map((n) => n.key);
        expect(g.lanes.find((l) => l.id === 'a2a')?.items.map((i) => i.key)).toEqual(delegation);
    });
});
