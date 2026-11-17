/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Semantic policies: the list tab and the create/edit form.
 *
 * `createSemanticPolicy` and `updateSemanticPolicy` exist only on
 * `gatewayClient`; `useGateway()` has neither, so a form that destructured them
 * from the hook would die with a TypeError on every save. Hence the mock here is
 * the hook's real shape. The Enabled checkbox must reach the request.
 *
 * Pinned: the exact create/update bodies, shadow/enforce toggling, delete
 * confirmation, filters, and messageless failures naming what failed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import type { SemanticPolicy } from '../../../lib/semanticPolicyTypes';

const gatewayClient = {
    listSemanticPolicies: vi.fn(),
    createSemanticPolicy: vi.fn(),
    updateSemanticPolicy: vi.fn(),
    deleteSemanticPolicy: vi.fn(),
    fetchGroupHierarchyTree: vi.fn(),
    listLLMs: vi.fn(),
};
const gateway = { gatewayClient, fetchGroupHierarchyTree: vi.fn() };
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/HierarchicalGroupSelector', () => ({
    default: (p: { selectedGroupIds: string[]; onSelectionChange: (ids: string[]) => void }) => (
        <button type="button" onClick={() => p.onSelectionChange([...p.selectedGroupIds, 'rg-9'])}>pick-group</button>
    ),
}));

import SemanticPoliciesTab from '../SemanticPoliciesTab';
import SemanticPolicyForm from '../SemanticPolicyForm';

const POLICY = {
    id: 'p1',
    name: 'no-pii',
    description: 'Stops PII',
    constraint_text: 'Must not exfiltrate',
    action: 'block',
    mode: 'shadow',
    surfaces: ['chat_input'],
    judge_model_id: 'm1',
    enabled: true,
    resource_group_ids: ['rg-1', 'rg-2', 'rg-3'],
} as unknown as SemanticPolicy;
const WIDE = {
    id: 'p2',
    name: 'tenant-wide-warn',
    constraint_text: 'Be nice',
    action: 'warn',
    mode: 'enforce',
    surfaces: [],
    judge_model_id: null,
    enabled: false,
    resource_group_ids: [],
} as unknown as SemanticPolicy;

beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    for (const fn of Object.values(gatewayClient)) fn.mockReset();
    gateway.fetchGroupHierarchyTree.mockReset();
    gatewayClient.listSemanticPolicies.mockResolvedValue({ policies: [POLICY, WIDE] });
    gatewayClient.createSemanticPolicy.mockResolvedValue({ id: 'new' });
    gatewayClient.updateSemanticPolicy.mockResolvedValue({ id: 'p1' });
    gatewayClient.deleteSemanticPolicy.mockResolvedValue(undefined);
    gatewayClient.fetchGroupHierarchyTree.mockResolvedValue({ tree: [{ id: 'rg-1', name: 'one', children: [] }] });
    gatewayClient.listLLMs.mockResolvedValue({
        models: {
            a: { id: 'm1', name: 'Judge One', provider: 'openai' },
            b: { id: 'm2', model_name: 'off-model', enabled: false },
        },
    });
    gateway.fetchGroupHierarchyTree.mockResolvedValue({
        tree: [{ id: 'rg-1', name: 'Alpha', children: [{ id: 'rg-2', name: 'Beta' }] }],
    });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('SemanticPolicyForm', () => {
    const onClose = vi.fn();
    const field = (ph: string | RegExp) => screen.getByPlaceholderText(ph);

    function fillRequired() {
        fireEvent.change(field('e.g. no-pii-egress'), { target: { value: '  my-rule  ' } });
        fireEvent.change(field(/The request must NOT/), { target: { value: '  no leaks  ' } });
    }

    it('creates with the exact body, shipping shadow by default and null for what is unset', async () => {
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        expect(screen.getByText('New Semantic Policy')).toBeInTheDocument();
        fillRequired();
        fireEvent.click(screen.getByRole('button', { name: 'Create Policy' }));

        await waitFor(() =>
            expect(gatewayClient.createSemanticPolicy).toHaveBeenCalledWith({
                name: 'my-rule',
                description: null,
                constraint_text: 'no leaks',
                action: 'block',
                mode: 'shadow',
                surfaces: null,
                judge_model_id: null,
                resource_group_ids: [],
            }),
        );
        expect(toast.success).toHaveBeenCalledWith('Policy Created', '"my-rule" has been created.');
        expect(onClose).toHaveBeenCalledWith(true);
    });

    it('carries choices from every tab into the create body', async () => {
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        fillRequired();
        fireEvent.change(field(/Stops requests that/), { target: { value: 'desc' } });
        const selects = screen.getAllByRole('combobox');
        fireEvent.change(selects[0], { target: { value: 'enforce' } });
        fireEvent.change(selects[1], { target: { value: 'warn' } });
        // Disabled models are not offered as judges.
        await screen.findByRole('option', { name: 'Judge One (openai)' });
        expect(screen.queryByRole('option', { name: /off-model/ })).toBeNull();
        fireEvent.change(selects[2], { target: { value: 'm1' } });

        fireEvent.click(screen.getByRole('button', { name: 'Surfaces' }));
        expect(screen.getByText('No surfaces selected — the policy applies to all surfaces.')).toBeInTheDocument();
        fireEvent.click(within(screen.getByText('MCP input').closest('label') as HTMLElement).getByRole('checkbox'));
        fireEvent.click(within(screen.getByText('Chat input').closest('label') as HTMLElement).getByRole('checkbox'));
        fireEvent.click(within(screen.getByText('Chat input').closest('label') as HTMLElement).getByRole('checkbox'));
        expect(screen.getByText('1 surface selected.')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Access Control' }));
        expect(screen.getByText('No groups selected — applies tenant-wide')).toBeInTheDocument();
        fireEvent.click(await screen.findByText('pick-group'));
        expect(screen.getByText('1 group selected')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Create Policy' }));
        await waitFor(() =>
            expect(gatewayClient.createSemanticPolicy).toHaveBeenCalledWith({
                name: 'my-rule',
                description: 'desc',
                constraint_text: 'no leaks',
                action: 'warn',
                mode: 'enforce',
                surfaces: ['mcp_input'],
                judge_model_id: 'm1',
                resource_group_ids: ['rg-9'],
            }),
        );
    });

    it('does not offer an Enabled switch on create, which the API would ignore', () => {
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        expect(screen.queryByText('Enabled')).toBeNull();
    });

    it('refuses to submit without a name or a constraint, returning to the General tab', () => {
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        fireEvent.click(screen.getByRole('button', { name: 'Surfaces' }));
        fireEvent.click(screen.getByRole('button', { name: 'Create Policy' }));
        expect(toast.error).toHaveBeenCalledWith('Validation', 'Name is required');
        expect(field('e.g. no-pii-egress')).toBeInTheDocument();

        fireEvent.change(field('e.g. no-pii-egress'), { target: { value: 'x' } });
        fireEvent.click(screen.getByRole('button', { name: 'Create Policy' }));
        expect(toast.error).toHaveBeenCalledWith('Validation', 'A natural-language constraint is required');
        expect(gatewayClient.createSemanticPolicy).not.toHaveBeenCalled();
    });

    it('updates the existing policy, and the Enabled switch reaches the request', async () => {
        render(<SemanticPolicyForm policy={POLICY} onClose={onClose} />);
        expect(screen.getByText('Edit Semantic Policy — no-pii')).toBeInTheDocument();
        fireEvent.click(within(screen.getByText('Enabled').closest('label') as HTMLElement).getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

        await waitFor(() =>
            expect(gatewayClient.updateSemanticPolicy).toHaveBeenCalledWith('p1', {
                name: 'no-pii',
                description: 'Stops PII',
                constraint_text: 'Must not exfiltrate',
                action: 'block',
                mode: 'shadow',
                surfaces: ['chat_input'],
                judge_model_id: 'm1',
                resource_group_ids: ['rg-1', 'rg-2', 'rg-3'],
                enabled: false,
            }),
        );
        expect(toast.success).toHaveBeenCalledWith('Policy Updated', '"no-pii" has been saved.');
        expect(onClose).toHaveBeenCalledWith(true);
    });

    it('asks for the judges with the query the route names: enabled chat models, and nothing else', async () => {
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        await waitFor(() => expect(gatewayClient.listLLMs).toHaveBeenCalledTimes(1));
        expect(gatewayClient.listLLMs).toHaveBeenCalledWith({ mode: 'chat', enabled: true });
    });

    it('keeps a pinned judge that is no longer available visible rather than dropping it', async () => {
        gatewayClient.listLLMs.mockResolvedValue({ models: [] });
        render(<SemanticPolicyForm policy={POLICY} onClose={onClose} />);
        expect(await screen.findByRole('option', { name: 'm1 (unavailable)' })).toBeInTheDocument();
    });

    it('a refused save shows the reason, stays open, and can be dismissed', async () => {
        gatewayClient.updateSemanticPolicy.mockRejectedValue(new Error('name taken'));
        render(<SemanticPolicyForm policy={POLICY} onClose={onClose} />);
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        expect(await screen.findByText('name taken')).toBeInTheDocument();
        expect(screen.getByText('Failed to update policy')).toBeInTheDocument();
        expect(toast.error).toHaveBeenCalledWith('Save Failed', 'name taken');
        expect(onClose).not.toHaveBeenCalled();
    });

    it('a messageless failure names what failed', async () => {
        gatewayClient.createSemanticPolicy.mockRejectedValue({});
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        fillRequired();
        fireEvent.click(screen.getByRole('button', { name: 'Create Policy' }));
        expect(await screen.findByText('Failed to save semantic policy')).toBeInTheDocument();
        expect(screen.getByText('Failed to create policy')).toBeInTheDocument();
    });

    it('Cancel closes as not saved; a failed group load is said, not shown as "no groups defined"', async () => {
        gatewayClient.fetchGroupHierarchyTree.mockRejectedValue(new Error('x'));
        gatewayClient.listLLMs.mockRejectedValue(new Error('y'));
        render(<SemanticPolicyForm policy={null} onClose={onClose} />);
        fireEvent.click(screen.getByRole('button', { name: 'Access Control' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the resource groups: x');
        expect(screen.queryByText(/No Resource Groups defined yet/)).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onClose).toHaveBeenCalledWith(false);
    });

    it.todo('BUG: PATCH ignores nulls (CP semantic_policies.py:199), so clearing surfaces, judge model or description on edit silently changes nothing');
});

describe('SemanticPoliciesTab', () => {
    async function open() {
        render(<SemanticPoliciesTab />);
        await screen.findByText('no-pii');
    }
    const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

    it('asks for the judges\' names with the query the route names: enabled chat models, and nothing else', async () => {
        render(<SemanticPoliciesTab />);
        await waitFor(() => expect(gatewayClient.listLLMs).toHaveBeenCalledTimes(1));
        expect(gatewayClient.listLLMs).toHaveBeenCalledWith({ mode: 'chat', enabled: true });
    });

    it('shows counts and resolves group and judge names, falling back honestly', async () => {
        await open();
        expect(screen.getByText(/2 policies, 1 active/)).toBeInTheDocument();
        const r = within(rowOf('no-pii'));
        expect(r.getByText('Chat input')).toBeInTheDocument();
        expect(r.getByText('Block (403)')).toBeInTheDocument();
        await waitFor(() => expect(r.getByText('Judge One')).toBeInTheDocument());
        expect(r.getByText('Alpha')).toBeInTheDocument();
        expect(r.getByText('Beta')).toBeInTheDocument();
        expect(r.getByText('+1')).toBeInTheDocument();

        const w = within(rowOf('tenant-wide-warn'));
        expect(w.getByText('All surfaces')).toBeInTheDocument();
        expect(w.getByText('Tenant default')).toBeInTheDocument();
        expect(w.getByText('Tenant-wide')).toBeInTheDocument();
    });

    it('still renders when the name lookups fail, showing raw ids', async () => {
        gateway.fetchGroupHierarchyTree.mockRejectedValue(new Error('x'));
        gatewayClient.listLLMs.mockRejectedValue(new Error('y'));
        await open();
        expect(within(rowOf('no-pii')).getByText('rg-1')).toBeInTheDocument();
        expect(within(rowOf('no-pii')).getByText('m1')).toBeInTheDocument();
    });

    it.each([
        ['stops pii', ['no-pii'], ['tenant-wide-warn']],
        ['be nice', ['tenant-wide-warn'], ['no-pii']],
    ])('searching "%s" narrows the table', async (term, shown, hidden) => {
        await open();
        fireEvent.change(screen.getByPlaceholderText('Search by name, description, or constraint...'), {
            target: { value: term },
        });
        for (const n of shown) expect(screen.getByText(n)).toBeInTheDocument();
        for (const n of hidden) expect(screen.queryByText(n)).toBeNull();
    });

    it('filters by mode, action and surface, where an empty surface list means all', async () => {
        await open();
        const [mode, action, surface] = screen.getAllByRole('combobox');

        fireEvent.change(mode, { target: { value: 'enforce' } });
        expect(screen.queryByText('no-pii')).toBeNull();
        fireEvent.change(mode, { target: { value: '' } });

        fireEvent.change(action, { target: { value: 'block' } });
        expect(screen.queryByText('tenant-wide-warn')).toBeNull();
        fireEvent.change(action, { target: { value: '' } });

        fireEvent.change(surface, { target: { value: 'mcp_input' } });
        expect(screen.queryByText('no-pii')).toBeNull();
        expect(screen.getByText('tenant-wide-warn')).toBeInTheDocument();

        fireEvent.change(mode, { target: { value: 'shadow' } });
        expect(screen.getByText('No policies match the current filters.')).toBeInTheDocument();
    });

    it('toggles shadow to enforce and back, reloading after', async () => {
        await open();
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Click to toggle shadow / enforce'));
        await waitFor(() => expect(gatewayClient.updateSemanticPolicy).toHaveBeenCalledWith('p1', { mode: 'enforce' }));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Mode updated', '"no-pii" is now enforce.'));
        await waitFor(() => expect(gatewayClient.listSemanticPolicies).toHaveBeenCalledTimes(2));

        fireEvent.click(within(rowOf('tenant-wide-warn')).getByTitle('Click to toggle shadow / enforce'));
        await waitFor(() => expect(gatewayClient.updateSemanticPolicy).toHaveBeenCalledWith('p2', { mode: 'shadow' }));
    });

    it('a failed toggle is reported, and a messageless one names what failed', async () => {
        await open();
        gatewayClient.updateSemanticPolicy.mockRejectedValueOnce(new Error('locked'));
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Click to toggle shadow / enforce'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Update failed', 'locked'));

        gatewayClient.updateSemanticPolicy.mockRejectedValueOnce({});
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Click to toggle shadow / enforce'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Update failed', 'Could not change mode'));
        expect(gatewayClient.listSemanticPolicies).toHaveBeenCalledTimes(1);
    });

    it('deletes after typed confirmation and removes the row', async () => {
        await open();
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Delete policy'));
        const input = await screen.findByPlaceholderText('Enter "no-pii"');
        fireEvent.change(input, { target: { value: 'no-pii' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(gatewayClient.deleteSemanticPolicy).toHaveBeenCalledWith('p1'));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Policy Deleted', '"no-pii" has been deleted.'));
        expect(screen.queryByText('no-pii')).toBeNull();
        expect(screen.getByText('tenant-wide-warn')).toBeInTheDocument();
    });

    it('a refused delete is shown inline and keeps the row', async () => {
        gatewayClient.deleteSemanticPolicy.mockRejectedValue({});
        await open();
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Delete policy'));
        const input = await screen.findByPlaceholderText('Enter "no-pii"');
        fireEvent.change(input, { target: { value: 'no-pii' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Delete Failed', 'Failed to delete policy'));
        expect(screen.getAllByText('no-pii').length).toBeGreaterThan(0);
    });

    it('a failed first load offers Retry, which recovers', async () => {
        gatewayClient.listSemanticPolicies.mockRejectedValueOnce({});
        render(<SemanticPoliciesTab />);
        expect(await screen.findByText('Failed to Load Policies')).toBeInTheDocument();
        expect(screen.getByText('Failed to load semantic policies')).toBeInTheDocument();
        expect(toast.error).toHaveBeenCalledWith('Failed to Load', 'Unable to load semantic policies');
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        expect(await screen.findByText('no-pii')).toBeInTheDocument();
    });

    it('opens the form empty from the empty state and seeded from a row, reloading on save', async () => {
        gatewayClient.listSemanticPolicies.mockResolvedValueOnce({ policies: [] });
        render(<SemanticPoliciesTab />);
        fireEvent.click(await screen.findByRole('button', { name: 'Create First Policy' }));
        expect(screen.getByText('New Semantic Policy')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(screen.queryByText('New Semantic Policy')).toBeNull();
        expect(gatewayClient.listSemanticPolicies).toHaveBeenCalledTimes(1);
        cleanup();

        await open();
        fireEvent.click(within(rowOf('no-pii')).getByTitle('Edit policy'));
        expect(screen.getByText('Edit Semantic Policy — no-pii')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        await waitFor(() => expect(screen.queryByText('Edit Semantic Policy — no-pii')).toBeNull());
        await waitFor(() => expect(gatewayClient.listSemanticPolicies).toHaveBeenCalledTimes(3));
    });
});
