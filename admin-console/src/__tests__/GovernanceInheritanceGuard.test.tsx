/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A child group may tighten the policy it inherits, never loosen it.
 *
 * A looser value must not be accepted in silence. If a department under an
 * organisation capped at temperature 1.0 could store 1.4, nothing would
 * enforce it — the runtime merge is most-restrictive-wins — so the group's
 * page would show a policy the platform has discarded, and that value
 * would become real the day the organisation relaxed its own.
 *
 * These tests pin the two halves of the guard: the rule table that decides
 * what "looser" means for each kind of field, and the form behaviour that
 * marks the field, explains it, and blocks Save.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import {
    GOVERNANCE_RULES,
    governanceRelaxations,
    relaxationsByPath,
} from '../lib/governanceInheritance';
import type { LLMGovernance } from '../lib/groupTypes';
import LLMGovernanceForm from '../components/groups/subtabs/LLMGovernanceForm';
import GovernanceRelaxationNotice from '../components/groups/limits/GovernanceRelaxationNotice';

describe('governanceRelaxations', () => {
    it('accepts a child value that tightens the inherited ceiling', () => {
        expect(
            governanceRelaxations({ temperature: { max: 0.4 } }, { temperature: { max: 1 } }, 'llm'),
        ).toEqual([]);
    });

    it('accepts a child value equal to the ceiling', () => {
        expect(
            governanceRelaxations({ temperature: { max: 1 } }, { temperature: { max: 1 } }, 'llm'),
        ).toEqual([]);
    });

    it('flags the temperature the operator actually hit', () => {
        const found = governanceRelaxations(
            { temperature: { max: 1.4 } },
            { temperature: { max: 1 } },
            'llm',
        );
        expect(found).toHaveLength(1);
        expect(found[0].path).toBe('temperature.max');
        expect(found[0].label).toBe('Max Temperature');
        expect(found[0].message).toContain('inherited maximum of 1');
    });

    it('compares enum fields on strictness, not alphabetically', () => {
        expect(
            governanceRelaxations(
                { temperature: { enforce: 'clamp' } },
                { temperature: { enforce: 'reject' } },
                'llm',
            ),
        ).toHaveLength(1);
        expect(
            governanceRelaxations(
                { temperature: { enforce: 'reject' } },
                { temperature: { enforce: 'clamp' } },
                'llm',
            ),
        ).toEqual([]);
    });

    it('refuses re-enabling tool access a parent restricted', () => {
        const found = governanceRelaxations(
            { tool_use: { access: 'enabled' } },
            { tool_use: { access: 'disabled' } },
            'llm',
        );
        expect(found).toHaveLength(1);
        expect(found[0].label).toBe('Tool Access');
    });

    it('refuses naming a tool the inherited policy does not allow', () => {
        const found = governanceRelaxations(
            { tool_use: { allowed_tools: ['read', 'delete'] } },
            { tool_use: { allowed_tools: ['read', 'write'] } },
            'llm',
        );
        expect(found).toHaveLength(1);
        expect(found[0].message).toContain('delete');
    });

    it('never flags union fields, which a child can only add to', () => {
        expect(
            governanceRelaxations(
                { tool_use: { blocked_tools: [] }, system_prompt: { banned_patterns: [] } },
                { tool_use: { blocked_tools: ['shell'] }, system_prompt: { banned_patterns: ['ssn'] } },
                'llm',
            ),
        ).toEqual([]);
    });

    it('handles the flat MCP and skill blobs', () => {
        expect(
            governanceRelaxations(
                { default_tool_access: 'enabled', approval_timeout_seconds: 900 },
                { default_tool_access: 'disabled', approval_timeout_seconds: 300 },
                'mcp',
            ),
        ).toHaveLength(2);
        const skill = governanceRelaxations(
            { discovery_enabled: true },
            { discovery_enabled: false },
            'skill',
        );
        expect(skill).toHaveLength(1);
        expect(skill[0].message).toContain('cannot be re-enabled');
    });

    it('covers every skill field the runtime restricts', () => {
        // The proxy and the control plane both restrict all five schema
        // fields; a rule table with fewer would report an effective value
        // the runtime does not use.
        const skillPaths = GOVERNANCE_RULES.skill.map((r) => r.path).sort();
        expect(skillPaths).toEqual([
            'approval_timeout_seconds',
            'discovery_enabled',
            'execution_mode',
            'max_concurrent_executions',
            'max_tool_calls_per_execution',
        ]);
        expect(
            governanceRelaxations(
                { max_concurrent_executions: 99 },
                { max_concurrent_executions: 2 },
                'skill',
            ),
        ).toHaveLength(1);
        expect(
            governanceRelaxations(
                { max_tool_calls_per_execution: 50 },
                { max_tool_calls_per_execution: 5 },
                'skill',
            ),
        ).toHaveLength(1);
        expect(
            governanceRelaxations(
                { max_concurrent_executions: 1 },
                { max_concurrent_executions: 4 },
                'skill',
            ),
        ).toEqual([]);
    });

    it('has nothing to say when the group inherits no policy', () => {
        expect(governanceRelaxations({ temperature: { max: 2 } }, null, 'llm')).toEqual([]);
        expect(governanceRelaxations({ temperature: { max: 2 } }, {}, 'llm')).toEqual([]);
    });

    it('mirrors the control plane rule set for every writable surface', () => {
        expect(Object.keys(GOVERNANCE_RULES).sort()).toEqual(['llm', 'mcp', 'skill']);
        for (const rules of Object.values(GOVERNANCE_RULES)) expect(rules.length).toBeGreaterThan(0);
    });

    it('indexes by path so a form can mark the exact field', () => {
        const byPath = relaxationsByPath(
            governanceRelaxations({ temperature: { max: 1.4 } }, { temperature: { max: 1 } }, 'llm'),
        );
        expect(byPath['temperature.max'].label).toBe('Max Temperature');
    });
});

describe('LLMGovernanceForm', () => {
    const inherited: LLMGovernance = { temperature: { max: 1, enforce: 'clamp' } };

    it('marks a looser value as it is typed and says what will happen', () => {
        const onRelaxationsChange = vi.fn();
        render(
            <LLMGovernanceForm
                value={{ temperature: { max: 1.4 } }}
                onChange={vi.fn()}
                inherited={inherited}
                effective={{ temperature: { max: 1 } }}
                onRelaxationsChange={onRelaxationsChange}
            />,
        );
        const input = screen.getByTestId('governance-input-temperature.max');
        expect(input).toHaveAttribute('aria-invalid', 'true');
        expect(screen.getByTestId('governance-error-temperature.max')).toHaveTextContent(
            'Above the inherited maximum of 1',
        );
        // A bare "Effective: 1" in grey would read as information rather
        // than a refusal.
        expect(screen.queryByText(/^Effective: 1$/)).not.toBeInTheDocument();
        expect(onRelaxationsChange).toHaveBeenCalledWith([
            expect.objectContaining({ path: 'temperature.max' }),
        ]);
    });

    it('stays quiet while every field is within the inherited policy', () => {
        const onRelaxationsChange = vi.fn();
        render(
            <LLMGovernanceForm
                value={{ temperature: { max: 0.4 } }}
                onChange={vi.fn()}
                inherited={inherited}
                onRelaxationsChange={onRelaxationsChange}
            />,
        );
        expect(screen.getByTestId('governance-input-temperature.max')).not.toHaveAttribute('aria-invalid');
        expect(screen.queryByTestId('governance-error-temperature.max')).not.toBeInTheDocument();
        expect(onRelaxationsChange).toHaveBeenCalledWith([]);
    });

    it('emits the new value on edit so the tab can re-check it', () => {
        const onChange = vi.fn();
        render(
            <LLMGovernanceForm value={{}} onChange={onChange} inherited={inherited} />,
        );
        fireEvent.change(screen.getByTestId('governance-input-temperature.max'), {
            target: { value: '1.4' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ temperature: expect.objectContaining({ max: 1.4 }) }),
        );
    });

    it('shows no inherited badge on a group with no ancestor policy', () => {
        render(<LLMGovernanceForm value={{ temperature: { max: 1.8 } }} onChange={vi.fn()} inherited={null} />);
        expect(screen.queryByTestId('governance-error-temperature.max')).not.toBeInTheDocument();
    });
});

describe('GovernanceRelaxationNotice', () => {
    it('renders nothing when the policy is clean', () => {
        const { container } = render(<GovernanceRelaxationNotice relaxations={[]} />);
        expect(container).toBeEmptyDOMElement();
    });

    it('names every offending field and says saving is blocked', () => {
        render(
            <GovernanceRelaxationNotice
                relaxations={[
                    { path: 'temperature.max', label: 'Max Temperature', message: 'Above the inherited maximum of 1 — the save will be rejected' },
                    { path: 'tool_use.access', label: 'Tool Access', message: 'Looser than the inherited "disabled" — the save will be rejected' },
                ]}
            />,
        );
        const notice = screen.getByTestId('governance-relaxations');
        expect(notice).toHaveTextContent('2 fields are looser');
        expect(notice).toHaveTextContent('Saving is blocked');
        expect(notice).toHaveTextContent('Max Temperature');
        expect(notice).toHaveTextContent('Tool Access');
    });
});
