/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The run overlay model and its geometry (`lib/compositionRunOverlay.ts`,
 * `layoutSimpleGraph` with an overlay) — pure functions, so every rule the
 * owner set is pinned without a layout engine:
 *
 *  - numbering is the control plane's, in its order; a delegate's steps
 *    nest under the call that entered them (5a, 5b), never on the root's
 *    lines;
 *  - a node used several times carries all its numbers on ONE chip;
 *  - outcome → THE connection status mapping, with a distinct hold marker;
 *  - untouched items are not in `touched` (dimmed); an undeclared target is
 *    added, marked, in its lane (created if the composition has none);
 *  - a crowded run collapses to ranges / "+N" and still never overlaps and
 *    never outgrows the 1280 px budget;
 *  - metadata only.
 */

import { describe, expect, it } from 'vitest';

import { buildCompositionModel } from '../composition';
import { CONNECTION_STATUS_META, RUN_OUTCOME_META, runOutcomeClass } from '../compositionConnection';
import {
    applyRunOverlay,
    buildChip,
    chipWidth,
    overlayLayout,
    runActionDisplayOrder,
    runOverlayRoute,
} from '../compositionRunOverlay';
import {
    SIMPLE_GRAPH_SIZES,
    SIMPLE_GRAPH_WIDTH_BUDGET,
    buildSimpleGraph,
    layoutSimpleGraph,
    type Rect,
} from '../compositionSimpleGraph';
import type { AISystemCompositionView, RunDetail } from '../aiSystemTypes';
import { COMPOSITION_PAYLOAD } from '../../components/ai-systems/__tests__/fixtures/compositionPayload';
import { OVERLAY_RUN, SENTINEL, crowdedRun } from '../../components/ai-systems/__tests__/fixtures/runOverlay';

const VIEW: AISystemCompositionView = COMPOSITION_PAYLOAD;
const graph = () => buildSimpleGraph(VIEW, buildCompositionModel(VIEW));
const apply = (run: RunDetail = OVERLAY_RUN) => applyRunOverlay(graph(), run);

const overlaps = (a: Rect, b: Rect) =>
    a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe('numbering — the control plane’s, in its order', () => {
    it('lists every action top level first, each call followed by the steps it entered', () => {
        const { overlay } = apply();
        expect(overlay.actions.map((a) => a.number)).toEqual(['1', '2', '3', '4', '5', '5a', '5b', '6', '7', '8', '9']);
        // The one ordering the Runs timeline uses too.
        expect(runActionDisplayOrder(OVERLAY_RUN.actions ?? []).map((a) => a.number)).toEqual(
            overlay.actions.map((a) => a.number)
        );
    });

    it('never renumbers: a payload without numbers keeps its order in the timeline', () => {
        const bare = (OVERLAY_RUN.actions ?? []).map((a) => ({ ...a, number: null }));
        expect(runActionDisplayOrder(bare)).toBe(bare);
    });
});

describe('badges on the connector each action travelled', () => {
    it('a node used several times lists all its numbers on one chip', () => {
        const { overlay } = apply();
        expect(overlay.badgesByItem['mcp:mcp-core']).toEqual(['1', '3', '7']);
        expect(overlay.chips['mcp:mcp-core'].map((t) => t.text)).toEqual(['1', '3', '7']);
        expect(overlay.badgesByItem['mcp:mcp-bureau']).toEqual(['2']);
        expect(overlay.badgesByItem['skill:skill-affordability']).toEqual(['4']);
        expect(overlay.badgesByItem['dep:dep-fraud']).toEqual(['5']);
        // A retrieval: the model's line AND the knowledge base's carry it.
        expect(overlay.badgesByItem['model:llm-sonnet']).toEqual(['6']);
        expect(overlay.badgesByItem['kb:kb-policy']).toEqual(['6']);
        // Matched by the delegate system on the observed-undeclared node.
        expect(overlay.badgesByItem['undeclared:sys-kyc']).toEqual(['9']);
    });

    it('labels each action with its target: tool, skill · script, model, A2A call', () => {
        const byN = Object.fromEntries(apply().overlay.actions.map((a) => [a.number, a]));
        expect([byN['1'].kindLabel, byN['1'].target, byN['1'].via]).toEqual(['MCP tool', 'applications_get', 'Loan Core']);
        expect([byN['4'].kindLabel, byN['4'].target]).toEqual(['Skill run', 'affordability-check · affordability.py']);
        expect([byN['6'].kindLabel, byN['6'].target]).toEqual(['Model call', 'claude-sonnet-5']);
        expect([byN['5'].kindLabel, byN['5'].target, byN['5'].via]).toEqual([
            'A2A call',
            'screening.fraud_sanctions',
            'Fraud Screener',
        ]);
        expect([byN['1'].turn, byN['5a'].turn, byN['1'].stepName]).toEqual(['1', '~2', 'Gather the application facts']);
    });

    it('a delegate’s own steps are sub-steps under the delegation, not on the root’s lines', () => {
        const { overlay } = apply();
        expect(overlay.insets['dep:dep-fraud'].map((a) => [a.number, a.target, a.delegateOf])).toEqual([
            ['5a', 'gpt-5-mini', 'Fraud Screener'],
            ['5b', 'sanctions_check', 'Fraud Screener'],
        ]);
        // gpt-5-mini is declared on the ROOT too — the delegate's call is not
        // the root's connection, so the root's line to it stays untouched.
        expect(overlay.badgesByItem['model:llm-mini']).toBeUndefined();
        expect(overlay.touched.has('model:llm-mini')).toBe(false);
        expect(Object.values(overlay.badgesByItem).flat()).not.toContain('5a');
    });
});

describe('outcome colours — THE connection status mapping, plus a hold marker', () => {
    it('answered = ok, error = degraded, blocked = down, held = down with its own glyph', () => {
        expect(RUN_OUTCOME_META.answered.status).toBe('ok');
        expect(RUN_OUTCOME_META.error.status).toBe('degraded');
        expect(RUN_OUTCOME_META.blocked.status).toBe('down');
        expect(RUN_OUTCOME_META.held.status).toBe('down');
        for (const o of ['answered', 'error', 'blocked', 'held'] as const) {
            expect(runOutcomeClass(o)).toBe(CONNECTION_STATUS_META[RUN_OUTCOME_META[o].status].className);
        }
        // Colour is never alone: every non-answered outcome has a glyph, and
        // a hold's differs from a block's.
        expect(RUN_OUTCOME_META.held.glyph).toBeTruthy();
        expect(RUN_OUTCOME_META.held.glyph).not.toBe(RUN_OUTCOME_META.blocked.glyph);
        expect(RUN_OUTCOME_META.error.glyph).toBeTruthy();
    });

    it('each chip token carries its action’s outcome', () => {
        const { overlay } = apply();
        expect(overlay.chips['mcp:mcp-core'].map((t) => t.outcome)).toEqual(['answered', 'answered', 'error']);
        expect(overlay.chips['mcp:mcp-bureau'][0].outcome).toBe('held');
    });
});

describe('what the run did not touch, and what it touched that is not declared', () => {
    it('only reached items are touched; the rest are dimmed', () => {
        const { overlay, graph: g } = apply();
        const all = g.lanes.flatMap((l) => l.items.map((i) => i.key));
        expect(all.filter((k) => !overlay.touched.has(k)).sort()).toEqual(
            ['dep:card:card-weather', 'dep:dep-address', 'dep:dep-bureau', 'model:llm-mini'].sort()
        );
        expect([...overlay.touchedLanes].sort()).toEqual(['a2a', 'kb', 'llm', 'mcp', 'skills']);
    });

    it('an undeclared target is added to its lane and marked', () => {
        const { overlay, graph: g } = apply();
        const mcp = g.lanes.find((l) => l.id === 'mcp');
        const shadow = mcp?.items.find((i) => i.label === 'Shadow CRM');
        expect(shadow).toMatchObject({ undeclared: true, note: 'Not in the declared composition' });
        expect(overlay.undeclared.has(shadow?.key ?? '')).toBe(true);
        expect(overlay.badgesByItem[shadow?.key ?? '']).toEqual(['8']);
        // The static graph is not mutated.
        expect(graph().lanes.find((l) => l.id === 'mcp')?.items.some((i) => i.undeclared)).toBe(false);
    });

    it('a lane the composition lacks is created, in lane order, and marked', () => {
        const noSkills: AISystemCompositionView = {
            ...VIEW,
            resources: { ...VIEW.resources, skills: [] },
        };
        const g = buildSimpleGraph(noSkills, buildCompositionModel(noSkills));
        expect(g.lanes.map((l) => l.id)).not.toContain('skills');
        const { graph: out, overlay } = applyRunOverlay(g, OVERLAY_RUN);
        expect(out.lanes.map((l) => l.id)).toEqual(['llm', 'mcp', 'a2a', 'skills', 'kb']);
        const skills = out.lanes.find((l) => l.id === 'skills');
        expect(skills?.undeclared).toBe(true);
        expect(overlay.badgesByItem['run:skills:skill-affordability']).toEqual(['4']);
    });
});

describe('the sequence list groups by step', () => {
    it('one header per step; a delegate’s steps stay under their call’s step', () => {
        const { overlay } = apply();
        expect(overlay.groups.map((g) => [g.title, g.actions.map((a) => a.number)])).toEqual([
            ['Gather the application facts', ['1', '2', '3', '4']],
            ['Assess risk and fraud', ['5', '5a', '5b', '6']],
            ['Decide and record', ['7', '8', '9']],
        ]);
    });
});

describe('a crowded run stays readable', () => {
    it('collapses consecutive numbers on a line to ranges ("7–12")', () => {
        const run = crowdedRun(60, (i) =>
            i >= 7 && i <= 12
                ? { proxy_type: 'llm', model: 'claude-sonnet-5', target_kind: 'model', target_id: 'llm-sonnet' }
                : { target_kind: 'mcp', target_id: i % 2 ? 'srv-core' : 'srv-bureau', capability: 'applications_get' }
        );
        const { overlay } = apply(run);
        expect(overlay.chips['model:llm-sonnet'].map((t) => t.text)).toEqual(['7–12']);
        expect(overlay.chips['model:llm-sonnet'][0].numbers).toHaveLength(6);
        // The list shows every action.
        expect(overlay.actions).toHaveLength(60);
    });

    it('folds what does not fit into "+N"; the chip never outgrows the fan', () => {
        const odd = Array.from({ length: 30 }, (_, k) => String(2 * k + 1));
        const tokens = buildChip(odd, () => 'answered');
        expect(tokens[tokens.length - 1].text).toMatch(/^\+\d+$/);
        expect(tokens.flatMap((t) => t.numbers)).toEqual(odd);
        expect(chipWidth(tokens)).toBeLessThanOrEqual(SIMPLE_GRAPH_SIZES.chipMaxW);
    });

    it('lays out 60 actions with no overlap and within the 1280 px budget', () => {
        const run = crowdedRun(60, (i) => {
            const pick = i % 5;
            if (pick === 0) return { proxy_type: 'llm', target_kind: 'model', target_id: i % 2 ? 'llm-sonnet' : 'llm-mini' };
            if (pick === 1) return { target_kind: 'skill', target_id: 'skill-affordability' };
            if (pick === 2) return { proxy_type: 'a2a', target_kind: 'a2a', target_id: 'card-fraud', delegate_system_id: 'sys-fraud' };
            return { target_kind: 'mcp', target_id: pick === 3 ? 'srv-core' : 'srv-bureau' };
        });
        // Plus a delegation with many sub-steps: the inset caps its rows.
        const nested = Array.from({ length: 9 }, (_, k) => ({
            ...(run.actions ?? [])[0],
            id: `n${k}`,
            number: `3${'abcdefghi'[k]}`,
            entered_by: '3',
            nesting: 1,
            own: false,
            target_kind: 'model' as const,
            target_id: 'llm-mini',
        }));
        const { graph: g, overlay } = apply({ ...run, actions: [...(run.actions ?? []), ...nested] });
        const layout = layoutSimpleGraph(g, overlayLayout(overlay));
        expect(layout.width).toBeLessThanOrEqual(SIMPLE_GRAPH_WIDTH_BUDGET);

        const chips = Object.values(layout.chips);
        const cells = layout.lanes.flatMap((l) => l.items.map((i) => i.cell));
        const insets = layout.lanes.flatMap((l) => l.items.flatMap((i) => (i.inset ? [i.inset] : [])));
        const boxes = layout.lanes.map((l) => l.box);
        expect(chips.length).toBeGreaterThan(3);
        for (const [i, a] of chips.entries()) {
            expect(a.w).toBeLessThanOrEqual(SIMPLE_GRAPH_SIZES.chipMaxW);
            for (const b of chips.slice(i + 1)) expect(overlaps(a, b)).toBe(false);
            for (const c of [...cells, ...boxes, layout.gateway]) expect(overlaps(a, c)).toBe(false);
        }
        // Insets sit inside their own cell, clear of the next item.
        const withInset = layout.lanes.flatMap((l) => l.items).filter((it) => it.inset);
        expect(withInset).toHaveLength(1);
        for (const it of withInset) {
            const inset = it.inset as Rect;
            expect(inset.y + inset.h).toBeLessThanOrEqual(it.cell.y + it.cell.h);
            expect(inset.x + inset.w).toBeLessThanOrEqual(it.cell.x + it.cell.w);
        }
        expect(insets.length).toBe(1);
        for (const [i, a] of cells.entries()) for (const b of cells.slice(i + 1)) expect(overlaps(a, b)).toBe(false);
    });
});

describe('a capped run says so', () => {
    it('reports shown vs the run’s action_count', () => {
        const { overlay } = apply({ ...OVERLAY_RUN, action_count: 700 });
        expect([overlay.shown, overlay.total]).toEqual([11, 700]);
        expect(overlay.summary).toContain('the first 11 of 700 actions are shown');
    });
});

describe('metadata only', () => {
    it('no argument, prompt, response or error text reaches the overlay model', () => {
        const { overlay } = apply();
        const json = JSON.stringify(overlay, (_k, v: unknown) => (v instanceof Set ? [...v] : v));
        expect(json).not.toContain(SENTINEL);
    });
});

describe('the deep link', () => {
    it('is the app’s route pattern with the run as its last segment', () => {
        expect(runOverlayRoute('sys-demo', 'run-1')).toBe('ai-systems:sys-demo:composition:run-1');
        expect(runOverlayRoute('sys-demo', null)).toBe('ai-systems:sys-demo:composition');
    });
});
