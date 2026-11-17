/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Composition tab's default view, left to right:
 * the AI System (its member agents' releases under it) → the Brutor
 * Gateway (Policies · Limits · Guardrails, then one row per non-empty
 * lane) → everything the system uses, grouped by lane. Model and geometry:
 * `lib/compositionSimpleGraph.ts` (a pure layout — no DOM measurement).
 *
 * Icons are the unit: a large icon with the name as a caption, the type as
 * a muted line, no card boxes. Every connector line is coloured AND styled
 * by the connection's status (solid / dash-dot / solid + cross / grey
 * dashed) and carries its reason as a tooltip; the legend under the graph
 * explains all four.
 *
 * The graph is `role="img"` with a one-sentence summary (statuses
 * included); the List view is its accessible equivalent. On a narrow
 * screen the graph region (not the page) scrolls sideways. With `fit` (a
 * run overlaid, graph in its own column) it scales down to the column
 * first — `graphFitScale`, floored at GRAPH_MIN_SCALE.
 */

import React from 'react';
import { BookOpen, Bot, Brain, Cpu, Network, Plug, Sparkles, Waypoints } from 'lucide-react';

import { CATEGORY_META, CompositionCategory, groupLink } from '../../lib/composition';
import { CONNECTION_STATUS_META, RUN_OUTCOME_META, runOutcomeClass } from '../../lib/compositionConnection';
import { overlayLayout, type BadgeToken, type OverlayAction, type RunOverlay } from '../../lib/compositionRunOverlay';
import {
    SIMPLE_GRAPH_SIZES,
    graphHasGates,
    graphFitScale,
    layoutSimpleGraph,
    type LaidOutItem,
    type LaidOutLane,
    type Rect,
    type SimpleGraph,
    type SimpleGraphLayout,
    type SimpleGraphLine,
    type SimpleItemKind,
    type SimpleLaneId,
} from '../../lib/compositionSimpleGraph';
import { useElementWidth } from '../../hooks/useElementWidth';
import { TrustMarker } from '../agent/AgentReleaseBadges';
import MCPIcon from '../common/MCPIcon';
import { LinkText } from './CompositionLink';
import { ConnectionLegend, CrossGlyph } from './ConnectionStatus';
import { HumanApprovalLegend, HumanApprovalMark } from './HumanApprovalMark';
import { HUMAN_APPROVAL_GLYPH } from '../../lib/humanApproval';

type IconComponent = React.ComponentType<{ className?: string }>;

const LANE_ICON: Record<SimpleLaneId, IconComponent> = {
    llm: Cpu,
    mcp: Plug,
    a2a: Waypoints,
    skills: Sparkles,
    kb: BookOpen,
};

/** The platform's icon for each kind of thing (MCP = the sidebar's MCP
 *  Servers icon; the agent icon for systems and delegation agents). */
const ITEM_ICON: Record<SimpleItemKind, { Icon: IconComponent; name: string }> = {
    model: { Icon: Brain, name: 'brain' },
    mcp: { Icon: MCPIcon, name: 'mcp' },
    agent: { Icon: Bot, name: 'bot' },
    skill: { Icon: Sparkles, name: 'sparkles' },
    kb: { Icon: BookOpen, name: 'book-open' },
};

const px = (r: Rect, origin: { x: number; y: number } = { x: 0, y: 0 }): React.CSSProperties => ({
    position: 'absolute',
    left: r.x - origin.x,
    top: r.y - origin.y,
    width: r.w,
    height: r.h,
});

/** The run overlay's hover link between a badge and its list row: the
 *  action numbers currently highlighted, and the setter both sides call. */
export interface OverlayHighlight {
    active: readonly string[] | null;
    setActive: (numbers: readonly string[] | null) => void;
}

const isActive = (h: OverlayHighlight | undefined, numbers: readonly string[]) =>
    Boolean(h?.active?.some((n) => numbers.includes(n)));

export function CompositionSimpleGraph({
    graph,
    open,
    overlay,
    highlight,
    fit = false,
}: {
    graph: SimpleGraph;
    open?: (tab: string) => void;
    /** A run applied to the graph (numbered badges, dimming, insets). */
    overlay?: RunOverlay | null;
    highlight?: OverlayHighlight;
    /** Scale the graph down to its region's width (the run overlay's
     *  two-column layout) instead of scrolling the region sideways. */
    fit?: boolean;
}) {
    const layout = layoutSimpleGraph(graph, overlay ? overlayLayout(overlay) : undefined);
    const dimmed = (key: string) => Boolean(overlay && !overlay.touched.has(key));
    const [measureRegion, regionWidth] = useElementWidth();
    // The geometry stays in layout px (tests and the layout read the same
    // numbers); a sizer box of the scaled size keeps the flow honest, and
    // the graph inside it is scaled from its top-left corner.
    const scale = fit ? graphFitScale(regionWidth, layout.width) : 1;
    return (
        <div className="space-y-3">
            <div
                ref={measureRegion}
                className="max-w-full overflow-x-auto"
                data-testid="composition-simple-graph-region"
                data-fit={fit ? 'true' : 'false'}
            >
                <div
                    data-testid="composition-simple-graph-sizer"
                    data-scale={scale}
                    style={scale < 1 ? { width: layout.width * scale, height: layout.height * scale } : undefined}
                >
                <div
                    role="img"
                    aria-label={overlay ? `${graph.summary} ${overlay.summary}` : graph.summary}
                    data-testid="composition-simple-graph"
                    data-layout="horizontal"
                    data-lanes={graph.lanes.map((l) => l.id).join(',')}
                    data-run={overlay?.runId ?? ''}
                    className="relative"
                    style={{
                        width: layout.width,
                        height: layout.height,
                        minWidth: layout.width,
                        ...(scale < 1 ? { transform: `scale(${scale})`, transformOrigin: 'top left' } : {}),
                    }}
                >
                    <SystemNode graph={graph} layout={layout} open={open} />
                    <Gateway graph={graph} layout={layout} open={open} overlay={overlay ?? null} />
                    {layout.lanes.flatMap((l) =>
                        l.items.map((it) => (
                            <ItemNode
                                key={it.item.key}
                                laid={it}
                                laneId={l.lane.id}
                                open={open}
                                dimmed={dimmed(it.item.key)}
                            />
                        ))
                    )}
                    <Lines layout={layout} dimmed={overlay ? dimmed : undefined} />
                    {overlay &&
                        Object.entries(layout.chips).map(([key, rect]) => (
                            <RunChip key={key} itemKey={key} rect={rect} tokens={overlay.chips[key] ?? []} highlight={highlight} />
                        ))}
                    {overlay &&
                        layout.lanes.flatMap((l) =>
                            l.items
                                .filter((it) => it.inset)
                                .map((it) => (
                                    <RunInset
                                        key={`inset-${it.item.key}`}
                                        itemKey={it.item.key}
                                        rect={it.inset as Rect}
                                        actions={overlay.insets[it.item.key] ?? []}
                                        highlight={highlight}
                                    />
                                ))
                        )}
                </div>
                </div>
            </div>
            <ConnectionLegend />
            {graphHasGates(graph) && <HumanApprovalLegend />}
        </div>
    );
}

/** One run badge token: the number (or range), coloured by the action's
 *  outcome through THE status mapping, with the outcome's glyph and the
 *  outcome in words as its title — colour is never alone. */
export function RunBadge({
    text,
    numbers,
    outcome,
    active,
    testId,
    onHover,
}: BadgeToken & { active: boolean; testId: string; onHover?: (numbers: readonly string[] | null) => void }) {
    const meta = RUN_OUTCOME_META[outcome];
    return (
        <span
            className={`composition-run-badge ${runOutcomeClass(outcome)} inline-flex items-center gap-0.5 rounded px-1 font-mono text-[11px] font-semibold tabular-nums`}
            data-testid={testId}
            data-numbers={numbers.join(',')}
            data-outcome={outcome}
            data-status={meta.status}
            data-glyph={meta.glyph ?? 'none'}
            data-active={active ? 'true' : 'false'}
            title={`${numbers.length > 1 ? `Actions ${numbers.join(', ')}` : `Action ${numbers[0]}`} — ${meta.label}`}
            onMouseEnter={onHover ? () => onHover(numbers) : undefined}
            onMouseLeave={onHover ? () => onHover(null) : undefined}
        >
            {text}
            {meta.glyph && <span aria-hidden="true">{meta.glyph}</span>}
        </span>
    );
}

/** A line's chip: every number of every action that travelled it. */
function RunChip({
    itemKey,
    rect,
    tokens,
    highlight,
}: {
    itemKey: string;
    rect: Rect;
    tokens: BadgeToken[];
    highlight?: OverlayHighlight;
}) {
    return (
        <div
            className="absolute flex items-center justify-center gap-0.5 whitespace-nowrap rounded-full border border-strong bg-surface px-1 shadow-sm"
            style={px(rect)}
            data-testid={`run-chip-${itemKey}`}
            data-x={rect.x}
            data-y={rect.y}
            data-w={rect.w}
            data-h={rect.h}
        >
            {tokens.map((t, i) => (
                <React.Fragment key={t.text}>
                    {i > 0 && (
                        <span className="text-[10px] text-muted" aria-hidden="true">
                            ·
                        </span>
                    )}
                    <RunBadge
                        {...t}
                        active={isActive(highlight, t.numbers)}
                        testId={`run-badge-${t.text}`}
                        onHover={highlight?.setActive}
                    />
                </React.Fragment>
            ))}
        </div>
    );
}

/** The sub-steps a delegation ran inside the run (5a, 5b …), nested under
 *  the delegation item whose A2A call entered them. */
function RunInset({
    itemKey,
    rect,
    actions,
    highlight,
}: {
    itemKey: string;
    rect: Rect;
    actions: OverlayAction[];
    highlight?: OverlayHighlight;
}) {
    const S = SIMPLE_GRAPH_SIZES;
    const shown = actions.slice(0, S.insetMaxRows);
    const who = actions.find((a) => a.delegateOf)?.delegateOf;
    return (
        <div
            className="absolute overflow-hidden rounded-md border border-dashed border-strong bg-surface-alt px-1.5 text-left"
            style={px(rect)}
            data-testid={`run-inset-${itemKey}`}
            data-x={rect.x}
            data-y={rect.y}
            data-h={rect.h}
        >
            <p className="truncate text-[10px] font-medium uppercase tracking-wide text-muted" style={{ height: S.insetHeaderH, lineHeight: `${S.insetHeaderH}px` }}>
                Inside {who ?? 'the delegation'}
            </p>
            <ul>
                {shown.map((a) => (
                    <li
                        key={a.number}
                        className="flex items-center gap-1 text-[11px] text-secondary"
                        style={{ height: S.insetRowH, paddingLeft: (a.nesting - 1) * 8 }}
                        data-testid={`run-inset-row-${a.number}`}
                        title={`${a.number} ${a.kindLabel}: ${a.target} — ${RUN_OUTCOME_META[a.outcome].label}`}
                    >
                        <RunBadge
                            text={a.number}
                            numbers={[a.number]}
                            outcome={a.outcome}
                            active={isActive(highlight, [a.number])}
                            testId={`run-badge-${a.number}`}
                            onHover={highlight?.setActive}
                        />
                        <span className="truncate">{a.target}</span>
                    </li>
                ))}
                {actions.length > shown.length && (
                    <li className="text-[10px] italic text-muted" style={{ height: S.insetRowH }}>
                        +{actions.length - shown.length} more — see the list
                    </li>
                )}
            </ul>
        </div>
    );
}

function SystemNode({
    graph,
    layout,
    open,
}: {
    graph: SimpleGraph;
    layout: SimpleGraphLayout;
    open?: (tab: string) => void;
}) {
    const { icon, column } = layout.system;
    return (
        <div
            className="flex flex-col items-center text-center"
            style={{ position: 'absolute', left: column.x, top: column.y, width: column.w }}
            data-testid="simple-graph-system"
            data-column="left"
            data-x={column.x}
        >
            <IconDisc kind="agent" category={null} dashed={false} testId="simple-graph-icon-system" size={icon.w} />
            <LinkText to={graph.system.link} open={open} className="mt-1 break-words text-sm font-semibold text-default">
                {graph.system.name}
            </LinkText>
            {graph.system.kindLabel && <span className="text-[11px] text-muted">{graph.system.kindLabel}</span>}
            {/* The member agents' releases: name@version and a
                small trust marker, nothing more. */}
            {graph.agents.length > 0 && (
                <ul className="mt-1 space-y-0.5" data-testid="simple-graph-agents">
                    {graph.agents.map((a) => (
                        <li key={a.key} className="text-xs text-secondary" data-testid={`simple-graph-agent-${a.key}`}>
                            <LinkText to={a.link} open={open} className="font-mono">
                                {a.label}
                            </LinkText>
                            {a.trust && (
                                <>
                                    {' '}
                                    <TrustMarker trust={a.trust} />
                                </>
                            )}
                        </li>
                    ))}
                </ul>
            )}
            {graph.humanApproval && (
                <p
                    className="mt-1 text-[11px] text-secondary"
                    data-testid="simple-graph-human-approval"
                >
                    <span aria-hidden="true">{HUMAN_APPROVAL_GLYPH} </span>
                    Human approval: {graph.humanApproval}
                </p>
            )}
        </div>
    );
}

function Gateway({
    graph,
    layout,
    open,
    overlay,
}: {
    graph: SimpleGraph;
    layout: SimpleGraphLayout;
    open?: (tab: string) => void;
    overlay: RunOverlay | null;
}) {
    const gw = layout.gateway;
    const lastLane = layout.lanes[layout.lanes.length - 1];
    const spineTop = layout.governance.y + layout.governance.h;
    return (
        <div
            className="composition-gateway rounded-xl"
            style={px(gw)}
            data-testid="simple-graph-gateway"
            data-column="middle"
            data-x={gw.x}
        >
            <div className="flex items-center gap-2 px-3 pt-2">
                <Network className="h-4 w-4 text-secondary" aria-hidden="true" />
                <span className="text-sm font-semibold text-default">Brutor Gateway</span>
            </div>
            <Governance graph={graph} rect={layout.governance} origin={gw} open={open} />
            {/* The rules apply to every lane: a neutral spine from the rules box through the lanes. */}
            {lastLane && (
                <span
                    className="absolute w-px bg-current text-muted"
                    style={{ left: gw.w / 2, top: spineTop - gw.y, height: lastLane.port.y - spineTop }}
                    aria-hidden="true"
                />
            )}
            {layout.lanes.map((l) => (
                <LaneBox
                    key={l.lane.id}
                    laid={l}
                    origin={gw}
                    dimmed={Boolean(overlay && !overlay.touchedLanes.has(l.lane.id))}
                />
            ))}
            {graph.lanes.length === 0 && (
                <p
                    className="absolute inset-x-0 px-3 text-center text-xs italic text-muted"
                    style={{ top: spineTop - gw.y + SIMPLE_GRAPH_SIZES.laneGap }}
                    data-testid="simple-graph-empty"
                >
                    Nothing is bound behind the gateway yet.
                </p>
            )}
        </div>
    );
}

function Governance({
    graph,
    rect,
    origin,
    open,
}: {
    graph: SimpleGraph;
    rect: Rect;
    origin: Rect;
    open?: (tab: string) => void;
}) {
    const { policies, limits, guardrails } = graph.governance;
    const governance = groupLink(graph.system.id, 'governance');
    const parts: { label: string; value: number; category: CompositionCategory; link: string }[] = [
        { label: 'Policies', value: policies, category: 'policy', link: governance },
        { label: 'Limits', value: limits, category: 'limit', link: governance },
        { label: 'Guardrails', value: guardrails, category: 'policy', link: 'guardrails' },
    ];
    return (
        <div
            className="flex flex-col justify-center gap-0.5 rounded-lg border border-default bg-surface px-3 text-sm"
            style={px(rect, origin)}
            data-testid="simple-graph-governance"
        >
            {parts.map((p) => (
                <span
                    key={p.label}
                    className={`flex items-center gap-1.5 ${CATEGORY_META[p.category].className}`}
                    data-testid={`simple-graph-count-${p.label.toLowerCase()}`}
                >
                    <span className="composition-swatch h-2.5 w-2.5 shrink-0 rounded-sm" aria-hidden="true" />
                    <LinkText to={p.link} open={open} className="text-default">
                        {p.label} <span className="font-semibold tabular-nums">{p.value}</span>
                    </LinkText>
                </span>
            ))}
        </div>
    );
}

function LaneBox({ laid, origin, dimmed }: { laid: LaidOutLane; origin: Rect; dimmed: boolean }) {
    const Icon = LANE_ICON[laid.lane.id];
    return (
        <div
            className={`flex items-center justify-center gap-1.5 rounded-lg border border-strong bg-surface px-3 text-sm font-medium text-default ${
                dimmed ? 'opacity-40' : ''
            } ${laid.lane.undeclared ? 'composition-run-undeclared' : ''}`}
            style={px(laid.box, origin)}
            data-testid={`simple-graph-lane-${laid.lane.id}`}
            data-direction={laid.lane.direction}
            data-y={laid.port.y}
            data-dimmed={dimmed ? 'true' : 'false'}
            data-undeclared={laid.lane.undeclared ? 'true' : 'false'}
            title={laid.lane.undeclared ? 'Used by this run — not in the declared composition' : undefined}
        >
            <Icon className="h-4 w-4 shrink-0 text-secondary" aria-hidden="true" />
            {laid.lane.title}
            {laid.lane.approval && (
                <HumanApprovalMark
                    approval={laid.lane.approval}
                    compact
                    testId={`simple-graph-lane-approval-${laid.lane.id}`}
                />
            )}
        </div>
    );
}

function IconDisc({
    kind,
    category,
    dashed,
    testId,
    size,
}: {
    kind: SimpleItemKind;
    category: CompositionCategory | null;
    dashed: boolean;
    testId: string;
    size: number;
}) {
    const { Icon, name } = ITEM_ICON[kind];
    const tint = category ? `${CATEGORY_META[category].className} composition-ink composition-icon-disc` : 'text-secondary';
    return (
        <span
            className={`composition-icon-ring inline-flex shrink-0 items-center justify-center rounded-full ${tint}`}
            style={{ width: size, height: size }}
            data-testid={testId}
            data-icon={name}
            data-dashed={dashed ? 'true' : 'false'}
            aria-hidden="true"
        >
            <Icon className="h-8 w-8" />
        </span>
    );
}

function ItemNode({
    laid,
    laneId,
    open,
    dimmed = false,
}: {
    laid: LaidOutItem;
    laneId: SimpleLaneId;
    open?: (tab: string) => void;
    /** Run overlay: the run did not reach it. */
    dimmed?: boolean;
}) {
    const { item, cell } = laid;
    return (
        <div
            className={`flex flex-col items-center text-center${dimmed ? ' opacity-40' : ''}${
                item.undeclared ? ' composition-run-undeclared rounded-lg' : ''
            }`}
            style={{ ...px(cell), paddingTop: SIMPLE_GRAPH_SIZES.pad }}
            data-dimmed={dimmed ? 'true' : 'false'}
            data-undeclared={item.undeclared ? 'true' : 'false'}
            data-testid={`simple-graph-item-${item.key}`}
            data-column="right"
            data-lane={laneId}
            data-kind={item.kind}
            data-category={item.category}
            data-dashed={item.dashed ? 'true' : 'false'}
            data-status={item.connection.status}
            data-x={cell.x}
            data-y={cell.y}
            data-h={cell.h}
        >
            <IconDisc
                kind={item.kind}
                category={item.category}
                dashed={item.dashed}
                testId={`simple-graph-icon-${item.key}`}
                size={SIMPLE_GRAPH_SIZES.icon}
            />
            {item.link ? (
                <LinkText
                    to={item.link}
                    open={open}
                    className="mt-1 line-clamp-2 max-w-full break-words text-xs font-medium text-default"
                >
                    {item.label}
                </LinkText>
            ) : (
                <span className="mt-1 line-clamp-2 max-w-full break-words text-xs font-medium text-default">
                    {item.label}
                </span>
            )}
            <span className="text-[10px] uppercase tracking-wide text-muted">{item.typeLabel}</span>
            {item.approval && (
                <HumanApprovalMark
                    approval={item.approval}
                    testId={`simple-graph-approval-${item.key}`}
                />
            )}
            {item.note && (
                <span
                    className={`text-[10px] italic ${item.undeclared ? 'composition-run-undeclared-text font-medium' : 'text-muted'}`}
                    data-testid={item.undeclared ? `simple-graph-undeclared-${item.key}` : undefined}
                >
                    {item.note}
                </span>
            )}
        </div>
    );
}

/** Every connector line, coloured AND styled by status, each with its
 *  reason as a tooltip (a wide invisible hit stroke makes it hoverable). */
function Lines({ layout, dimmed }: { layout: SimpleGraphLayout; dimmed?: (key: string) => boolean }) {
    return (
        <svg
            width={layout.width}
            height={layout.height}
            className="pointer-events-none absolute left-0 top-0"
            data-testid="simple-graph-lines"
        >
            {layout.lines.map((line) => (
                // The system → gateway line carries every action: never dimmed.
                <Line key={line.key} line={line} dimmed={line.key !== 'system' && Boolean(dimmed?.(line.key))} />
            ))}
        </svg>
    );
}

function Line({ line, dimmed = false }: { line: SimpleGraphLine; dimmed?: boolean }) {
    const meta = CONNECTION_STATUS_META[line.status];
    const end = line.arrowAt === 'to' ? line.to : line.from;
    // Horizontal tangents at both ends: the arrowhead points right into an
    // item, or left into the gateway (skills load INTO the Skill Executor).
    const d = line.arrowAt === 'to' ? -7 : 7;
    const head = `M ${end.x + d} ${end.y - 4} L ${end.x} ${end.y} L ${end.x + d} ${end.y + 4}`;
    return (
        <g
            className={`${meta.className} ${dimmed ? 'opacity-25' : ''}`}
            data-dimmed={dimmed ? 'true' : 'false'}
            data-testid={`simple-graph-line-${line.key}`}
            data-status={line.status}
            data-dash={meta.dash ?? 'solid'}
            data-glyph={meta.glyph ?? 'none'}
            data-arrow={line.arrowAt}
        >
            <title>{line.title}</title>
            <path d={line.path} fill="none" stroke="transparent" strokeWidth={12} style={{ pointerEvents: 'stroke' }} />
            <path
                d={line.path}
                fill="none"
                className="composition-connection-stroke"
                strokeWidth={2}
                strokeDasharray={meta.dash ?? undefined}
            />
            <path d={head} fill="none" className="composition-connection-stroke" strokeWidth={2} strokeLinejoin="round" />
            {meta.glyph === 'cross' && <CrossGlyph at={line.mid} />}
        </g>
    );
}

export default CompositionSimpleGraph;
