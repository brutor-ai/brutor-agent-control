/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { COMPOSITION_PAYLOAD } from '../../components/ai-systems/__tests__/fixtures/compositionPayload';
import type { AISystemCompositionView, HumanApprovalGate } from '../aiSystemTypes';
import { buildCompositionModel } from '../composition';
import { SIMPLE_GRAPH_SIZES, buildSimpleGraph, graphHasGates, layoutSimpleGraph } from '../compositionSimpleGraph';
import { approvalTitle, laneApproval, nodeApproval } from '../humanApproval';

const gate = (over: Partial<HumanApprovalGate>): HumanApprovalGate => ({
    source: 'agent_grant',
    scope: 'always',
    lane: 'mcp',
    target: '*',
    resource_ids: [],
    detail: 'gate',
    ...over,
});

const withGates = (
    gates: HumanApprovalGate[],
    over: Partial<AISystemCompositionView['human_approval']> = {}
): AISystemCompositionView => ({
    ...COMPOSITION_PAYLOAD,
    human_approval: { autonomy_requires_approval: false, approval_window_seconds: null, gates, ...over },
});

const graphOf = (view: AISystemCompositionView) => buildSimpleGraph(view, buildCompositionModel(view));

const firstMcp = COMPOSITION_PAYLOAD.resources.mcp_servers.find((m) => !m.system)!;
const mcpKey = `mcp:${firstMcp.id}`;

describe('where a node is gated', () => {
    it('an always gate beats a conditional one on the same resource', () => {
        const view = withGates([
            gate({ scope: 'conditional', resource_ids: ['r'], detail: 'when args match' }),
            gate({ scope: 'always', resource_ids: ['r'], detail: 'always' }),
        ]);
        const approval = nodeApproval(view, ['r']);
        expect(approval?.scope).toBe('always');
        expect(approval?.details).toEqual(['when args match', 'always']);
    });

    it('a conditional gate alone reads conditional', () => {
        const view = withGates([gate({ scope: 'conditional', resource_ids: ['r'] })]);
        expect(nodeApproval(view, ['r'])?.scope).toBe('conditional');
    });

    it('a node with no gate is not marked', () => {
        const view = withGates([gate({ resource_ids: ['other'] })]);
        expect(nodeApproval(view, ['r'])).toBeNull();
    });

    it('a gate naming nothing bound stays on its lane, and only that lane', () => {
        const view = withGates([gate({ lane: 'mcp', resource_ids: [] })]);
        expect(laneApproval(view, 'mcp')).not.toBeNull();
        expect(laneApproval(view, 'skills')).toBeNull();
    });

    it('states how long a person has, when the system declares it', () => {
        const view = withGates([gate({ resource_ids: ['r'], detail: 'needs approval' })], {
            approval_window_seconds: 7200,
        });
        expect(approvalTitle(nodeApproval(view, ['r'])!)).toBe('needs approval\nA person has 2 h to decide.');
    });
});

describe('the Simple graph', () => {
    it('a system with no gate says nothing about human approval', () => {
        // Deliberately silent: no gate may be the owner's choice, and "no
        // oversight" would read as a fault.
        const graph = graphOf(COMPOSITION_PAYLOAD);
        expect(graph.humanApproval).toBeNull();
        expect(graphHasGates(graph)).toBe(false);
        expect(graph.summary).not.toMatch(/approval/i);
    });

    it('marks the resource, counts it and says so in the text equivalent', () => {
        const graph = graphOf(withGates([gate({ resource_ids: [firstMcp.id], detail: 'x' })]));
        const items = graph.lanes.flatMap((l) => l.items);
        expect(items.find((i) => i.key === mcpKey)?.approval?.scope).toBe('always');
        expect(items.filter((i) => i.approval)).toHaveLength(1);
        expect(graph.humanApproval).toBe(`1 of ${items.length} resources`);
        expect(graph.summary).toContain('Human approval:');
        expect(graph.summary).toContain('needs approval');
        expect(graphHasGates(graph)).toBe(true);
    });

    it('finds an MCP gate through either id the binding goes by', () => {
        if (!firstMcp.mcp_server_id) return;
        const graph = graphOf(withGates([gate({ resource_ids: [firstMcp.mcp_server_id] })]));
        expect(graph.lanes.flatMap((l) => l.items).find((i) => i.key === mcpKey)?.approval).toBeTruthy();
    });

    it('a system-wide autonomy rule is stated without marking every resource', () => {
        const graph = graphOf(withGates([], { autonomy_requires_approval: true }));
        expect(graph.humanApproval).toBe('side-effecting agent calls');
        expect(graphHasGates(graph)).toBe(false);
    });

    it('a gate that cannot be placed is not lost', () => {
        const graph = graphOf(withGates([gate({ lane: 'mcp', target: 'ghost_tool' })]));
        expect(graph.lanes.find((l) => l.id === 'mcp')?.approval?.details).toEqual(['gate']);
        expect(graph.humanApproval).toBe('declared on tools no bound server reports');
        expect(graphHasGates(graph)).toBe(true);
    });

    it('a lane with no items still appears when it holds an unplaced gate', () => {
        const view: AISystemCompositionView = {
            ...withGates([gate({ lane: 'skills', target: 'ghost' })]),
            resources: { ...COMPOSITION_PAYLOAD.resources, skills: [] },
        };
        expect(graphOf(view).lanes.find((l) => l.id === 'skills')?.items).toEqual([]);
    });
});

describe('the layout makes room for the mark', () => {
    const rows = (view: AISystemCompositionView) => layoutSimpleGraph(graphOf(view)).lanes.flatMap((l) => l.items);

    it('a gated item is taller and an ungated one is unchanged', () => {
        const gated = rows(withGates([gate({ resource_ids: [firstMcp.id] })]));
        for (const it of gated) {
            const expected = SIMPLE_GRAPH_SIZES.itemH + (it.item.key === mcpKey ? SIMPLE_GRAPH_SIZES.approvalH : 0);
            expect(it.cell.h).toBe(expected);
        }
    });

    it('no two rows overlap with every resource gated', () => {
        const all = COMPOSITION_PAYLOAD.resources;
        const ids = [...all.models, ...all.mcp_servers, ...all.skills, ...all.knowledge_bases].map((r) => r.id);
        const laid = rows(withGates([gate({ resource_ids: ids })]));
        const sorted = [...laid].sort((a, b) => a.cell.y - b.cell.y);
        sorted.slice(1).forEach((it, i) => {
            expect(it.cell.y).toBeGreaterThanOrEqual(sorted[i].cell.y + sorted[i].cell.h);
        });
    });

    it('the graph is tall enough for the lines under the system', () => {
        const S = SIMPLE_GRAPH_SIZES;
        const view: AISystemCompositionView = {
            ...withGates([], { autonomy_requires_approval: true }),
            agents: [],
            dependencies: [],
            resources: { models: [], mcp_servers: [], skills: [], knowledge_bases: [] },
        };
        const laid = layoutSimpleGraph(graphOf(view));
        expect(laid.height).toBeGreaterThanOrEqual(
            laid.system.icon.y + laid.system.icon.h + S.systemCaptionH + S.systemApprovalH
        );
    });
});
