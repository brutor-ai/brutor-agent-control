/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's mirror of the control plane's relaxation refusal. Every case
 * here is a value a child group may NOT set — and, just as importantly, the
 * cases that only tighten and must be left alone. A false positive here
 * blocks a legitimate save while the operator types.
 */

import { describe, it, expect } from 'vitest';
import {
    GOVERNANCE_RULES,
    governanceRelaxations,
    relaxationsByPath,
} from '../governanceInheritance';

describe('governanceRelaxations — min_wins', () => {
    const inherited = { temperature: { max: 0.7 } };

    it('flags a numeric ceiling raised above the inherited one', () => {
        const found = governanceRelaxations({ temperature: { max: 1.2 } }, inherited, 'llm');
        expect(found).toHaveLength(1);
        expect(found[0].path).toBe('temperature.max');
        expect(found[0].message).toContain('0.7');
    });

    it('leaves an equal or tighter value alone', () => {
        expect(governanceRelaxations({ temperature: { max: 0.7 } }, inherited, 'llm')).toEqual([]);
        expect(governanceRelaxations({ temperature: { max: 0.2 } }, inherited, 'llm')).toEqual([]);
    });

    it('ignores a half-typed field rather than flagging NaN as a relaxation', () => {
        expect(governanceRelaxations({ temperature: { max: NaN } }, inherited, 'llm')).toEqual([]);
    });

    it('ignores a non-numeric value on either side', () => {
        expect(governanceRelaxations({ temperature: { max: '1.2' } }, inherited, 'llm')).toEqual([]);
        expect(
            governanceRelaxations({ temperature: { max: 1.2 } }, { temperature: { max: 'x' } }, 'llm'),
        ).toEqual([]);
    });
});

describe('governanceRelaxations — strictness', () => {
    it('flags a looser enforce mode', () => {
        const found = governanceRelaxations(
            { temperature: { enforce: 'clamp' } },
            { temperature: { enforce: 'reject' } },
            'llm',
        );
        expect(found).toHaveLength(1);
        expect(found[0].path).toBe('temperature.enforce');
        expect(found[0].message).toContain('reject');
    });

    it('allows tightening from clamp to reject', () => {
        expect(
            governanceRelaxations(
                { temperature: { enforce: 'reject' } },
                { temperature: { enforce: 'clamp' } },
                'llm',
            ),
        ).toEqual([]);
    });

    it('ranks a model\'s tool access enabled < disabled', () => {
        const looser = governanceRelaxations(
            { tool_use: { access: 'enabled' } },
            { tool_use: { access: 'disabled' } },
            'llm',
        );
        expect(looser).toHaveLength(1);
        expect(
            governanceRelaxations(
                { tool_use: { access: 'disabled' } },
                { tool_use: { access: 'enabled' } },
                'llm',
            ),
        ).toEqual([]);
    });

    it('has no approval among a model\'s tool-use rules: no approval timeout, and approval_required is not a rank', () => {
        expect(GOVERNANCE_RULES.llm.map((rule) => rule.path)).not.toContain('tool_use.approval_timeout_seconds');
        const access = GOVERNANCE_RULES.llm.find((rule) => rule.path === 'tool_use.access');
        expect(Object.keys(access?.ordering ?? {})).toEqual(['enabled', 'disabled']);
    });

    it('ranks an MCP capability\'s access enabled < approval_required < disabled', () => {
        expect(
            governanceRelaxations({ default_tool_access: 'enabled' }, { default_tool_access: 'approval_required' }, 'mcp'),
        ).toHaveLength(1);
        expect(
            governanceRelaxations({ default_tool_access: 'disabled' }, { default_tool_access: 'approval_required' }, 'mcp'),
        ).toEqual([]);
    });

    it('renders the inherited token without underscores', () => {
        const found = governanceRelaxations(
            { default_tool_access: 'enabled' },
            { default_tool_access: 'approval_required' },
            'mcp',
        );
        expect(found[0].message).toContain('approval required');
        expect(found[0].message).not.toContain('approval_required');
    });

    it('does not flag a non-string value it cannot rank', () => {
        expect(
            governanceRelaxations(
                { default_tool_access: { mode: 'enabled' } },
                { default_tool_access: 'disabled' },
                'mcp',
            ),
        ).toEqual([]);
    });
});

describe('governanceRelaxations — bool_and', () => {
    it('refuses to re-enable discovery a parent switched off', () => {
        const found = governanceRelaxations(
            { discovery_enabled: true },
            { discovery_enabled: false },
            'skill',
        );
        expect(found).toHaveLength(1);
        expect(found[0].message).toContain('cannot be re-enabled');
    });

    it('lets a child switch discovery off under an enabled parent', () => {
        expect(
            governanceRelaxations(
                { discovery_enabled: false },
                { discovery_enabled: true },
                'skill',
            ),
        ).toEqual([]);
    });
});

describe('governanceRelaxations — list_subset', () => {
    it('names the tools the inherited allowlist does not permit', () => {
        const found = governanceRelaxations(
            { tool_use: { allowed_tools: ['read', 'write', 'delete'] } },
            { tool_use: { allowed_tools: ['read'] } },
            'llm',
        );
        expect(found).toHaveLength(1);
        expect(found[0].message).toContain('write, delete');
    });

    it('accepts a strict subset and an identical list', () => {
        expect(
            governanceRelaxations(
                { tool_use: { allowed_tools: ['read'] } },
                { tool_use: { allowed_tools: ['read', 'write'] } },
                'llm',
            ),
        ).toEqual([]);
    });

    it('an empty child list adds nothing and is not a relaxation', () => {
        expect(
            governanceRelaxations(
                { tool_use: { allowed_tools: [] } },
                { tool_use: { allowed_tools: ['read'] } },
                'llm',
            ),
        ).toEqual([]);
    });
});

describe('governanceRelaxations — nothing to compare', () => {
    it('is empty when there is no inherited policy', () => {
        expect(governanceRelaxations({ temperature: { max: 9 } }, null, 'llm')).toEqual([]);
        expect(governanceRelaxations({ temperature: { max: 9 } }, undefined, 'llm')).toEqual([]);
        expect(governanceRelaxations(null, { temperature: { max: 0.1 } }, 'llm')).toEqual([]);
    });

    it('skips fields absent or null on either side', () => {
        expect(governanceRelaxations({}, { temperature: { max: 0.1 } }, 'llm')).toEqual([]);
        expect(
            governanceRelaxations({ temperature: { max: 9 } }, { temperature: { max: null } }, 'llm'),
        ).toEqual([]);
    });

    it('survives a non-object standing in for a nested section', () => {
        expect(
            governanceRelaxations({ temperature: 'hot' }, { temperature: { max: 0.1 } }, 'llm'),
        ).toEqual([]);
    });

    it('reports every offending field on one blob', () => {
        const found = governanceRelaxations(
            { execution_mode: 'unrestricted', approval_timeout_seconds: 600, max_concurrent_executions: 50 },
            { execution_mode: 'disabled', approval_timeout_seconds: 60, max_concurrent_executions: 5 },
            'skill',
        );
        expect(found.map((f) => f.path).sort()).toEqual([
            'approval_timeout_seconds',
            'execution_mode',
            'max_concurrent_executions',
        ]);
    });
});

describe('relaxationsByPath', () => {
    it('indexes by path so a form can mark the exact field', () => {
        const found = governanceRelaxations(
            { temperature: { max: 1.2 } },
            { temperature: { max: 0.7 } },
            'llm',
        );
        const byPath = relaxationsByPath(found);
        expect(byPath['temperature.max'].label).toBe('Max Temperature');
        expect(byPath['temperature.enforce']).toBeUndefined();
    });

    it('is an empty map for no relaxations', () => {
        expect(relaxationsByPath([])).toEqual({});
    });
});

describe('GOVERNANCE_RULES', () => {
    it('gives every strictness rule an ordering to rank by', () => {
        // Without an ordering every token ranks 0, so no relaxation is ever
        // detected on that field — the check silently does nothing.
        const unranked = (['llm', 'mcp', 'skill'] as const).flatMap((surface) =>
            GOVERNANCE_RULES[surface]
                .filter((rule) => rule.kind === 'strictness' && !rule.ordering)
                .map((rule) => `${surface}.${rule.path}`),
        );
        expect(unranked).toEqual([]);
    });

    it('omits union-merged fields — a child can only add to those', () => {
        const paths = GOVERNANCE_RULES.llm.map((r) => r.path);
        expect(paths).not.toContain('tool_use.blocked_tools');
        expect(paths).not.toContain('banned_patterns');
    });
});
