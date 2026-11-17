/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure-function tests for the delegation-graph tree builder.
 *
 * These cover the shapes the server actually returns on the list
 * endpoint — single-step roots, linear chains, fan-outs, and orphan
 * detection — so the rendered Mission Control view stays consistent
 * with the data.
 */

import { describe, it, expect } from 'vitest';

import type { A2ADelegationStep } from '../../../lib/a2aTypes';
import { buildTree } from '../delegationGraphTree';

function step(overrides: Partial<A2ADelegationStep>): A2ADelegationStep {
    return {
        current_task_id: overrides.current_task_id ?? 'node',
        parent_task_id: overrides.parent_task_id ?? null,
        depth: overrides.depth ?? 0,
        direction: overrides.direction ?? 'outbound',
        proxy_log_id: overrides.proxy_log_id ?? `log-${Math.random()}`,
        timestamp: overrides.timestamp ?? new Date().toISOString(),
        duration_ms: overrides.duration_ms ?? 100,
        http_status_code: overrides.http_status_code ?? 200,
        success: overrides.success ?? true,
        error_message: overrides.error_message ?? null,
        card_id: overrides.card_id ?? null,
        endpoint: overrides.endpoint ?? '/v1/proxy/a2a/outbound',
        target_url: overrides.target_url ?? null,
        correlation_id: overrides.correlation_id ?? null,
    };
}

describe('buildTree', () => {
    it('returns empty array for empty input', () => {
        expect(buildTree([])).toEqual([]);
    });

    it('produces one node for a single-step graph', () => {
        const nodes = buildTree([step({ current_task_id: 'a', depth: 0 })]);
        expect(nodes.length).toBe(1);
        expect(nodes[0].step.current_task_id).toBe('a');
        expect(nodes[0].indent).toBe(0);
        expect(nodes[0].detached).toBe(false);
    });

    it('nests parent → child in a linear chain', () => {
        const t0 = new Date('2026-01-01T00:00:00Z').toISOString();
        const t1 = new Date('2026-01-01T00:00:01Z').toISOString();
        const nodes = buildTree([
            step({ current_task_id: 'child', parent_task_id: 'root', depth: 1, timestamp: t1 }),
            step({ current_task_id: 'root', depth: 0, timestamp: t0 }),
        ]);
        expect(nodes.map(n => n.step.current_task_id)).toEqual(['root', 'child']);
        expect(nodes.map(n => n.indent)).toEqual([0, 1]);
    });

    it('orders fan-out siblings by timestamp', () => {
        const t0 = new Date('2026-01-01T00:00:00Z').toISOString();
        const tA = new Date('2026-01-01T00:00:02Z').toISOString();
        const tB = new Date('2026-01-01T00:00:01Z').toISOString();
        const nodes = buildTree([
            step({ current_task_id: 'root', depth: 0, timestamp: t0 }),
            step({ current_task_id: 'later', parent_task_id: 'root', depth: 1, timestamp: tA }),
            step({ current_task_id: 'earlier', parent_task_id: 'root', depth: 1, timestamp: tB }),
        ]);
        expect(nodes.map(n => n.step.current_task_id)).toEqual(['root', 'earlier', 'later']);
    });

    it('flags orphans without losing them', () => {
        const nodes = buildTree([
            step({ current_task_id: 'root', depth: 0 }),
            step({ current_task_id: 'orphan', parent_task_id: 'missing', depth: 2 }),
        ]);
        // Root renders normally.
        expect(nodes[0].step.current_task_id).toBe('root');
        expect(nodes[0].detached).toBe(false);
        // Orphan appears at the end marked detached, indent bumped.
        const orphanNode = nodes.find(n => n.step.current_task_id === 'orphan');
        expect(orphanNode).toBeDefined();
        expect(orphanNode?.detached).toBe(true);
        expect(orphanNode?.indent).toBe(3);
    });

    it('handles multiple roots sorted by timestamp', () => {
        const t0 = new Date('2026-01-01T00:00:00Z').toISOString();
        const t1 = new Date('2026-01-01T00:00:01Z').toISOString();
        const nodes = buildTree([
            step({ current_task_id: 'root2', depth: 0, timestamp: t1 }),
            step({ current_task_id: 'root1', depth: 0, timestamp: t0 }),
        ]);
        expect(nodes.map(n => n.step.current_task_id)).toEqual(['root1', 'root2']);
    });

    it('preserves children beneath each root in a forest', () => {
        const t0 = new Date('2026-01-01T00:00:00Z').toISOString();
        const nodes = buildTree([
            step({ current_task_id: 'a', depth: 0, timestamp: t0 }),
            step({ current_task_id: 'b', depth: 0, timestamp: t0 }),
            step({ current_task_id: 'a-child', parent_task_id: 'a', depth: 1, timestamp: t0 }),
            step({ current_task_id: 'b-child', parent_task_id: 'b', depth: 1, timestamp: t0 }),
        ]);
        const ids = nodes.map(n => n.step.current_task_id);
        // Root a comes with its child before root b starts.
        expect(ids.indexOf('a-child')).toBeLessThan(ids.indexOf('b'));
        expect(ids.indexOf('b-child')).toBeGreaterThan(ids.indexOf('b'));
    });
});
