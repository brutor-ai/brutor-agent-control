/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Graph-tree reconstruction for the A2A delegation graph detail view.
 *
 * Kept apart from `A2ADelegationGraphDetail.tsx` so the pure logic is
 * easy to unit-test in isolation. The component calls `buildTree()`
 * with the server-provided step list and renders the resulting flat
 * array; this module owns the parent/child ordering.
 */

import type { A2ADelegationStep } from '../../lib/a2aTypes';

export interface TreeNode {
    step: A2ADelegationStep;
    /** Indent level used for visual nesting. For roots this is the step's
     * own depth; for orphans (parent unknown) it's the step's own depth
     * plus one to mark them as detached. */
    indent: number;
    detached: boolean;
}

/**
 * Flatten the step list into a depth-first traversal ordered the way
 * an operator would expect to read the graph: each root first, then
 * each child in ascending timestamp order, recursing.
 *
 * Orphaned steps (parent_task_id set but no matching current_task_id in
 * the input) are surfaced at the end marked `detached`, rather than
 * silently dropped.
 */
export function buildTree(steps: A2ADelegationStep[]): TreeNode[] {
    const byParent = new Map<string, A2ADelegationStep[]>();
    const roots: A2ADelegationStep[] = [];
    const knownIds = new Set<string>();

    for (const h of steps) {
        if (h.current_task_id) knownIds.add(h.current_task_id);
    }

    for (const h of steps) {
        if (!h.parent_task_id) {
            roots.push(h);
        } else {
            const arr = byParent.get(h.parent_task_id) ?? [];
            arr.push(h);
            byParent.set(h.parent_task_id, arr);
        }
    }

    const sortByTs = (a: A2ADelegationStep, b: A2ADelegationStep) =>
        new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime();
    roots.sort(sortByTs);
    for (const list of byParent.values()) list.sort(sortByTs);

    const out: TreeNode[] = [];
    function walk(step: A2ADelegationStep, indent: number) {
        out.push({ step, indent, detached: false });
        const kids = step.current_task_id ? byParent.get(step.current_task_id) : undefined;
        if (kids) {
            for (const k of kids) walk(k, indent + 1);
        }
    }
    for (const r of roots) walk(r, r.depth);

    for (const h of steps) {
        if (h.parent_task_id && !knownIds.has(h.parent_task_id)) {
            out.push({ step: h, indent: h.depth + 1, detached: true });
        }
    }
    return out;
}
