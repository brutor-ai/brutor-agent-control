/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Composition tab's "Simple graph",
 * left to right:
 *
 *                     ┌── Brutor Gateway ──────────┐
 *                     │ Policies · Limits · Guard. │
 *    (🤖)  ─────────▶ │                            │
 *   System            │  [ LLM Proxy      ] ───────┼──▶ (🧠) model
 *   agent@1.2         │                            │──▶ (🧠) model
 *                     │  [ MCP Proxy      ] ───────┼──▶ (MCP) server
 *                     │  [ Skill Executor ] ◀──────┼─── (✨) skill
 *                     └────────────────────────────┘
 *
 * Icons are the unit (no boxes around items): a large icon with the name
 * as a caption and the type as a muted line. Every connector line carries
 * the connection's status from the control plane (`CompositionConnection`
 * — ONE rule, `ai_system_connection_status.connection_status`): colour AND
 * line style AND, for `down`, a cross glyph, so colour is never the only
 * signal — mapped by `compositionConnection.ts`, the module the Diagram
 * and List views read too. The system → gateway line is the system's
 * stored health.
 *
 * The items are taken from the SAME `CompositionModel` the Diagram and the
 * List render (labels, links, external = dashed, connection), so the three
 * views cannot disagree. The one deliberate difference: the platform's own
 * system MCP servers (Agent Skills, …) are gateway plumbing, not something
 * the owner connected, and are left out of the MCP lane.
 *
 * Layout is DETERMINISTIC (`layoutSimpleGraph`): fixed cell sizes, one
 * item per row in the right column, so nothing can overlap and many items
 * grow the column downwards. No DOM measurement — the geometry is a pure
 * function, testable without a layout engine.
 */

import type { AgentReleaseTrust } from './agentIdentityTypes';
import { releaseLabel } from './agentRelease';
import type { AISystemCompositionView, CompositionConnection, CompositionConnectionStatus } from './aiSystemTypes';
import { CONNECTION_STATUS_META, NO_EVIDENCE, connectionTitle, statusMix } from './compositionConnection';
import type { CompositionCategory, CompositionModel, CompositionNode } from './composition';
import { groupLink } from './composition';
import { systemKindLabel } from './groupTypes';
import { HUMAN_APPROVAL_META, humanApprovalSummary, laneApproval, type NodeApproval } from './humanApproval';

export type SimpleLaneId = 'llm' | 'mcp' | 'a2a' | 'skills' | 'kb';

/** What an item is — picks its icon. */
export type SimpleItemKind = 'model' | 'mcp' | 'agent' | 'skill' | 'kb';

export interface SimpleGraphItem {
    key: string;
    label: string;
    kind: SimpleItemKind;
    /** The small type label: Model, MCP server, Agent, Skill, Knowledge base. */
    typeLabel: string;
    category: CompositionCategory;
    /** null: the page it would open is not part of this console. */
    link: string | null;
    /** External delegation agents: assessed at the boundary only. */
    dashed: boolean;
    /** Extra muted caption line ("Assumed, not tested"), or null. */
    note: string | null;
    connection: CompositionConnection;
    /** The ids a run action's target can name this item by (see
     *  `CompositionNode.targetIds`) — the run overlay places badges by them. */
    targetIds: string[];
    /** Run overlay only: the run reached it, the declared composition does
     *  not list it. */
    undeclared?: boolean;
    /** Declared human approval; absent or null = none. */
    approval?: NodeApproval | null;
}

export interface SimpleGraphLane {
    id: SimpleLaneId;
    title: string;
    /** `out`: the lane reaches its items (arrow lane → item). `in`: the
     *  items are loaded INTO the lane (skills → Skill Executor). */
    direction: 'out' | 'in';
    items: SimpleGraphItem[];
    /** Run overlay only: a lane the static composition has no items in. */
    undeclared?: boolean;
    /** Gates that could not be placed on one item of this lane. */
    approval?: NodeApproval | null;
}

export interface SimpleGraphGovernance {
    /** Policies other than guardrails (argument, response, governance, cache). */
    policies: number;
    limits: number;
    guardrails: number;
}

/** A member agent under the system icon. */
export interface SimpleGraphAgent {
    key: string;
    /** `name@version` of its current release; the identity's name when it
     *  has never declared one. */
    label: string;
    /** null = no release declared (no marker). */
    trust: AgentReleaseTrust | null;
    link: string | null;
}

export interface SimpleGraph {
    system: { id: string; name: string; kindLabel: string; link: string };
    /** The system → gateway line (the system's stored health). */
    systemStatus: CompositionConnection;
    agents: SimpleGraphAgent[];
    governance: SimpleGraphGovernance;
    lanes: SimpleGraphLane[];
    /** One-sentence text equivalent — the graph's aria-label. */
    summary: string;
    /** Under the system: "Human approval: …"; null = nothing declared. */
    humanApproval: string | null;
}

// ---------------------------------------------------------------------------
// Lanes
// ---------------------------------------------------------------------------

export interface LaneSpec {
    id: SimpleLaneId;
    title: string;
    direction: 'out' | 'in';
    kind: SimpleItemKind;
    typeLabel: string;
    /** Plural noun for the summary. */
    noun: [string, string];
    select: (n: CompositionNode) => boolean;
}

export const LANES: readonly LaneSpec[] = [
    {
        id: 'llm',
        title: 'LLM Proxy',
        direction: 'out',
        kind: 'model',
        typeLabel: 'Model',
        noun: ['model', 'models'],
        select: (n) => n.key.startsWith('model:'),
    },
    {
        id: 'mcp',
        title: 'MCP Proxy',
        direction: 'out',
        kind: 'mcp',
        typeLabel: 'MCP server',
        noun: ['MCP server', 'MCP servers'],
        select: (n) => n.key.startsWith('mcp:'),
    },
    {
        id: 'a2a',
        title: 'A2A',
        direction: 'out',
        kind: 'agent',
        typeLabel: 'Agent',
        noun: ['agent', 'agents'],
        select: (n) => n.category === 'delegation_agent',
    },
    {
        id: 'skills',
        title: 'Skill Executor',
        direction: 'in',
        kind: 'skill',
        typeLabel: 'Skill',
        noun: ['skill', 'skills'],
        select: (n) => n.key.startsWith('skill:'),
    },
    {
        id: 'kb',
        title: 'Knowledge Base',
        direction: 'out',
        kind: 'kb',
        typeLabel: 'Knowledge base',
        noun: ['knowledge base', 'knowledge bases'],
        select: (n) => n.key.startsWith('kb:'),
    },
];

export const SIMPLE_LANE_ORDER: readonly SimpleLaneId[] = LANES.map((l) => l.id);

export function governanceCounts(view: AISystemCompositionView): SimpleGraphGovernance {
    const guardrails = view.gateway.policies.filter((p) => p.kind === 'guardrail').length;
    return {
        policies: view.gateway.policies.length - guardrails,
        limits: view.gateway.limits.length,
        guardrails,
    };
}

/** Whether any mark is drawn on the graph (the legend's condition). */
export const graphHasGates = (graph: SimpleGraph): boolean =>
    graph.lanes.some((l) => l.approval || l.items.some((i) => i.approval));

const count = (n: number, [one, many]: [string, string]) => `${n} ${n === 1 ? one : many}`;

export function buildSimpleGraph(view: AISystemCompositionView, model: CompositionModel): SimpleGraph {
    const systemMcp = new Set(view.resources.mcp_servers.filter((m) => m.system).map((m) => `mcp:${m.id}`));
    const nodes = model.sections
        .filter((s) => s.id === 'resources' || s.id === 'delegation')
        .flatMap((s) => s.nodes)
        .filter((n) => !systemMcp.has(n.key));

    const lanes: SimpleGraphLane[] = [];
    const phrases: string[] = [];
    for (const spec of LANES) {
        const items = nodes.filter(spec.select).map(
            (n): SimpleGraphItem => ({
                key: n.key,
                label: n.label,
                kind: spec.kind,
                typeLabel: spec.typeLabel,
                category: n.category ?? 'ai_resource',
                link: n.link,
                dashed: n.dashed,
                note: n.dashed ? 'Assumed, not tested' : null,
                connection: n.connection ?? NO_EVIDENCE,
                targetIds: n.targetIds,
                approval: n.humanApproval,
            })
        );
        const approval = laneApproval(view, spec.id);
        if (items.length === 0 && !approval) continue;
        lanes.push({ id: spec.id, title: spec.title, direction: spec.direction, items, approval });
        const verb = spec.direction === 'in' ? 'loads' : 'reaches';
        phrases.push(`${spec.title} ${verb} ${count(items.length, spec.noun)} (${statusMix(items)})`);
    }

    const agents = model.sections
        .filter((s) => s.id === 'agents')
        .flatMap((s) => s.nodes)
        .map(
            (n): SimpleGraphAgent => ({
                key: n.key,
                label: n.agentRelease?.ref ? releaseLabel(n.agentRelease.ref) : n.label,
                trust: n.agentRelease?.ref?.trust ?? null,
                link: n.link,
            })
        );

    const governance = governanceCounts(view);
    const kindLabel = systemKindLabel(view.system.system_kind);
    const system = kindLabel ? `${view.system.name} (${kindLabel})` : view.system.name;
    const systemStatus = view.system_status;
    // The graph is role="img": its children are hidden from assistive
    // technology, so the releases and every line's status are in the sentence.
    const who = agents.length
        ? `${system}, run by ${agents.map((a) => (a.trust ? `${a.label} (${a.trust})` : a.label)).join(', ')},`
        : system;
    const rules = `Policies ${governance.policies}, Limits ${governance.limits}, Guardrails ${governance.guardrails}`;
    const own = `connection to the gateway ${CONNECTION_STATUS_META[systemStatus.status].label.toLowerCase()}`;
    const allItems = lanes.flatMap((l) => l.items);
    const humanApproval = humanApprovalSummary(view, allItems.filter((i) => i.approval).length, allItems.length);
    const gated = allItems.flatMap((i) =>
        i.approval ? [`${i.label} (${HUMAN_APPROVAL_META[i.approval.scope].label.toLowerCase()})`] : []
    );
    const summary =
        `${who} calls through the Brutor Gateway (${rules}; ${own})` +
        (phrases.length ? `: ${phrases.join('; ')}.` : '; nothing is bound behind it yet.') +
        (humanApproval ? ` Human approval: ${humanApproval}${gated.length ? `: ${gated.join(', ')}` : ''}.` : '');

    return {
        system: {
            id: view.system.id,
            name: view.system.name,
            kindLabel,
            link: groupLink(view.system.id),
        },
        systemStatus,
        agents,
        governance,
        lanes,
        summary,
        humanApproval,
    };
}

// ---------------------------------------------------------------------------
// Layout — pure geometry, px
// ---------------------------------------------------------------------------

/** Every size the layout uses, in px. Exported so tests and the renderer
 *  read the same numbers. */
export const SIMPLE_GRAPH_SIZES = {
    pad: 8,
    icon: 40,
    systemW: 176,
    systemGap: 72,
    gatewayW: 232,
    gatewayHeaderH: 36,
    governanceH: 84,
    laneBoxW: 184,
    laneBoxH: 40,
    laneGap: 12,
    /** Space between the gateway and the item column — the curves. */
    fanW: 144,
    itemW: 168,
    /** One item: icon + two caption lines + the type line (+ note). */
    itemH: 104,
    /** The human-approval marker under an item that has one. */
    approvalH: 22,
    /** The "Human approval" lines under the system. */
    systemApprovalH: 56,
    /** Under the system icon: name (two lines) and kind. */
    systemCaptionH: 56,
    /** One member-agent release line under the system. */
    agentLineH: 20,
    // --- The run overlay (numbered badges on the lines) ---------------
    /** The fan is wider with a run applied: the badge chips sit in it. */
    fanWOverlay: 200,
    chipH: 20,
    /** Chips stay inside the fan, clear of the gateway and the icons. */
    chipMaxW: 184,
    /** Clear space kept between two chips of one lane. */
    chipGap: 4,
    /** The nested sub-steps under a delegation item. */
    insetHeaderH: 18,
    insetRowH: 20,
    insetPad: 6,
    insetMaxRows: 6,
} as const;

/** The widest the graph may be: a 1280 px viewport less the default
 *  sidebar (256) and the page's gutters (2 × 24). A run overlay must fit
 *  in it — no horizontal page scroll at 1280. */
export const SIMPLE_GRAPH_WIDTH_BUDGET = 1280 - 256 - 48;

/** With a run applied the tab splits into two columns — the graph (and
 *  its legend) left, the sequence list (and its legend) right — when the
 *  tab's content is at least this wide: an ~1100 px viewport less the
 *  default sidebar. Narrower, it stacks (graph, then sequence). */
export const RUN_COLUMNS_MIN_WIDTH = 1100 - 256;

/** The graph never shrinks below this factor to fit its column: smaller
 *  and the 11 px badges stop being readable. Past it the graph region
 *  (never the page) scrolls sideways. */
export const GRAPH_MIN_SCALE = 0.6;

/** The factor that fits a `width`-px graph into `available` px: 1 when it
 *  fits or the room is unknown, never above 1 or below GRAPH_MIN_SCALE. */
export function graphFitScale(available: number | null, width: number): number {
    if (!available || available <= 0 || width <= available) return 1;
    return Math.max(GRAPH_MIN_SCALE, available / width);
}

/** The run overlay's arrangement for a tab `width` px wide (null = not
 *  measured, e.g. no layout engine: the wide arrangement). */
export function runOverlayArrangement(width: number | null): 'columns' | 'stacked' {
    return width != null && width < RUN_COLUMNS_MIN_WIDTH ? 'stacked' : 'columns';
}

/** An inset's height for `rows` sub-steps (0 = none). */
export function insetHeight(rows: number): number {
    const S = SIMPLE_GRAPH_SIZES;
    if (rows <= 0) return 0;
    const shown = Math.min(rows, S.insetMaxRows) + (rows > S.insetMaxRows ? 1 : 0);
    return S.insetHeaderH + shown * S.insetRowH + S.insetPad;
}

export interface Point {
    x: number;
    y: number;
}
export interface Rect {
    x: number;
    y: number;
    w: number;
    h: number;
}

export interface LaidOutItem {
    item: SimpleGraphItem;
    /** The item's cell (icon centred at the top; an inset grows it). */
    cell: Rect;
    /** Where its line touches it: the icon's left edge, at its centre. */
    anchor: Point;
    /** Run overlay: the nested sub-steps box under the caption, or null. */
    inset: Rect | null;
}

export interface LaidOutLane {
    lane: SimpleGraphLane;
    box: Rect;
    /** Where its lines leave the gateway: its right edge, at the lane's middle. */
    port: Point;
    items: LaidOutItem[];
}

export interface SimpleGraphLine {
    key: string;
    status: CompositionConnectionStatus;
    /** The line's accessible title / tooltip. */
    title: string;
    /** Cubic path, `from` → `to`. */
    path: string;
    from: Point;
    to: Point;
    /** Which end carries the arrowhead. */
    arrowAt: 'from' | 'to';
    /** The path's midpoint (for the status glyph). */
    mid: Point;
}

export interface SimpleGraphLayout {
    width: number;
    height: number;
    system: { icon: Rect; column: Rect };
    gateway: Rect;
    governance: Rect;
    lanes: LaidOutLane[];
    lines: SimpleGraphLine[];
    /** Run overlay: each item's badge chip, on its line inside the fan. */
    chips: Record<string, Rect>;
}

/** What the run overlay needs room for, per item key. */
export interface SimpleGraphLayoutOverlay {
    /** The chip's width in px (clamped to `chipMaxW`). */
    chipWidths: Record<string, number>;
    /** How many nested sub-steps the item's inset lists. */
    insetRows: Record<string, number>;
}

/** The point on `curve(from, to)` whose x is `x` (the curve is monotonic
 *  in x, so a bisection on t finds it). */
export function pointOnCurveAtX(from: Point, to: Point, x: number): Point {
    const D = to.x - from.x;
    const xAt = (t: number) => from.x + D * (1.5 * t * (1 - t) + t * t * t);
    let lo = 0;
    let hi = 1;
    for (let i = 0; i < 40; i++) {
        const mid = (lo + hi) / 2;
        if (xAt(mid) < x) lo = mid;
        else hi = mid;
    }
    const t = (lo + hi) / 2;
    return { x, y: from.y + (to.y - from.y) * (3 * t * t - 2 * t * t * t) };
}

function curve(from: Point, to: Point): { path: string; mid: Point } {
    const dx = (to.x - from.x) / 2;
    const path = `M ${from.x} ${from.y} C ${from.x + dx} ${from.y}, ${to.x - dx} ${to.y}, ${to.x} ${to.y}`;
    // A symmetric cubic with horizontal tangents passes through the midpoint.
    return { path, mid: { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 } };
}

export function layoutSimpleGraph(graph: SimpleGraph, overlay?: SimpleGraphLayoutOverlay): SimpleGraphLayout {
    const S = SIMPLE_GRAPH_SIZES;
    const fanW = overlay ? S.fanWOverlay : S.fanW;
    const systemX = S.pad;
    const gatewayX = systemX + S.systemW + S.systemGap;
    const itemX = gatewayX + S.gatewayW + fanW;
    const width = itemX + S.itemW + S.pad;

    const gatewayY = S.pad;
    const governance: Rect = {
        x: gatewayX + (S.gatewayW - S.laneBoxW) / 2,
        y: gatewayY + S.gatewayHeaderH,
        w: S.laneBoxW,
        h: S.governanceH,
    };

    let y = governance.y + governance.h + S.laneGap * 2;
    const lanes: LaidOutLane[] = [];
    const lines: SimpleGraphLine[] = [];
    const chips: Record<string, Rect> = {};
    for (const lane of graph.lanes) {
        // One item per row; an item's inset (run overlay) grows its row.
        const baseH = (it: SimpleGraphItem) => S.itemH + (it.approval ? S.approvalH : 0);
        const heights = lane.items.map((it) => baseH(it) + insetHeight(overlay?.insetRows[it.key] ?? 0));
        const itemsH = heights.reduce((a, b) => a + b, 0);
        const blockH = Math.max(S.laneBoxH + S.laneGap, itemsH);
        const mid = y + blockH / 2;
        const box: Rect = { x: governance.x, y: mid - S.laneBoxH / 2, w: S.laneBoxW, h: S.laneBoxH };
        const port: Point = { x: gatewayX + S.gatewayW, y: mid };
        let cy = y;
        const items = lane.items.map((item, i): LaidOutItem => {
            const cell: Rect = { x: itemX, y: cy, w: S.itemW, h: heights[i] };
            cy += heights[i];
            const anchor: Point = { x: cell.x + (cell.w - S.icon) / 2, y: cell.y + S.pad + S.icon / 2 };
            const rows = overlay?.insetRows[item.key] ?? 0;
            const inset: Rect | null = rows
                ? { x: cell.x + 4, y: cell.y + baseH(item) - S.pad, w: cell.w - 8, h: insetHeight(rows) }
                : null;
            return { item, cell, anchor, inset };
        });
        if (overlay) {
            // Chips centred in the fan, on their own line; then pushed apart
            // top to bottom so two chips of one lane never overlap.
            const cx = port.x + fanW / 2;
            const placed = items
                .filter(({ item }) => (overlay.chipWidths[item.key] ?? 0) > 0)
                .map(({ item, anchor }) => {
                    const w = Math.min(overlay.chipWidths[item.key], S.chipMaxW);
                    const at = pointOnCurveAtX(port, anchor, cx);
                    return { key: item.key, rect: { x: cx - w / 2, y: at.y - S.chipH / 2, w, h: S.chipH } };
                });
            let floor = -Infinity;
            for (const c of placed) {
                c.rect.y = Math.max(c.rect.y, floor);
                floor = c.rect.y + S.chipH + S.chipGap;
                chips[c.key] = c.rect;
            }
        }
        for (const { item, anchor } of items) {
            const { path, mid: m } = curve(port, anchor);
            lines.push({
                key: item.key,
                status: item.connection.status,
                title: connectionTitle(item.label, item.connection),
                path,
                from: port,
                to: anchor,
                arrowAt: lane.direction === 'in' ? 'from' : 'to',
                mid: m,
            });
        }
        lanes.push({ lane, box, port, items });
        y += blockH + S.laneGap;
    }
    if (graph.lanes.length === 0) y += S.laneBoxH + S.laneGap;

    const gateway: Rect = { x: gatewayX, y: gatewayY, w: S.gatewayW, h: y - gatewayY };
    // The system icon faces the governance box: every call passes it first.
    const icon: Rect = {
        x: systemX + (S.systemW - S.icon) / 2,
        y: governance.y + governance.h / 2 - S.icon / 2,
        w: S.icon,
        h: S.icon,
    };
    const from: Point = { x: icon.x + icon.w, y: icon.y + icon.h / 2 };
    const to: Point = { x: gatewayX, y: from.y };
    lines.unshift({
        key: 'system',
        status: graph.systemStatus.status,
        title: connectionTitle(`${graph.system.name} → Brutor Gateway`, graph.systemStatus),
        path: `M ${from.x} ${from.y} L ${to.x} ${to.y}`,
        from,
        to,
        arrowAt: 'to',
        mid: { x: (from.x + to.x) / 2, y: from.y },
    });

    // The system column: icon, name, kind, then one line per member agent.
    const systemBottom =
        icon.y +
        icon.h +
        S.systemCaptionH +
        graph.agents.length * S.agentLineH +
        (graph.humanApproval ? S.systemApprovalH : 0);
    const height = Math.max(gateway.y + gateway.h, systemBottom) + S.pad;
    return {
        width,
        height,
        system: { icon, column: { x: systemX, y: icon.y, w: S.systemW, h: height - icon.y } },
        gateway,
        governance,
        lanes,
        lines,
        chips,
    };
}
