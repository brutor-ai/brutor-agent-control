/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Argument policy form: the policy decides which tool calls an agent may make,
 * so what matters is the body on the wire and what stops a policy that would
 * look enforced without being enforced.
 *
 * The footer's submit button sits outside the <form>, so the browser's own
 * `required` checks never run; everything has to be caught in the handler.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gatewayClient = {
    fetchGroupHierarchyTree: vi.fn(),
    fetchServers: vi.fn(),
    listAgentSkills: vi.fn(),
};
const gateway = {
    gatewayClient,
    createArgumentPolicy: vi.fn(),
    updateArgumentPolicy: vi.fn(),
    setArgumentPolicyGroups: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/HierarchicalGroupSelector', () => ({
    default: ({
        selectedGroupIds,
        onSelectionChange,
    }: {
        selectedGroupIds: string[];
        onSelectionChange: (ids: string[]) => void;
    }) => (
        <button type="button" onClick={() => onSelectionChange([...selectedGroupIds, 'g-new'])}>
            pick group
        </button>
    ),
}));

import ArgumentPolicyForm from '../ArgumentPolicyForm';

/** The severities the Control Plane answers for each surface (`severities_by_surface` of the list). */
const SEVERITIES_BY_SURFACE = {
    llm_output_tool_calls: ['deny', 'warn'],
    mcp_input: ['deny', 'approval_required', 'warn'],
    skill_input: ['deny', 'approval_required', 'warn'],
} as const;
import type { ArgumentPolicy } from '../../../lib/argumentPolicyTypes';

const onClose = vi.fn();

const EXISTING: ArgumentPolicy = {
    id: 'ap-1',
    tenant_id: 't-1',
    name: 'pg-readonly',
    description: 'No writes',
    surface: 'mcp_input',
    target: 'query_db',
    argument_key: 'sql',
    analyzer: 'sql',
    analyzer_config: { dialect: 'postgresql' },
    rules: [{ deny_if: 'writes', message: 'Read only' }],
    enabled: false,
    severity: 'warn',
    group_ids: ['g-old'],
};

/** Labels here are not tied to their controls, so find a control by its label's block. */
function control<T extends HTMLElement>(label: string | RegExp, index = 0): T {
    const el = screen.getAllByText(label)[index].closest('label');
    const box = el?.parentElement?.querySelector<T>('select, input, textarea');
    if (!box) throw new Error(`no control for ${String(label)}`);
    return box;
}

const type = (label: string | RegExp, value: string, index = 0) =>
    fireEvent.change(control(label, index), { target: { value } });
const tab = (name: string) => fireEvent.click(screen.getByRole('button', { name }));
const submit = (name = 'Create Policy') => fireEvent.click(screen.getByRole('button', { name }));

/** Fill in the fields every policy needs. */
function fillBasics(name = 'orders-guard', argumentKey = 'sql') {
    fireEvent.change(screen.getByPlaceholderText('e.g. pg-readonly-analytics'), { target: { value: name } });
    fireEvent.change(screen.getByPlaceholderText('e.g. sql, url, command'), { target: { value: argumentKey } });
}

/** Go to a JSON policy on the whole-argument key, on the Rules tab. */
function startJsonPolicy() {
    fillBasics('transfer-band', '*');
    type('Analyzer', 'json');
    tab('Rules');
}

const created = () => gateway.createArgumentPolicy.mock.calls[0][0] as Record<string, unknown>;
const sentRules = () => created().rules as Record<string, unknown>[];

describe('ArgumentPolicyForm', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gatewayClient.fetchGroupHierarchyTree.mockResolvedValue({ tree: [{ id: 'g-1', name: 'Root' }] });
        gatewayClient.fetchServers.mockResolvedValue([]);
        gatewayClient.listAgentSkills.mockResolvedValue({ skills: {} });
        gateway.createArgumentPolicy.mockResolvedValue({ id: 'ap-new' });
        gateway.updateArgumentPolicy.mockResolvedValue({ id: 'ap-1' });
        gateway.setArgumentPolicyGroups.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('creating', () => {
        it('sends every field, trimmed, including the chosen groups', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('  orders-guard  ', ' sql ');
            fireEvent.change(screen.getByPlaceholderText(/Block writes/), { target: { value: '  desc  ' } });
            fireEvent.change(screen.getByPlaceholderText('postgres_query (or *)'), { target: { value: ' query_orders ' } });
            type('Surface', 'skill_input');
            type('Severity', 'warn');
            fireEvent.click(screen.getByRole('checkbox', { name: 'Enabled' }));

            tab('Access Control');
            fireEvent.click(await screen.findByRole('button', { name: 'pick group' }));
            expect(screen.getByText('1 group selected')).toBeInTheDocument();

            tab('Rules');
            fireEvent.change(screen.getByPlaceholderText(/Read-only analytics/), { target: { value: ' no writes ' } });

            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{ "dialect": "postgresql" }'), {
                target: { value: '{"dialect":"mysql"}' },
            });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(created()).toEqual({
                name: 'orders-guard',
                description: 'desc',
                surface: 'skill_input',
                target: 'query_orders',
                argument_key: 'sql',
                analyzer: 'sql',
                analyzer_config: { dialect: 'mysql' },
                rules: [
                    {
                        deny_if: 'writes',
                        allow_only_if: undefined,
                        values: undefined,
                        field: undefined,
                        value: undefined,
                        severity: undefined,
                        message: 'no writes',
                    },
                ],
                enabled: false,
                severity: 'warn',
                group_ids: ['g-new'],
            });
            await waitFor(() => expect(onClose).toHaveBeenCalledWith(true));
            expect(toast.success).toHaveBeenCalledWith('Policy Created', '"orders-guard" has been created.');
        });

        it('sends empties as null / wildcard rather than blank strings', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            fireEvent.change(screen.getByPlaceholderText('postgres_query (or *)'), { target: { value: '   ' } });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(created()).toMatchObject({
                description: null,
                target: '*',
                analyzer_config: null,
                group_ids: [],
                enabled: true,
                severity: 'deny',
            });
        });

        it('drops blank lines from a rule\'s values', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Rules');
            fireEvent.change(control('Predicate'), { target: { value: 'touches_any' } });
            fireEvent.change(screen.getByPlaceholderText(/value-1/), {
                target: { value: 'orders\n\n  customers  \n' },
            });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules()[0]).toMatchObject({ deny_if: 'touches_any', values: ['orders', 'customers'] });
        });

        it('sends an "allow only if" rule under the other key', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Rules');
            fireEvent.change(control('Mode'), { target: { value: 'allow_only_if' } });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules()[0]).toMatchObject({ deny_if: undefined, allow_only_if: 'writes' });
        });

        it('sends each rule, and only the rules that were not removed', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Rules');
            expect(screen.getByTitle('A policy needs at least one rule')).toBeDisabled();

            fireEvent.click(screen.getByRole('button', { name: 'Add rule' }));
            fireEvent.click(screen.getByRole('button', { name: 'Add rule' }));
            expect(screen.getByText('3 rules')).toBeInTheDocument();

            fireEvent.change(control('Predicate', 1), { target: { value: 'multi_statement' } });
            fireEvent.click(screen.getAllByTitle('Remove rule')[0]);
            expect(screen.getByText('2 rules')).toBeInTheDocument();

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules().map((r) => r.deny_if)).toEqual(['multi_statement', 'writes']);
        });
    });

    describe('editing', () => {
        it('loads the policy and saves the body and the groups separately', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);
            expect(screen.getByText('Edit Argument Policy — pg-readonly')).toBeInTheDocument();
            expect(screen.getByRole('checkbox', { name: 'Enabled' })).not.toBeChecked();

            tab('Access Control');
            fireEvent.click(await screen.findByRole('button', { name: 'pick group' }));

            submit('Save Changes');

            await waitFor(() => expect(gateway.setArgumentPolicyGroups).toHaveBeenCalledWith('ap-1', ['g-old', 'g-new']));
            const [id, body] = gateway.updateArgumentPolicy.mock.calls[0] as [string, Record<string, unknown>];
            expect(id).toBe('ap-1');
            expect(body).toMatchObject({
                name: 'pg-readonly',
                description: 'No writes',
                surface: 'mcp_input',
                target: 'query_db',
                argument_key: 'sql',
                analyzer_config: { dialect: 'postgresql' },
                enabled: false,
                severity: 'warn',
            });
            expect(body).not.toHaveProperty('group_ids');
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();
            expect(onClose).toHaveBeenCalledWith(true);
        });

        it('shows the severity the policy actually has', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);
            expect(control<HTMLSelectElement>('Severity').value).toBe('warn');
        });

        it('offers the severities the Control Plane answers for the surface, and no other', () => {
            render(
                <ArgumentPolicyForm
                    severitiesBySurface={{ ...SEVERITIES_BY_SURFACE, skill_input: ['deny', 'warn'] }}
                    policy={null}
                    onClose={onClose}
                />,
            );
            type('Surface', 'skill_input');
            expect(Array.from(control<HTMLSelectElement>('Severity').options).map((o) => o.value)).toEqual(['deny', 'warn']);
            type('Surface', 'mcp_input');
            expect(Array.from(control<HTMLSelectElement>('Severity').options).map((o) => o.value)).toEqual(['deny', 'approval_required', 'warn']);
        });

        it('offers deny, approval required and warn as a policy\'s severity, and says what inherits it', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Surface', 'mcp_input');
            const options = Array.from(control<HTMLSelectElement>('Severity').options).map((o) => o.value);
            expect(options).toEqual(['deny', 'approval_required', 'warn']);
            expect(screen.getByTestId('policy-severity-help')).toHaveTextContent(
                'What a rule does when it has no outcome of its own: it inherits this one. A rule that guards a compared field must still deny: when this is not Deny, give that rule the outcome Deny itself.',
            );
        });

        it('shows a policy that holds calls for approval as it is stored, and saves it so', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={{ ...EXISTING, severity: 'approval_required' }} onClose={onClose} />);
            expect(control<HTMLSelectElement>('Severity').value).toBe('approval_required');
            submit('Save Changes');
            await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalled());
            expect((gateway.updateArgumentPolicy.mock.calls[0] as [string, Record<string, unknown>])[1].severity).toBe('approval_required');
        });

        it('shows a stored approval on the model tool-call surface as it is stored, says it must be changed, and keeps Save off until it is', async () => {
            const stored = {
                ...EXISTING,
                surface: 'llm_output_tool_calls',
                severity: 'approval_required',
                rules: [{ deny_if: 'writes', message: 'Read only', severity: 'approval_required' }],
            } as typeof EXISTING;
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={stored} onClose={onClose} />);

            // As stored: not turned into a refusal on open.
            const severity = control<HTMLSelectElement>('Severity');
            expect(severity.value).toBe('approval_required');
            expect(severity.selectedOptions[0].textContent).toBe('approval required (stored: not available for model tool calls)');
            expect(screen.getByTestId('stored-approval-must-change')).toHaveTextContent(
                'This policy is stored with Approval required on the surface of model tool calls, which the Control Plane refuses: the gateway holds nothing for a person there. Change the severity of the policy and the outcome of rule 1 to save it.',
            );
            const save = screen.getByRole('button', { name: 'Save Changes' });
            expect(save).toBeDisabled();
            fireEvent.click(save);
            expect(gateway.updateArgumentPolicy).not.toHaveBeenCalled();

            // The policy changed, the rule not yet.
            type('Severity', 'deny');
            // Once changed, the stored value is no longer offered.
            expect(Array.from(control<HTMLSelectElement>('Severity').options).map((o) => o.value)).toEqual(['deny', 'warn']);
            expect(screen.getByTestId('stored-approval-must-change')).toHaveTextContent('Change the outcome of rule 1 to save it.');
            expect(save).toBeDisabled();

            tab('Rules');
            const rule = screen.getByTestId('rule-severity-0') as HTMLSelectElement;
            expect(rule.value).toBe('approval_required');
            fireEvent.change(rule, { target: { value: 'deny' } });
            expect(screen.queryByTestId('stored-approval-must-change')).toBeNull();
            expect(save).toBeEnabled();
            fireEvent.click(save);
            await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalled());
            const sent = (gateway.updateArgumentPolicy.mock.calls[0] as [string, { severity: string; rules: { severity?: string }[] }])[1];
            expect(sent.severity).toBe('deny');
            expect(sent.rules[0].severity).toBe('deny');
        });

        it('saves approval required as the severity chosen for a policy', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);
            type('Severity', 'approval_required');
            submit('Save Changes');
            await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalled());
            expect((gateway.updateArgumentPolicy.mock.calls[0] as [string, Record<string, unknown>])[1].severity).toBe('approval_required');
        });

        it('reports a failed group assignment even though the policy saved', async () => {
            gateway.setArgumentPolicyGroups.mockRejectedValue(new Error('group is in another tenant'));
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);

            submit('Save Changes');

            expect(await screen.findByText('group is in another tenant')).toBeInTheDocument();
            expect(screen.getByText('Failed to update policy')).toBeInTheDocument();
            expect(onClose).not.toHaveBeenCalled();
        });

        it('pre-selects the server exposing the stored tool, and offers its argument keys', async () => {
            gatewayClient.fetchServers.mockResolvedValue([
                {
                    id: 'srv-1',
                    name: 'Postgres',
                    capabilities: {
                        tools: [
                            { name: 'query_db', inputSchema: { properties: { sql: {}, timeout: {} } } },
                            { name: 'ping' },
                        ],
                    },
                },
            ]);
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);

            await waitFor(() => expect(control<HTMLSelectElement>('MCP Server').value).toBe('srv-1'));
            const keySelect = await waitFor(() => {
                const el = control<HTMLSelectElement>(/Argument key/);
                expect(el.tagName).toBe('SELECT');
                return el;
            });
            expect(keySelect.value).toBe('sql');
            expect(Array.from(keySelect.options).map((o) => o.value)).toEqual(['sql', 'timeout', '__manual__']);
        });
    });

    describe('validation that blocks a bad save', () => {
        /** The toast appeared, and nothing was sent. */
        const saveIsBlocked = async (message: string | RegExp) => {
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Validation', expect.stringMatching(message)));
            return gateway.createArgumentPolicy.mock.calls.length === 0 && onClose.mock.calls.length === 0;
        };

        it('requires a name, and goes back to where it is entered', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            tab('Rules');

            submit();

            expect(await saveIsBlocked(/Name is required/)).toBe(true);
            expect(screen.getByPlaceholderText('e.g. pg-readonly-analytics')).toBeInTheDocument();
        });

        it('requires an argument key', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('p', '   ');
            tab('Rules');

            submit();

            expect(await saveIsBlocked(/Argument key is required/)).toBe(true);
            expect(screen.getByPlaceholderText('e.g. sql, url, command')).toBeInTheDocument();
        });

        it('requires values for a predicate that takes them', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Rules');
            fireEvent.change(control('Predicate'), { target: { value: 'touches_any' } });
            fireEvent.change(screen.getByPlaceholderText(/value-1/), { target: { value: ' \n ' } });

            submit();

            expect(await saveIsBlocked(/Rule #1 \(touches_any\) requires values/)).toBe(true);
        });

        it('requires a field path on a comparison', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            // Seeding prepends the presence rule, so the comparison is rule 2.
            submit();

            expect(await saveIsBlocked(/Rule #2 \(field_gt\) needs a field path/)).toBe(true);
        });

        it('requires a number for a threshold', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });

            submit();

            expect(await saveIsBlocked(/Rule #2 \(field_gt\) needs a numeric value/)).toBe(true);
        });

        it('requires an operand for an equality comparison', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_eq' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'currency' } });

            submit();

            expect(await saveIsBlocked(/Rule #2 \(field_eq\) needs a value/)).toBe(true);
        });

        it('refuses a threshold a caller can bypass by omitting the field', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '1000' } });

            submit();

            expect(await saveIsBlocked(/bypass this policy by leaving out amount\. Add a rule that denies the call when amount is not present/)).toBe(true);
        });

        it('refuses a threshold whose guard does not deny: a warning lets the call through', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '1000' } });
            // The seeded guard, made a warning.
            fireEvent.change(screen.getByTestId('rule-severity-0'), { target: { value: 'warn' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"schema":{"required":["amount"]}}' } });

            submit();

            expect(await saveIsBlocked(/The rule that guards amount does not deny: set its severity to Deny/)).toBe(true);
        });

        it('refuses a threshold whose guard inherits a policy severity that does not deny', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('transfer-band', '*');
            type('Surface', 'skill_input');
            type('Analyzer', 'json');
            type('Severity', 'approval_required');
            tab('Rules');
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '1000' } });
            // The seeded guard has no outcome of its own: it inherits the policy's.
            fireEvent.change(screen.getByTestId('rule-severity-0'), { target: { value: '' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"schema":{"required":["amount"]}}' } });

            submit();

            expect(await saveIsBlocked(/The rule that guards amount does not deny: set its severity to Deny/)).toBe(true);
        });

        it('states the rule about guards, and the limits of the analysis, beside the rules', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            const help = screen.getByTestId('argument-policy-rule-help');
            expect(help).toHaveTextContent('every compared field needs a guard that refuses a call without it');
            expect(help).toHaveTextContent('The guard has to deny');
            expect(help).toHaveTextContent('when a policy is created and each time it is changed');
            expect(help).toHaveTextContent(
                'Unless this policy sets its own limits (Advanced), the gateway reads at most 512 values, 8 levels of nesting and 64 items of a list in the arguments of one call. A call with more is refused when this policy applies to it, whatever the rules say.',
            );
            expect(help).toHaveTextContent(
                'A field that is present with a value the rule cannot compare (null, a list, an object, text where a number is expected) is refused, not let through.',
            );
        });

        it('refuses comparison rules unless the argument key is the whole object', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('p', 'amount');
            type('Analyzer', 'json');
            tab('Rules');
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '5' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), {
                target: { value: '{"schema":{"required":["amount"]}}' },
            });

            submit();

            expect(await saveIsBlocked(/argument key must be '\*'/)).toBe(true);
        });

        it('refuses analyzer config that is not valid JSON, and shows where', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{ "dialect": "postgresql" }'), {
                target: { value: '{oops' },
            });
            tab('General');

            submit();

            expect(await saveIsBlocked(/not valid JSON/)).toBe(true);
            expect(screen.getByText('Analyzer config (JSON)')).toBeInTheDocument();
        });

        it('refuses analyzer config that is JSON but not an object', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{ "dialect": "postgresql" }'), {
                target: { value: '["postgresql"]' },
            });

            submit();

            expect(await saveIsBlocked(/must be a JSON object/)).toBe(true);
        });
    });

    describe('the limits of the analysis of a JSON policy', () => {
        const limit = (name: string) => screen.getByTestId<HTMLInputElement>(`analysis-limit-${name}`);
        const openLimits = () => {
            fillBasics('transfer-band', '*');
            type('Analyzer', 'json');
            tab('Advanced');
        };

        it('offers the three limits with the defaults as placeholders and their ranges', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            expect(limit('max_leaves').placeholder).toBe('512');
            expect(limit('max_depth').placeholder).toBe('8');
            expect(limit('max_array_elements').placeholder).toBe('64');
            expect(limit('max_leaves').value).toBe('');
            expect(screen.getByTestId('analysis-limits')).toHaveTextContent('Most values in the arguments of one call (1 to 4096)');
            expect(screen.getByTestId('analysis-limits')).toHaveTextContent('Most levels of nesting (1 to 32)');
            expect(screen.getByTestId('analysis-limits')).toHaveTextContent('Most items of a list (1 to 1024)');
            expect(screen.getByTestId('analysis-limits')).toHaveTextContent(
                'A call beyond a limit is refused when this policy applies to it, whatever the rules say. Leave a limit empty for its default. A call that several policies apply to must fit the limits of each.',
            );
        });

        it('does not offer them for another analyzer', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            tab('Advanced');
            expect(screen.queryByTestId('analysis-limits')).toBeNull();
        });

        it('sends the limits that were set in the analyzer config, as numbers, and leaves the others out', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            fireEvent.change(limit('max_leaves'), { target: { value: '2000' } });
            fireEvent.change(limit('max_array_elements'), { target: { value: '200' } });
            fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"strict_numbers":true}' } });
            submit();
            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalled());
            expect(created().analyzer_config).toEqual({ strict_numbers: true, max_leaves: 2000, max_array_elements: 200 });
        });

        it('sends no analyzer config when nothing is set', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            submit();
            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalled());
            expect(created().analyzer_config).toBeNull();
        });

        it('shows the stored limits in their fields and the rest of the config as JSON; emptying a limit removes it', async () => {
            const stored: ArgumentPolicy = {
                ...EXISTING,
                argument_key: '*',
                analyzer: 'json',
                analyzer_config: { strict_numbers: true, max_leaves: 1024, max_depth: 16 },
                rules: [{ deny_if: 'schema_invalid', message: 'Bad shape' }],
            };
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={stored} onClose={onClose} />);
            tab('Advanced');
            expect(limit('max_leaves').value).toBe('1024');
            expect(limit('max_depth').value).toBe('16');
            expect(limit('max_array_elements').value).toBe('');
            expect(JSON.parse(control<HTMLTextAreaElement>('Analyzer config (JSON)').value)).toEqual({ strict_numbers: true });
            fireEvent.change(limit('max_depth'), { target: { value: '' } });
            submit('Save Changes');
            await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalled());
            expect((gateway.updateArgumentPolicy.mock.calls[0] as [string, Record<string, unknown>])[1].analyzer_config).toEqual({
                strict_numbers: true,
                max_leaves: 1024,
            });
        });

        it.each([
            ['max_leaves', '4097', /Most values must be a whole number from 1 to 4096/],
            ['max_depth', '0', /Most levels of nesting must be a whole number from 1 to 32/],
            ['max_array_elements', '1.5', /Most items of a list must be a whole number from 1 to 1024/],
        ])('refuses %s = %s and does not save', async (name, value, message) => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            fireEvent.change(limit(name), { target: { value } });
            submit();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Validation', expect.stringMatching(message)));
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();
        });

        it('refuses a limit written in the JSON as well as in its field', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            fireEvent.change(limit('max_leaves'), { target: { value: '100' } });
            fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"max_leaves":200}' } });
            submit();
            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Validation', 'max_leaves is set twice: in its field and in the analyzer config. Set it in one place.'),
            );
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();
        });

        it('shows the Control Plane\'s refusal of a limit as it said it', async () => {
            const { thrownByClient } = await import('../../../test/serverAnswers');
            const refusal = 'analyzer_config.max_depth must be an integer from 1 to 32';
            gateway.createArgumentPolicy.mockRejectedValue(await thrownByClient(422, { detail: refusal }));
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            openLimits();
            fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: '{"max_depth":"deep"}' } });
            submit();
            expect(await screen.findByText(refusal)).toBeInTheDocument();
        });
    });

    describe('comparison bands', () => {
        /** A two-threshold band, looser first, guarded through the schema. */
        function fillBand(first: string, second: string) {
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: first } });
            fireEvent.click(screen.getByRole('button', { name: 'Add rule' }));
            fireEvent.change(control('Predicate', 2), { target: { value: 'field_gt' } });
            fireEvent.change(screen.getByTestId('rule-field-2'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-2'), { target: { value: second } });
        }

        it('seeds a presence rule when the first comparison is chosen, once', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();

            fireEvent.change(control('Predicate'), { target: { value: 'field_gt' } });
            expect(screen.getByText('2 rules')).toBeInTheDocument();
            expect(control<HTMLSelectElement>('Predicate', 0).value).toBe('schema_invalid');
            expect(control<HTMLSelectElement>('Predicate', 1).value).toBe('field_gt');

            fireEvent.change(control('Predicate', 1), { target: { value: 'field_lt' } });
            expect(screen.getByText('2 rules')).toBeInTheDocument();
        });

        it('sends the threshold as a number, with each rule\'s own outcome', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Surface', 'skill_input');
            fillBand('10000', '1000');
            fireEvent.change(screen.getByTestId('rule-severity-1'), { target: { value: 'deny' } });
            fireEvent.change(screen.getByTestId('rule-severity-2'), { target: { value: 'approval_required' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), {
                target: { value: '{"schema":{"required":["amount"]}}' },
            });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(created().argument_key).toBe('*');
            expect(created().analyzer_config).toEqual({ schema: { required: ['amount'] } });
            expect(sentRules()).toEqual([
                expect.objectContaining({ deny_if: 'schema_invalid', severity: 'deny' }),
                expect.objectContaining({ deny_if: 'field_gt', field: 'amount', value: 10000, severity: 'deny' }),
                expect.objectContaining({ deny_if: 'field_gt', field: 'amount', value: 1000, severity: 'approval_required' }),
            ]);
        });

        describe('the operand of an equality or membership rule', () => {
            /** A guarded equality rule on `field`, on the Rules tab. */
            function fillEquality(predicate: string, field: string) {
                startJsonPolicy();
                fireEvent.change(control('Predicate'), { target: { value: predicate } });
                fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: field } });
            }
            const requireField = (field: string) => {
                tab('Advanced');
                fireEvent.change(screen.getByPlaceholderText('{}'), { target: { value: `{"schema":{"required":["${field}"]}}` } });
            };

            it('is sent as a boolean when the field is true or false', async () => {
                render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
                fillEquality('field_eq', 'dry_run');
                fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'boolean' } });
                fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: 'false' } });
                requireField('dry_run');
                submit();
                await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
                expect(sentRules()[1]).toEqual(expect.objectContaining({ deny_if: 'field_eq', field: 'dry_run', value: false }));
            });

            it('is sent as a number when the field is a number, and as text by default', async () => {
                render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
                fillEquality('field_ne', 'retries');
                fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '3' } });
                expect(screen.getByTestId<HTMLSelectElement>('rule-operand-kind-1').value).toBe('text');
                fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'number' } });
                requireField('retries');
                submit();
                await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
                expect(sentRules()[1]).toEqual(expect.objectContaining({ deny_if: 'field_ne', field: 'retries', value: 3 }));
            });

            it('a list of options is sent as booleans, and an option that is neither true nor false stops the save', async () => {
                render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
                fillEquality('field_in', 'confirmed');
                fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'boolean' } });
                fireEvent.change(screen.getByTestId('rule-values-1'), { target: { value: 'true\nmaybe' } });
                requireField('confirmed');
                submit();
                await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Validation', 'Rule 2: "maybe" is not true or false'));
                expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();

                tab('Rules');
                fireEvent.change(screen.getByTestId('rule-values-1'), { target: { value: 'true' } });
                submit();
                await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
                expect(sentRules()[1]).toEqual(expect.objectContaining({ deny_if: 'field_in', field: 'confirmed', values: [true] }));
            });

            it('a stored boolean operand is shown as one and saved as one', async () => {
                const stored: ArgumentPolicy = {
                    ...EXISTING,
                    argument_key: '*',
                    analyzer: 'json',
                    severity: 'deny',
                    analyzer_config: { schema: { required: ['dry_run', 'confirmed'] } },
                    rules: [
                        { deny_if: 'schema_invalid' },
                        { deny_if: 'field_eq', field: 'dry_run', value: false },
                        { allow_only_if: 'field_in', field: 'confirmed', values: [true] },
                    ],
                };
                render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={stored} onClose={onClose} />);
                tab('Rules');
                expect(screen.getByTestId<HTMLSelectElement>('rule-operand-kind-1').value).toBe('boolean');
                expect(screen.getByTestId<HTMLSelectElement>('rule-value-1').value).toBe('false');
                expect(screen.getByTestId<HTMLSelectElement>('rule-operand-kind-2').value).toBe('boolean');
                submit('Save Changes');
                await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalledTimes(1));
                const rules = (gateway.updateArgumentPolicy.mock.calls[0][1] as { rules: Record<string, unknown>[] }).rules;
                expect(rules[1]).toEqual(expect.objectContaining({ deny_if: 'field_eq', value: false }));
                expect(rules[2]).toEqual(expect.objectContaining({ allow_only_if: 'field_in', values: [true] }));
            });

            describe('a stored membership list of more than one type', () => {
                const MIXED = ['EUR', 100, true, '100'];
                const stored: ArgumentPolicy = {
                    ...EXISTING,
                    argument_key: '*',
                    analyzer: 'json',
                    severity: 'deny',
                    analyzer_config: { schema: { required: ['currency'] } },
                    rules: [
                        { deny_if: 'schema_invalid' },
                        { allow_only_if: 'field_in', field: 'currency', values: MIXED },
                    ],
                };
                const open = () => {
                    render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={stored} onClose={onClose} />);
                    tab('Rules');
                };
                const sentValues = async () => {
                    await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalledTimes(1));
                    return (gateway.updateArgumentPolicy.mock.calls[0][1] as { rules: Record<string, unknown>[] }).rules[1].values;
                };

                it('opens as it is stored, not as text, and says how the lines are written', () => {
                    open();
                    expect(screen.getByTestId<HTMLSelectElement>('rule-operand-kind-1').value).toBe('mixed');
                    expect(screen.getByTestId<HTMLTextAreaElement>('rule-values-1').value).toBe('"EUR"\n100\ntrue\n"100"');
                    expect(screen.getByTestId('rule-mixed-note-1')).toHaveTextContent(
                        'This list holds values of more than one type. Each line is one value as it is stored: text in double quotes, a number, or true or false. To make every value one type, choose it under Compare as.',
                    );
                });

                it('saved untouched, keeps the type of each value', async () => {
                    open();
                    submit('Save Changes');
                    expect(await sentValues()).toEqual(MIXED);
                });

                it('keeps the types of the other values when one line is edited', async () => {
                    open();
                    fireEvent.change(screen.getByTestId('rule-values-1'), { target: { value: '"EUR"\n250\ntrue\n"100"\nfalse' } });
                    submit('Save Changes');
                    expect(await sentValues()).toEqual(['EUR', 250, true, '100', false]);
                });

                it('refuses to save a line that would have to be guessed', async () => {
                    open();
                    fireEvent.change(screen.getByTestId('rule-values-1'), { target: { value: '"EUR"\nUSD' } });
                    submit('Save Changes');
                    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
                        'Validation',
                        'Rule 2: "USD" is not text in double quotes, a number, or true or false',
                    ));
                    expect(gateway.updateArgumentPolicy).not.toHaveBeenCalled();
                });

                it('becomes text only when the administrator chooses Text: the lines lose their quotes and every value is sent as text', async () => {
                    open();
                    fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'text' } });
                    expect(screen.getByTestId<HTMLTextAreaElement>('rule-values-1').value).toBe('EUR\n100\ntrue\n100');
                    expect(screen.queryByTestId('rule-mixed-note-1')).toBeNull();
                    submit('Save Changes');
                    expect(await sentValues()).toEqual(['EUR', '100', 'true', '100']);
                });

                it('chosen as numbers, is refused for the value that is not one', async () => {
                    open();
                    fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'number' } });
                    submit('Save Changes');
                    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Validation', 'Rule 2: "EUR" is not a number'));
                    expect(gateway.updateArgumentPolicy).not.toHaveBeenCalled();
                });

                it('can be put back as stored after another type was chosen', async () => {
                    open();
                    fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'text' } });
                    fireEvent.change(screen.getByTestId('rule-operand-kind-1'), { target: { value: 'mixed' } });
                    expect(screen.getByTestId<HTMLTextAreaElement>('rule-values-1').value).toBe('"EUR"\n100\ntrue\n"100"');
                    submit('Save Changes');
                    expect(await sentValues()).toEqual(MIXED);
                });

                it('is not offered for a list of one type', () => {
                    render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={{ ...stored, rules: [stored.rules[0], { allow_only_if: 'field_in', field: 'currency', values: ['EUR', 'USD'] }] }} onClose={onClose} />);
                    tab('Rules');
                    const kinds = [...screen.getByTestId<HTMLSelectElement>('rule-operand-kind-1').options].map((option) => option.value);
                    expect(kinds).toEqual(['text', 'number', 'boolean']);
                });
            });

            it('says how far into the arguments a field path is read', () => {
                render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
                fillEquality('field_eq', 'items.70.sku');
                expect(screen.getByTestId('rule-comparison-1')).toHaveTextContent(
                    "The gateway reads the first 64 items of a list and 8 levels of nesting, or this policy's own limits (Advanced). A path beyond them is never reached, and the call is refused.",
                );
            });
        });

        it('reads the band back in plain language', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Surface', 'skill_input');
            fillBand('10000', '1000');
            fireEvent.change(screen.getByTestId('rule-severity-2'), { target: { value: 'approval_required' } });

            const summary = screen.getByTestId('band-summary');
            expect(summary).toHaveTextContent('amount above 10000 → refuse');
            expect(summary).toHaveTextContent('amount above 1000 → hold for a human');
        });

        it('warns about a rule a looser one makes unreachable, as typed into the form', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBand('1000', '10000');

            expect(screen.getByTestId('rule-shadowed-2')).toHaveTextContent('Rule 3 can never run');
        });

        it('does not warn when the tightest threshold is first', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBand('10000', '1000');

            expect(screen.queryByTestId('rule-shadowed-2')).not.toBeInTheDocument();
        });

        it('sends an explicit presence rule as satisfying the guard', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_gte' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'amount' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: '0.5' } });
            // Turn the seeded rule into `allow only if field_present: amount`.
            fireEvent.change(control('Predicate', 0), { target: { value: 'field_present' } });
            fireEvent.change(control('Mode', 0), { target: { value: 'allow_only_if' } });
            fireEvent.change(screen.getByPlaceholderText(/value-1/), { target: { value: 'amount' } });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules()[0]).toMatchObject({ allow_only_if: 'field_present', values: ['amount'] });
            expect(sentRules()[1]).toMatchObject({ deny_if: 'field_gte', field: 'amount', value: 0.5 });
        });

        it('sends an equality operand as text, not a number', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_eq' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'currency' } });
            fireEvent.change(screen.getByTestId('rule-value-1'), { target: { value: 'EUR' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), {
                target: { value: '{"schema":{"required":["currency"]}}' },
            });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules()[1]).toMatchObject({ deny_if: 'field_eq', field: 'currency', value: 'EUR' });
        });

        it('lets a list comparison be expressed: field_in sends `values`', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_in' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'currency' } });
            fireEvent.change(screen.getByPlaceholderText(/value-1/), { target: { value: 'EUR\nUSD' } });
            tab('Advanced');
            fireEvent.change(screen.getByPlaceholderText('{}'), {
                target: { value: '{"schema":{"required":["currency"]}}' },
            });

            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(sentRules()[1]).toMatchObject({ deny_if: 'field_in', field: 'currency', values: ['EUR', 'USD'] });
        });

        it('requires the list for a list comparison', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            startJsonPolicy();
            fireEvent.change(control('Predicate'), { target: { value: 'field_not_in' } });
            fireEvent.change(screen.getByTestId('rule-field-1'), { target: { value: 'currency' } });

            submit();

            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Validation', expect.stringMatching(/Rule #2 \(field_not_in\) requires values/)),
            );
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();
        });
    });

    describe('analyzer and discovery', () => {
        it('moves rules to a predicate the new analyzer accepts', async () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('p', 'url');

            type('Analyzer', 'url');
            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(created().analyzer).toBe('url');
            expect(sentRules()[0].deny_if).toBe('host_in_private_range');
        });

        it('offers only predicates valid for the analyzer', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Analyzer', 'shell');
            tab('Rules');

            const options = Array.from(control<HTMLSelectElement>('Predicate').options).map((o) => o.value);
            expect(options).toEqual(['command_in', 'command_not_in', 'flag_present', 'flag_not_present']);
        });

        it('lists the tenant\'s skills as targets on the skill surface', async () => {
            gatewayClient.listAgentSkills.mockResolvedValue({
                skills: { a: { name: 'summarise' }, b: { name: 'translate' } },
            });
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();

            type('Surface', 'skill_input');

            const target = await waitFor(() => {
                const el = control<HTMLSelectElement>('Tool / Skill name');
                expect(el.tagName).toBe('SELECT');
                return el;
            });
            fireEvent.change(target, { target: { value: 'translate' } });
            submit();

            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
            expect(created().target).toBe('translate');
        });

        it('auto-picks the only argument a chosen tool has, and clears a stale key', async () => {
            gatewayClient.fetchServers.mockResolvedValue([
                {
                    id: 'srv-1',
                    name: 'Files',
                    capabilities: {
                        tools: [
                            { name: 'read', inputSchema: { properties: { path: {} } } },
                            { name: 'copy', inputSchema: { properties: { from: {}, to: {} } } },
                        ],
                    },
                },
            ]);
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics('p', 'leftover');
            type('Surface', 'mcp_input');
            await screen.findByRole('option', { name: 'Files' });
            type('MCP Server', 'srv-1');

            const target = () => control<HTMLSelectElement>('Tool / Skill name');
            await waitFor(() => expect(target().tagName).toBe('SELECT'));

            fireEvent.change(target(), { target: { value: 'read' } });
            await waitFor(() => expect(control<HTMLSelectElement>(/Argument key/).value).toBe('path'));

            // Two arguments: the old key is cleared, and the operator must choose.
            fireEvent.change(target(), { target: { value: 'copy' } });
            await waitFor(() => expect(screen.getByPlaceholderText('e.g. sql, url, command')).toHaveValue(''));

            submit();
            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Validation', expect.stringMatching(/Argument key is required/)),
            );
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();

            fireEvent.click(screen.getByRole('button', { name: 'Pick from discovered list' }));
            expect(Array.from(control<HTMLSelectElement>(/Argument key/).options).map((o) => o.value)).toEqual([
                'from',
                'to',
                '__manual__',
            ]);
        });

        it('falls back to typing a tool name when the server has none', async () => {
            gatewayClient.fetchServers.mockResolvedValue([{ id: 'srv-1', name: 'Bare', capabilities: {} }]);
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Surface', 'mcp_input');
            await screen.findByRole('option', { name: 'Bare' });
            type('MCP Server', 'srv-1');

            expect(await screen.findByText(/No tools discovered/)).toBeInTheDocument();
            expect(screen.getByPlaceholderText('postgres_query (or *)')).toBeInTheDocument();
        });

        it('switches between the discovered list and manual entry', async () => {
            gatewayClient.listAgentSkills.mockResolvedValue({ skills: { a: { name: 'summarise' } } });
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            type('Surface', 'skill_input');
            await waitFor(() => expect(control('Tool / Skill name').tagName).toBe('SELECT'));

            fireEvent.change(control('Tool / Skill name'), { target: { value: '__manual__' } });
            expect(await screen.findByRole('button', { name: 'Pick from discovered list' })).toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: 'Pick from discovered list' }));
            expect(control('Tool / Skill name').tagName).toBe('SELECT');
        });

        it('keeps working when discovery and the group tree fail', async () => {
            gatewayClient.fetchServers.mockRejectedValue(new Error('down'));
            gatewayClient.listAgentSkills.mockRejectedValue(new Error('down'));
            gatewayClient.fetchGroupHierarchyTree.mockRejectedValue(new Error('down'));
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();

            tab('Access Control');
            expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the resource groups: down');
            expect(screen.queryByText(/No Resource Groups defined yet/)).toBeNull();

            submit();
            await waitFor(() => expect(gateway.createArgumentPolicy).toHaveBeenCalledTimes(1));
        });
    });

    describe('a policy on a model\'s tool calls, where the gateway holds nothing for a person', () => {
        const NOTE =
            'Approvals are not available for model tool calls: the gateway holds nothing for a person on that surface. To hold a call for a person, put the policy on the tool\'s own surface (MCP inbound or Skill input).';
        const values = (select: HTMLSelectElement) => Array.from(select.options).map((o) => o.value);

        it('offers deny and warn as the policy\'s severity, and says why no approval', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            expect(control<HTMLSelectElement>('Surface').value).toBe('llm_output_tool_calls');
            expect(values(control<HTMLSelectElement>('Severity'))).toEqual(['deny', 'warn']);
            expect(screen.getByTestId('no-approval-on-surface')).toHaveTextContent(NOTE);
        });

        it('offers deny and warn as a rule\'s outcome', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            tab('Rules');
            expect(values(screen.getByTestId<HTMLSelectElement>('rule-severity-0'))).toEqual(['', 'deny', 'warn']);
        });

        it('offers all three on the surfaces where a call can be held, with no note', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            for (const surface of ['mcp_input', 'skill_input']) {
                type('Surface', surface);
                expect(values(control<HTMLSelectElement>('Severity'))).toEqual(['deny', 'approval_required', 'warn']);
                expect(screen.queryByTestId('no-approval-on-surface')).toBeNull();
            }
            tab('Rules');
            expect(values(screen.getByTestId<HTMLSelectElement>('rule-severity-0'))).toEqual(['', 'deny', 'approval_required', 'warn']);
        });

        it('moving a policy to the surface turns every approval into a refusal, of the policy and of each rule', async () => {
            render(
                <ArgumentPolicyForm
                    severitiesBySurface={SEVERITIES_BY_SURFACE}
                    policy={{ ...EXISTING, severity: 'approval_required', rules: [{ deny_if: 'writes', severity: 'approval_required' }, { deny_if: 'ddl', severity: 'warn' }] }}
                    onClose={onClose}
                />,
            );
            type('Surface', 'llm_output_tool_calls');
            expect(control<HTMLSelectElement>('Severity').value).toBe('deny');
            submit('Save Changes');
            await waitFor(() => expect(gateway.updateArgumentPolicy).toHaveBeenCalled());
            const sent = (gateway.updateArgumentPolicy.mock.calls[0] as [string, { surface: string; severity: string; rules: Array<{ severity?: string }> }])[1];
            expect(sent.surface).toBe('llm_output_tool_calls');
            expect(sent.severity).toBe('deny');
            expect(sent.rules.map((rule) => rule.severity)).toEqual(['deny', 'warn']);
        });

        it('shows the Control Plane\'s refusal of an approval there as it words it', async () => {
            const { thrownByClient } = await import('../../../test/serverAnswers');
            const said =
                "rules[0].severity: Approvals are not available for model tool calls: the gateway holds nothing for a person on that surface. Use 'deny' or 'warn', or put the policy on the tool's own surface (mcp_input, skill_input), where a call can be held.";
            gateway.createArgumentPolicy.mockRejectedValue(await thrownByClient(400, { detail: said }));
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();
            submit();
            expect(await screen.findByText(said)).toBeInTheDocument();
            expect(toast.error).toHaveBeenCalledWith('Save Failed', said);
            expect(onClose).not.toHaveBeenCalled();
        });
    });

    describe('errors and leaving', () => {
        it('says in plain words that the Control Plane refused a change that leaves a compared field unguarded', async () => {
            const { thrownByClient } = await import('../../../test/serverAnswers');
            gateway.updateArgumentPolicy.mockRejectedValue(
                await thrownByClient(400, {
                    detail:
                        "A comparison rule can be bypassed by omitting the field it compares. Add the path to analyzer_config.schema.required (with a schema_invalid rule) or an explicit field_present rule for: amount. The rule guarding amount does not deny: its effective severity (its own, else the policy's) must be 'deny', because the gateway lets a 'warn' through and holds an 'approval_required' instead of refusing it",
                }),
            );
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={EXISTING} onClose={onClose} />);
            submit('Save Changes');

            const said =
                'Nothing was saved. A caller can bypass this policy by leaving out amount. The rule that guards amount does not deny: set its severity to Deny. A warning lets the call through, and an approval holds it instead of refusing it.';
            expect(await screen.findByText(said)).toBeInTheDocument();
            expect(toast.error).toHaveBeenCalledWith('Save Failed', said);
            expect(onClose).not.toHaveBeenCalled();
        });

        it('shows a server refusal, keeps the form open, and lets it be dismissed', async () => {
            gateway.createArgumentPolicy.mockRejectedValue(new Error('name already exists'));
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fillBasics();

            submit();

            expect(await screen.findByText('name already exists')).toBeInTheDocument();
            expect(screen.getByText('Failed to create policy')).toBeInTheDocument();
            expect(toast.error).toHaveBeenCalledWith('Save Failed', 'name already exists');
            expect(onClose).not.toHaveBeenCalled();
            expect(screen.getByRole('button', { name: 'Create Policy' })).toBeEnabled();

            const box = screen.getByText('Failed to create policy').closest('.bg-red-50') as HTMLElement;
            fireEvent.click(box.querySelector('button') as HTMLButtonElement);
            expect(screen.queryByText('name already exists')).not.toBeInTheDocument();
        });

        it('closes without saving on Cancel', () => {
            render(<ArgumentPolicyForm severitiesBySurface={SEVERITIES_BY_SURFACE} policy={null} onClose={onClose} />);
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

            expect(onClose).toHaveBeenCalledWith(false);
            expect(gateway.createArgumentPolicy).not.toHaveBeenCalled();
        });
    });
});
