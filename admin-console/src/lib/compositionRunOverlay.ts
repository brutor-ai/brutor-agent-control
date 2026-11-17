/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The run overlay — one run applied to the Composition tab's Simple graph.
 *
 * Every ACTION (the hierarchy is run > step > turn > action) becomes a numbered
 * badge on the connector it travelled: the MCP server's line for a tool
 * call (labelled with the tool), the skill's for a skill run (skill +
 * script), the model's for a model call, the delegation agent's for an A2A
 * call. A node used several times lists all its numbers on one chip
 * ("2 · 4 · 6"); a crowded chip collapses consecutive numbers to ranges
 * ("7–12") and, past the chip's width, to "+N" — the sequence list always
 * shows every action.
 *
 * A delegate's own actions inside the run (the fraud screener's model call)
 * are NOT the root's connections: they are listed as sub-steps (5a, 5b) in
 * an inset under the delegation item whose A2A call entered them.
 *
 * ONE computation: the numbers, outcomes, targets, systems and nesting all
 * come from the control plane's projection of the run's actions
 * (`GET /v1/admin/runs/{id}` → `app/services/run_actions.py`), the same
 * payload the Runs timeline and Run details read. This module only places
 * them — it never renumbers or reclassifies an action.
 *
 * METADATA ONLY: an overlay action carries identifiers, names, outcome and
 * timing. The payload's `error_message` is deliberately not read here, and
 * no argument, prompt or response is ever part of the model (guard:
 * `compositionRunOverlay.test.ts` plants content in every free-text field).
 */

import type {
    RunActionOutcome,
    RunActionTargetKind,
    RunDetail,
    RunSummary,
} from './aiSystemTypes';
import { NO_EVIDENCE, RUN_OUTCOME_META, RUN_OUTCOME_ORDER } from './compositionConnection';
import {
    LANES,
    SIMPLE_GRAPH_SIZES,
    type SimpleGraph,
    type SimpleGraphItem,
    type SimpleGraphLane,
    type SimpleGraphLayoutOverlay,
    type SimpleLaneId,
} from './compositionSimpleGraph';

/** The route that opens a system's Composition tab with a run applied
 *  (`ai-systems:{id}:composition:{runId}` — the app's deep-link pattern). */
export function runOverlayRoute(systemId: string, runId: string | null): string {
    return runId ? `ai-systems:${systemId}:composition:${runId}` : `ai-systems:${systemId}:composition`;
}

/** The picker's "who/what for": "application APP-1001", else the channel
 *  ("via loan-portal"), else null. */
export function runSubject(run: RunSummary): string | null {
    if (run.subject_id) return `${run.subject_kind || 'subject'} ${run.subject_id}`;
    if (run.client_label) return `via ${run.client_label}`;
    return null;
}

const LANE_OF: Record<RunActionTargetKind, SimpleLaneId> = {
    model: 'llm',
    mcp: 'mcp',
    a2a: 'a2a',
    skill: 'skills',
    kb: 'kb',
};

/** What each kind of action is called in the list. */
const KIND_LABEL: Record<RunActionTargetKind, string> = {
    model: 'Model call',
    mcp: 'MCP tool',
    a2a: 'A2A call',
    skill: 'Skill run',
    kb: 'Retrieval',
};

export interface OverlayAction {
    /** The control plane's number: "5", "5a", "5a.1". */
    number: string;
    /** The A2A call it ran inside, or null (top level). */
    enteredBy: string | null;
    nesting: number;
    outcome: RunActionOutcome;
    /** "MCP tool", "Model call", … */
    kindLabel: string;
    /** What it reached, by name: the tool, "skill · script", the model, the
     *  delegate. Never an argument. */
    target: string;
    /** Where on the graph (the resource the target lives on), or null. */
    via: string | null;
    /** The graph item its badge sits on; null = not drawn on the graph
     *  (a delegate's step, or an action with no recorded target). */
    itemKey: string | null;
    stepId: string | null;
    stepName: string | null;
    /** "2" asserted, "~2" inferred, null neither. */
    turn: string | null;
    durationMs: number | null;
    /** The AI System that took it, when not the run's root. */
    delegateOf: string | null;
}

/** One chip token: a number, a range, or "+N". */
export interface BadgeToken {
    text: string;
    /** The action numbers the token stands for. */
    numbers: string[];
    /** The worst outcome among them (blocked > held > error > answered). */
    outcome: RunActionOutcome;
}

export interface OverlayStepGroup {
    /** null = actions no step was asserted for. */
    stepId: string | null;
    title: string;
    actions: OverlayAction[];
}

export interface RunOverlay {
    runId: string;
    /** Every action in display order: top level by number, each A2A call
     *  followed by the sub-steps it entered. */
    actions: OverlayAction[];
    /** Top-level numbers per graph item, in order. */
    badgesByItem: Record<string, string[]>;
    /** Per graph item, its chip's tokens (collapsed when crowded). */
    chips: Record<string, BadgeToken[]>;
    /** Delegation item → the sub-steps the run's calls into it entered. */
    insets: Record<string, OverlayAction[]>;
    /** Items the run reached (a badge, an inset, or a retrieval). */
    touched: Set<string>;
    /** Lanes the run reached. */
    touchedLanes: Set<SimpleLaneId>;
    /** Items the run reached that the declared composition does not list. */
    undeclared: Set<string>;
    /** The list's step groups (a header each), in display order. */
    groups: OverlayStepGroup[];
    /** Actions shown / the run's action_count (the projection is capped). */
    shown: number;
    total: number;
    /** A sentence for the graph's aria-label. */
    summary: string;
}

// ---------------------------------------------------------------------------
// Chips
// ---------------------------------------------------------------------------

const OUTCOME_RANK: Record<RunActionOutcome, number> = { answered: 0, error: 1, held: 2, blocked: 3 };

function worst(outcomes: RunActionOutcome[]): RunActionOutcome {
    return outcomes.reduce<RunActionOutcome>((a, b) => (OUTCOME_RANK[b] > OUTCOME_RANK[a] ? b : a), 'answered');
}

// Calibrated against the rendered badge (11 px mono, 1.5 px border, px-1)
// in Chromium at 1280: 6.6 px a character + 11 px of border and padding, a
// glyph ~9 px, a "·" separator with its gaps ~7 px, the chip's own padding
// and border ~10 px — each rounded up, so the chip never overflows its rect.
const CHAR_W = 7;
const TOKEN_PAD = 12;
const GLYPH_W = 10;
const SEP_W = 8;
const CHIP_PAD = 12;

export function tokenWidth(t: BadgeToken): number {
    return t.text.length * CHAR_W + TOKEN_PAD + (RUN_OUTCOME_META[t.outcome].glyph ? GLYPH_W : 0);
}

/** The chip's width in px for its tokens (estimate; monospace digits). */
export function chipWidth(tokens: BadgeToken[]): number {
    if (tokens.length === 0) return 0;
    return CHIP_PAD + tokens.reduce((a, t) => a + tokenWidth(t), 0) + SEP_W * (tokens.length - 1);
}

/** Up to this many numbers are listed one by one ("2 · 4 · 6"). */
export const CHIP_MAX_SINGLES = 4;

/**
 * A line's chip. Few numbers: each one ("2 · 4 · 6"). Crowded: runs of
 * consecutive numbers collapse to ranges ("7–12"); if that still does not
 * fit the chip, the tail folds into "+N". Sub-numbers never reach a chip —
 * only top-level actions travel the root's lines.
 */
export function buildChip(numbers: string[], outcomeOf: (n: string) => RunActionOutcome): BadgeToken[] {
    const single = (n: string): BadgeToken => ({ text: n, numbers: [n], outcome: outcomeOf(n) });
    let tokens: BadgeToken[];
    if (numbers.length <= CHIP_MAX_SINGLES) {
        tokens = numbers.map(single);
    } else {
        tokens = [];
        let run: string[] = [];
        const flush = () => {
            if (run.length === 0) return;
            if (run.length <= 2) tokens.push(...run.map(single));
            else
                tokens.push({
                    text: `${run[0]}–${run[run.length - 1]}`,
                    numbers: run,
                    outcome: worst(run.map(outcomeOf)),
                });
            run = [];
        };
        for (const n of numbers) {
            const prev = run[run.length - 1];
            if (prev !== undefined && Number(n) === Number(prev) + 1) run.push(n);
            else {
                flush();
                run = [n];
            }
        }
        flush();
    }
    const max = SIMPLE_GRAPH_SIZES.chipMaxW;
    if (chipWidth(tokens) <= max) return tokens;
    // Fold the tail into "+N" until the chip fits (the list shows them all).
    for (let keep = tokens.length - 1; keep >= 1; keep--) {
        const rest = tokens.slice(keep).flatMap((t) => t.numbers);
        const more: BadgeToken = { text: `+${rest.length}`, numbers: rest, outcome: worst(rest.map(outcomeOf)) };
        const next = [...tokens.slice(0, keep), more];
        if (chipWidth(next) <= max) return next;
    }
    return [{ text: `${numbers.length} actions`, numbers, outcome: worst(numbers.map(outcomeOf)) }];
}

// ---------------------------------------------------------------------------
// The overlay
// ---------------------------------------------------------------------------

type Action = NonNullable<RunDetail['actions']>[number];

function targetText(a: Action): { target: string; via: string | null } {
    const name = a.target_name ?? a.target_id ?? null;
    switch (a.target_kind) {
        case 'mcp':
            return { target: a.capability ?? name ?? 'MCP call', via: name };
        case 'skill':
            return { target: [name ?? 'Skill', a.skill_script].filter(Boolean).join(' · '), via: null };
        case 'model':
            return { target: a.model ?? name ?? 'Model', via: a.model && name && name !== a.model ? name : null };
        case 'a2a': {
            const to = a.delegate_system_name ?? name;
            return { target: a.capability ?? to ?? 'A2A call', via: a.capability ? to : null };
        }
        case 'kb':
            return { target: name ?? 'Knowledge base', via: null };
        default:
            return { target: a.proxy_subtype ?? a.proxy_type, via: null };
    }
}

function turnOf(a: Action): string | null {
    if (a.turn_id && a.turn_seq != null) return String(a.turn_seq);
    if (a.turn_id) return a.turn_id;
    return a.inferred_turn ? `~${a.inferred_turn}` : null;
}

function isOutcome(v: unknown): v is RunActionOutcome {
    return typeof v === 'string' && (RUN_OUTCOME_ORDER as readonly string[]).includes(v);
}

/** Display order: top level in number order, each call followed by the
 *  sub-steps it entered (depth first). The array arrives in the gateway's
 *  order, which puts a delegate's steps BEFORE the call they ran inside.
 *  The ONE ordering — the Runs timeline and the overlay's list both use it.
 *  A payload without numbers (the contract makes them optional) keeps its order. */
export function runActionDisplayOrder<T extends Action>(actions: T[]): T[] {
    if (!actions.every((a) => a.number)) return actions;
    return displayOrder(actions);
}

function displayOrder<T extends Action>(actions: T[]): T[] {
    const children = new Map<string, T[]>();
    const top: T[] = [];
    for (const a of actions) {
        if (a.entered_by) {
            const list = children.get(a.entered_by) ?? [];
            list.push(a);
            children.set(a.entered_by, list);
        } else top.push(a);
    }
    const out: T[] = [];
    const seen = new Set<string>();
    const visit = (a: T) => {
        const n = a.number ?? '';
        if (seen.has(n)) return;
        seen.add(n);
        out.push(a);
        for (const c of children.get(n) ?? []) visit(c);
    };
    top.forEach(visit);
    // A sub-step whose entering call fell past the cap still gets listed.
    for (const a of actions) if (!seen.has(a.number ?? '')) visit(a);
    return out;
}

/**
 * Apply one run to the Simple graph. Returns the graph to draw — the static
 * one plus any item or lane the run reached that the composition does not
 * declare (marked `undeclared`) — and the overlay model.
 */
export function applyRunOverlay(
    graph: SimpleGraph,
    run: RunDetail
): { graph: SimpleGraph; overlay: RunOverlay } {
    const raw = (run.actions ?? []).filter((a) => a.number);
    const ordered = displayOrder(raw);
    const byNumber = new Map(ordered.map((a) => [a.number ?? '', a]));
    const outcomeOf = (n: string): RunActionOutcome => {
        const o = byNumber.get(n)?.outcome;
        return isOutcome(o) ? o : 'answered';
    };

    // A mutable copy of the lanes: the run may add items (and lanes).
    const lanes: SimpleGraphLane[] = graph.lanes.map((l) => ({ ...l, items: [...l.items] }));
    const undeclared = new Set<string>();
    const findItem = (laneId: SimpleLaneId, ids: string[]): SimpleGraphItem | null => {
        const lane = lanes.find((l) => l.id === laneId);
        return lane?.items.find((it) => it.targetIds.some((t) => ids.includes(t))) ?? null;
    };
    const addUndeclared = (laneId: SimpleLaneId, id: string, label: string): SimpleGraphItem => {
        let lane = lanes.find((l) => l.id === laneId);
        const spec = LANES.find((l) => l.id === laneId);
        if (!spec) throw new Error(`unknown lane ${laneId}`);
        if (!lane) {
            lane = { id: laneId, title: spec.title, direction: spec.direction, items: [], undeclared: true };
            // Keep the static lane order.
            const order = LANES.map((l) => l.id);
            const at = lanes.findIndex((l) => order.indexOf(l.id) > order.indexOf(laneId));
            if (at === -1) lanes.push(lane);
            else lanes.splice(at, 0, lane);
        }
        const item: SimpleGraphItem = {
            key: `run:${laneId}:${id}`,
            label,
            kind: spec.kind,
            typeLabel: spec.typeLabel,
            category: laneId === 'a2a' ? 'delegation_agent' : 'ai_resource',
            link: '',
            dashed: false,
            note: 'Not in the declared composition',
            connection: NO_EVIDENCE,
            targetIds: [id],
            undeclared: true,
        };
        lane.items.push(item);
        undeclared.add(item.key);
        return item;
    };
    const place = (kind: RunActionTargetKind, ids: string[], label: string): string | null => {
        const wanted = ids.filter(Boolean);
        if (wanted.length === 0) return null;
        const laneId = LANE_OF[kind];
        return (findItem(laneId, wanted) ?? addUndeclared(laneId, wanted[0], label)).key;
    };

    const badgesByItem: Record<string, string[]> = {};
    const insets: Record<string, OverlayAction[]> = {};
    const touched = new Set<string>();
    const itemOfNumber = new Map<string, string | null>();
    const actions: OverlayAction[] = [];

    for (const a of ordered) {
        const number = a.number ?? '';
        const { target, via } = targetText(a);
        const top = !a.entered_by;
        let itemKey: string | null = null;
        if (top && a.target_kind && a.target_id) {
            const ids = a.target_kind === 'a2a' ? [a.target_id, a.delegate_system_id ?? ''] : [a.target_id];
            const label =
                a.target_kind === 'a2a'
                    ? (a.delegate_system_name ?? a.target_name ?? a.target_id)
                    : (a.target_name ?? a.target_id);
            itemKey = place(a.target_kind, ids, label);
            if (itemKey) {
                (badgesByItem[itemKey] ??= []).push(number);
                touched.add(itemKey);
            }
            // A retrieval touches the knowledge bases too (same number).
            for (const kb of a.kb_ids ?? []) {
                const kbKey = place('kb', [kb], kb);
                if (kbKey) {
                    (badgesByItem[kbKey] ??= []).push(number);
                    touched.add(kbKey);
                }
            }
        }
        itemOfNumber.set(number, itemKey);
        const oa: OverlayAction = {
            number,
            enteredBy: a.entered_by ?? null,
            nesting: a.nesting ?? 0,
            outcome: isOutcome(a.outcome) ? a.outcome : 'answered',
            kindLabel: a.target_kind ? KIND_LABEL[a.target_kind] : a.proxy_type,
            target,
            via,
            itemKey,
            stepId: a.step_id ?? null,
            stepName: a.step_name ?? null,
            turn: turnOf(a),
            durationMs: typeof a.duration_ms === 'number' ? a.duration_ms : null,
            delegateOf: a.own === false ? (a.system_name ?? a.system_id ?? 'another AI System') : null,
        };
        actions.push(oa);
        if (!top) {
            // Nested under the delegation item its top-level ancestor reached.
            let root = a.entered_by ?? '';
            while (byNumber.get(root)?.entered_by) root = byNumber.get(root)?.entered_by ?? '';
            const host = itemOfNumber.get(root);
            if (host) {
                (insets[host] ??= []).push(oa);
                touched.add(host);
            }
        }
    }

    const chips: Record<string, BadgeToken[]> = {};
    for (const [key, numbers] of Object.entries(badgesByItem)) chips[key] = buildChip(numbers, outcomeOf);

    const touchedLanes = new Set<SimpleLaneId>(
        lanes.filter((l) => l.items.some((it) => touched.has(it.key))).map((l) => l.id)
    );

    // Step groups for the list: a nested action stays under its caller's step.
    const groups: OverlayStepGroup[] = [];
    const anyStep = actions.some((a) => a.stepId || a.stepName);
    const stepOf = new Map<string, { id: string | null; name: string | null }>();
    for (const a of actions) {
        const own = { id: a.stepId, name: a.stepName };
        const inherited = a.enteredBy && !a.stepId && !a.stepName ? stepOf.get(a.enteredBy) : undefined;
        const step = inherited ?? own;
        stepOf.set(a.number, step);
        const title = anyStep ? (step.name ?? step.id ?? 'No step asserted') : 'Unphased — the client asserted no steps';
        const last = groups[groups.length - 1];
        const id = anyStep ? (step.id ?? step.name) : null;
        if (last && last.stepId === id && last.title === title) last.actions.push(a);
        else groups.push({ stepId: id, title, actions: [a] });
    }

    const counts = RUN_OUTCOME_ORDER.map((o) => {
        const n = actions.filter((a) => a.outcome === o).length;
        return n ? `${n} ${RUN_OUTCOME_META[o].label.toLowerCase()}` : null;
    })
        .filter(Boolean)
        .join(', ');
    const topCount = actions.filter((a) => !a.enteredBy).length;
    const nested = actions.length - topCount;
    const shown = raw.length;
    const total = Math.max(run.action_count ?? 0, shown);
    const summary =
        `Run ${run.id} applied: ${actions.length} action${actions.length === 1 ? '' : 's'}` +
        (nested ? ` (${nested} inside delegations)` : '') +
        (counts ? ` — ${counts}` : '') +
        `; ${touched.size} of the graph's items reached` +
        (undeclared.size ? `, ${undeclared.size} not in the declared composition` : '') +
        (shown < total ? `; the first ${shown} of ${total} actions are shown` : '') +
        '. The sequence list after the graph lists every action.';

    return {
        graph: { ...graph, lanes },
        overlay: {
            runId: run.id,
            actions,
            badgesByItem,
            chips,
            insets,
            touched,
            touchedLanes,
            undeclared,
            groups,
            shown,
            total,
            summary,
        },
    };
}

/** What the layout needs to make room for the overlay. */
export function overlayLayout(overlay: RunOverlay): SimpleGraphLayoutOverlay {
    return {
        chipWidths: Object.fromEntries(Object.entries(overlay.chips).map(([k, t]) => [k, chipWidth(t)])),
        insetRows: Object.fromEntries(Object.entries(overlay.insets).map(([k, a]) => [k, a.length])),
    };
}
