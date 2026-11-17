/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which argument policies the capability-filter screen must name as also
 * gating a tool. Over-matching invents a gate that isn't there; under-
 * matching hides one that is.
 */

import { describe, it, expect } from 'vitest';
import { policyAppliesToGroup, policyTargets } from '../argumentPolicyGates';
import type { ArgumentPolicy } from '../argumentPolicyTypes';

const policy = (over: Partial<ArgumentPolicy> = {}): ArgumentPolicy => ({
    id: 'p1',
    tenant_id: 't1',
    name: 'Transfer band',
    surface: 'mcp_input',
    target: 'transfer_funds',
    argument_key: 'arguments',
    analyzer: 'json',
    rules: [],
    enabled: true,
    severity: 'deny',
    group_ids: [],
    ...over,
});

describe('policyTargets', () => {
    it('matches the named tool', () => {
        expect(policyTargets(policy(), 'transfer_funds')).toBe(true);
        expect(policyTargets(policy(), 'read_balance')).toBe(false);
    });

    it('treats "*" as every tool on the surface', () => {
        expect(policyTargets(policy({ target: '*' }), 'anything_at_all')).toBe(true);
    });

    it('never claims a disabled policy gates anything', () => {
        expect(policyTargets(policy({ enabled: false }), 'transfer_funds')).toBe(false);
        expect(policyTargets(policy({ enabled: false, target: '*' }), 'transfer_funds')).toBe(false);
    });

    it('is scoped to the MCP input surface only', () => {
        expect(policyTargets(policy({ surface: 'skill_input' }), 'transfer_funds')).toBe(false);
        expect(
            policyTargets(policy({ surface: 'llm_output_tool_calls', target: '*' }), 'transfer_funds'),
        ).toBe(false);
    });

    it('does not match a partial tool name', () => {
        expect(policyTargets(policy({ target: 'transfer' }), 'transfer_funds')).toBe(false);
    });
});

describe('policyAppliesToGroup', () => {
    it('an unbound policy is tenant-wide and applies everywhere', () => {
        expect(policyAppliesToGroup(policy({ group_ids: [] }), 'g1')).toBe(true);
    });

    it('a bound policy applies only to the groups it names', () => {
        const p = policy({ group_ids: ['g1', 'g2'] });
        expect(policyAppliesToGroup(p, 'g2')).toBe(true);
        expect(policyAppliesToGroup(p, 'g3')).toBe(false);
    });

    it('treats a policy answered without group_ids as tenant-wide: the contract makes the field optional', () => {
        const unbound = { ...policy(), group_ids: undefined } as unknown as ArgumentPolicy;
        expect(policyAppliesToGroup(unbound, 'g1')).toBe(true);
    });
});
