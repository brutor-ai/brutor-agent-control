/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End-User Groups: who inherits which Resource Group access.
 *
 * Membership and bindings grant access to every member at once, so the pins are
 * on the exact ids sent for add/remove/bind/unbind, the read-only rule for
 * the roster of a group an identity provider created (not changed here), and that a failure is visible where the operator is looking: the
 * delete modal and the pickers cover the page, so an error rendered behind them
 * reads as a click that did nothing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gatewayClient = {
    fetchEndUserGroups: vi.fn(),
    fetchEndUserGroupDetail: vi.fn(),
    createEndUserGroup: vi.fn(),
    deleteEndUserGroup: vi.fn(),
    addEndUserGroupMembers: vi.fn(),
    removeEndUserGroupMembers: vi.fn(),
    bindEndUserGroupToResourceGroups: vi.fn(),
    unbindEndUserGroupFromResourceGroups: vi.fn(),
    fetchEndUsers: vi.fn(),
    fetchGroupHierarchyTree: vi.fn(),
};
const gateway = { gatewayClient };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import EndUserGroupsTab from '../EndUserGroupsTab';

const MANUAL = {
    id: 'eug-1',
    tenant_id: 't-1',
    name: 'eng',
    display_name: 'Engineering',
    description: 'All engineers',
    source: 'manual',
    member_count: 2,
};
const SAML = {
    id: 'eug-2',
    tenant_id: 't-1',
    name: 'okta-sales',
    source: 'saml',
    idp_server_id: 'idp-okta',
    source_external_id: 'ext-9',
    member_count: 1,
};

const ALICE = { id: 1, username: 'alice', email: 'alice@acme.test', is_active: true };

function detail(over: Record<string, unknown> = {}) {
    return { ...MANUAL, members: [ALICE], resource_groups: [{ id: 'rg-1', name: 'prod', display_name: 'Production' }], ...over };
}

function listOf(groups: unknown[], total = groups.length) {
    return { groups, pagination: { total, offset: 0, limit: 50, returned: groups.length }, filters: {} };
}

/** The shape GatewayAPIError gives a FastAPI `{detail: {code, message}}` body. */
function apiError(message: string, code?: string) {
    return Object.assign(new Error(message), { status: 409, details: { detail: { code, message } } });
}

function dialogOf(heading: string): HTMLElement {
    const box = screen.getByRole('heading', { name: heading }).closest<HTMLElement>('.fixed');
    if (!box) throw new Error(`no dialog around "${heading}"`);
    return box;
}

async function openPanel(name = 'eng') {
    fireEvent.click(await screen.findByRole('button', { name: `Manage members of ${name}` }));
}

describe('EndUserGroupsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gatewayClient.fetchEndUserGroups.mockResolvedValue(listOf([MANUAL, SAML]));
        gatewayClient.fetchEndUserGroupDetail.mockResolvedValue(detail());
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('list', () => {
        it('asks for the first page and shows name, source, count and IdP provenance', async () => {
            render(<EndUserGroupsTab />);

            expect(await screen.findByText('Engineering')).toBeInTheDocument();
            expect(gatewayClient.fetchEndUserGroups).toHaveBeenCalledWith({
                search: undefined,
                source: undefined,
                limit: 50,
                offset: 0,
            });
            const row = screen.getByText('okta-sales').closest('tr');
            if (!row) throw new Error('no row');
            // A group a provider created is shown by its stored source, with nothing of SAML offered.
            expect(within(row).getByText('saml')).toBeInTheDocument();
            expect(within(row).queryByText('SAML')).toBeNull();
            expect(within(row).getByText(/idp-okta/)).toBeInTheDocument();
            expect(within(row).getByText(/ext-9/)).toBeInTheDocument();
            expect(screen.getByText('eng')).toBeInTheDocument();
            expect(screen.getByText('All engineers')).toBeInTheDocument();
        });

        it('sends search and source to the server and resets to the first page', async () => {
            render(<EndUserGroupsTab />);
            await screen.findByText('Engineering');

            fireEvent.change(screen.getByPlaceholderText(/Search by name/), { target: { value: 'sal' } });
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups).toHaveBeenLastCalledWith({
                    search: 'sal',
                    source: undefined,
                    limit: 50,
                    offset: 0,
                }),
            );

            expect(Array.from((screen.getByRole('combobox') as HTMLSelectElement).options).map((o) => o.value)).toEqual(['all', 'manual']);
            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'manual' } });
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups).toHaveBeenLastCalledWith({
                    search: 'sal',
                    source: 'manual',
                    limit: 50,
                    offset: 0,
                }),
            );
        });

        it('tells "nothing matches" apart from "nothing exists"', async () => {
            gatewayClient.fetchEndUserGroups.mockResolvedValue(listOf([]));
            render(<EndUserGroupsTab />);
            expect(await screen.findByText(/No user groups yet/)).toBeInTheDocument();

            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'manual' } });
            expect(await screen.findByText(/No user groups match/)).toBeInTheDocument();
        });

        it('shows a load failure instead of an empty list', async () => {
            gatewayClient.fetchEndUserGroups.mockRejectedValue(new Error('backend down'));
            render(<EndUserGroupsTab />);
            expect(await screen.findByText('backend down')).toBeInTheDocument();
        });

        it('pages with an offset and discloses the total', async () => {
            gatewayClient.fetchEndUserGroups.mockResolvedValue(listOf([MANUAL, SAML], 120));
            render(<EndUserGroupsTab />);

            expect(await screen.findByText(/Showing 1–2 of 120/)).toBeInTheDocument();
            expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();

            fireEvent.click(screen.getByRole('button', { name: 'Next' }));
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups).toHaveBeenLastCalledWith(
                    expect.objectContaining({ offset: 50 }),
                ),
            );
            fireEvent.click(await screen.findByRole('button', { name: 'Prev' }));
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups).toHaveBeenLastCalledWith(
                    expect.objectContaining({ offset: 0 }),
                ),
            );
        });

        it('steps back a page when the last row of the last page is gone', async () => {
            gatewayClient.fetchEndUserGroups.mockImplementation((q: { offset: number }) =>
                Promise.resolve(q.offset === 50 ? listOf([], 50) : listOf([MANUAL], 50 + 1)),
            );
            render(<EndUserGroupsTab />);
            await screen.findByText(/of 51/);
            fireEvent.click(screen.getByRole('button', { name: 'Next' }));

            // Without the step back this reads "No user groups yet", the pager is
            // gone, and 50 groups are out of reach.
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups).toHaveBeenLastCalledWith(
                    expect.objectContaining({ offset: 0 }),
                ),
            );
            expect(await screen.findByText('Engineering')).toBeInTheDocument();
            expect(screen.queryByText(/No user groups yet/)).not.toBeInTheDocument();
        });
    });

    describe('create', () => {
        async function openCreate() {
            render(<EndUserGroupsTab />);
            await screen.findByText('Engineering');
            fireEvent.click(screen.getByRole('button', { name: 'New User Group' }));
            return dialogOf('Create User Group');
        }

        it('blocks a blank name without calling the server', async () => {
            const box = await openCreate();
            fireEvent.change(screen.getByPlaceholderText('engineering-team'), { target: { value: '   ' } });
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));

            expect(await within(box).findByText('Name is required.')).toBeInTheDocument();
            expect(gatewayClient.createEndUserGroup).not.toHaveBeenCalled();
        });

        it('sends the trimmed fields, then opens the new group so members can be added', async () => {
            gatewayClient.createEndUserGroup.mockResolvedValue({ id: 'eug-new' });
            const box = await openCreate();

            fireEvent.change(screen.getByPlaceholderText('engineering-team'), { target: { value: ' ops ' } });
            fireEvent.change(screen.getByPlaceholderText('Engineering Team'), { target: { value: ' Ops Team ' } });
            const description = box.querySelector('textarea');
            if (!description) throw new Error('no textarea');
            fireEvent.change(description, { target: { value: ' on call ' } });
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));

            await waitFor(() =>
                expect(gatewayClient.createEndUserGroup).toHaveBeenCalledWith({
                    name: 'ops',
                    display_name: 'Ops Team',
                    description: 'on call',
                }),
            );
            await waitFor(() => expect(gatewayClient.fetchEndUserGroupDetail).toHaveBeenCalledWith('eug-new'));
            expect(screen.queryByRole('heading', { name: 'Create User Group' })).not.toBeInTheDocument();
            expect(gatewayClient.fetchEndUserGroups.mock.calls.length).toBeGreaterThan(1);
        });

        it('leaves optional fields out rather than sending empty strings', async () => {
            gatewayClient.createEndUserGroup.mockResolvedValue({ id: 'eug-new' });
            const box = await openCreate();

            fireEvent.change(screen.getByPlaceholderText('engineering-team'), { target: { value: 'ops' } });
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));

            await waitFor(() =>
                expect(gatewayClient.createEndUserGroup).toHaveBeenCalledWith({
                    name: 'ops',
                    display_name: undefined,
                    description: undefined,
                }),
            );
        });

        it('explains a name conflict in the operator\'s terms and keeps the form open', async () => {
            gatewayClient.createEndUserGroup.mockRejectedValue(
                apiError("EndUserGroup 'ops' already exists in tenant", 'name_conflict'),
            );
            const box = await openCreate();

            fireEvent.change(screen.getByPlaceholderText('engineering-team'), { target: { value: ' ops ' } });
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));

            expect(await within(box).findByText('A group named "ops" already exists.')).toBeInTheDocument();
            expect(screen.getByRole('heading', { name: 'Create User Group' })).toBeInTheDocument();
        });

        it('shows the server message for other coded errors, and a plain error otherwise', async () => {
            gatewayClient.createEndUserGroup.mockRejectedValueOnce(apiError('Quota reached', 'quota'));
            const box = await openCreate();
            fireEvent.change(screen.getByPlaceholderText('engineering-team'), { target: { value: 'ops' } });
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));
            expect(await within(box).findByText('Quota reached')).toBeInTheDocument();

            gatewayClient.createEndUserGroup.mockRejectedValueOnce(new Error('network down'));
            fireEvent.click(within(box).getByRole('button', { name: 'Create' }));
            expect(await within(box).findByText('network down')).toBeInTheDocument();
        });

        it('closes on Cancel without creating', async () => {
            const box = await openCreate();
            fireEvent.click(within(box).getByRole('button', { name: 'Cancel' }));
            expect(screen.queryByRole('heading', { name: 'Create User Group' })).not.toBeInTheDocument();
            expect(gatewayClient.createEndUserGroup).not.toHaveBeenCalled();
        });
    });

    describe('delete', () => {
        const deleteButton = () => screen.getByRole('button', { name: 'Delete Resource Group' });

        it('needs the name typed, deletes that group, and refreshes', async () => {
            gatewayClient.deleteEndUserGroup.mockResolvedValue(undefined);
            render(<EndUserGroupsTab />);
            fireEvent.click(await screen.findByRole('button', { name: 'Delete eng' }));

            // Opening delete from the row must not also open the detail panel.
            expect(gatewayClient.fetchEndUserGroupDetail).not.toHaveBeenCalled();
            expect(screen.getByText(/revoke RG access from every member/)).toBeInTheDocument();
            expect(deleteButton()).toBeDisabled();

            const before = gatewayClient.fetchEndUserGroups.mock.calls.length;
            fireEvent.change(screen.getByPlaceholderText('Enter "Engineering"'), { target: { value: 'Engineering' } });
            fireEvent.click(deleteButton());

            await waitFor(() => expect(gatewayClient.deleteEndUserGroup).toHaveBeenCalledWith('eug-1'));
            await waitFor(() =>
                expect(gatewayClient.fetchEndUserGroups.mock.calls.length).toBeGreaterThan(before),
            );
            await waitFor(() =>
                expect(screen.queryByPlaceholderText('Enter "Engineering"')).not.toBeInTheDocument(),
            );
        });

        it('says what deleting a group an identity provider created does, and nothing of SAML', async () => {
            render(<EndUserGroupsTab />);
            fireEvent.click(await screen.findByRole('button', { name: 'Delete okta-sales' }));
            expect(
                screen.getByText('This group was created by an identity provider. It is removed here, with the access it gave its members; the members themselves are not deleted.'),
            ).toBeInTheDocument();
            expect(screen.queryByText(/SAML/)).toBeNull();
        });

        it('reports a failure inside the modal, where the operator is looking', async () => {
            gatewayClient.deleteEndUserGroup.mockRejectedValue(new Error('still bound'));
            render(<EndUserGroupsTab />);
            fireEvent.click(await screen.findByRole('button', { name: 'Delete eng' }));
            fireEvent.change(screen.getByPlaceholderText('Enter "Engineering"'), { target: { value: 'Engineering' } });
            fireEvent.click(deleteButton());

            expect(await screen.findByText('Delete failed')).toBeInTheDocument();
            const failed = screen.getByText('Delete failed').parentElement;
            if (!failed) throw new Error('no error box');
            expect(within(failed).getByText('still bound')).toBeInTheDocument();
            expect(screen.getByPlaceholderText('Enter "Engineering"')).toBeInTheDocument();
        });

        it.todo(
            'BUG: the end-user-group delete modal says "Delete Resource Group" and warns that AI model assignments, MCP bindings and API keys are removed. EndUserGroupsTab.tsx passes itemType="group", which GenericDeleteConfirmationModal maps to Resource Group copy; the shared modal needs an end-user-group item type',
        );
    });

    describe('detail panel', () => {
        it('opens from the row and shows roster and bindings', async () => {
            render(<EndUserGroupsTab />);
            fireEvent.click(await screen.findByText('Engineering'));

            expect(await screen.findByText('Members (1)')).toBeInTheDocument();
            expect(gatewayClient.fetchEndUserGroupDetail).toHaveBeenCalledWith('eug-1');
            expect(screen.getByText('alice')).toBeInTheDocument();
            expect(screen.getByText('alice@acme.test')).toBeInTheDocument();
            expect(screen.getByText('Resource Group bindings (1)', { exact: false })).toBeInTheDocument();
            expect(screen.getByText('Production')).toBeInTheDocument();
        });

        it('shows why the panel is empty when the detail fails to load', async () => {
            gatewayClient.fetchEndUserGroupDetail.mockRejectedValue(new Error('gone'));
            render(<EndUserGroupsTab />);
            await openPanel();
            expect(await screen.findByText('gone')).toBeInTheDocument();
        });

        it('says when a group grants nothing', async () => {
            gatewayClient.fetchEndUserGroupDetail.mockResolvedValue(detail({ members: [], resource_groups: [] }));
            render(<EndUserGroupsTab />);
            await openPanel();
            expect(await screen.findByText('No members yet.')).toBeInTheDocument();
            expect(screen.getByText(/members get no resource access via this/)).toBeInTheDocument();
        });

        it('locks the roster of an IdP-sourced group and names who owns it', async () => {
            gatewayClient.fetchEndUserGroupDetail.mockResolvedValue({
                ...SAML,
                members: [ALICE],
                resource_groups: [],
            });
            render(<EndUserGroupsTab />);
            await openPanel('okta-sales');

            expect(await screen.findByText(/Created by an identity provider/)).toBeInTheDocument();
            expect(screen.queryByText(/SAML/)).toBeNull();
            expect(screen.getByText('idp-okta', { selector: 'code' })).toBeInTheDocument();
            expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
            expect(screen.getByRole('button', { name: 'Remove alice' })).toBeDisabled();
            // No reconciler exists in this release: the lock names none.
            expect(screen.getByRole('button', { name: 'Add' })).toHaveAttribute('title', 'The roster of a group an identity provider created is not changed here.');
            // Bindings are local policy, not IdP state, so they stay editable.
            expect(screen.getByRole('button', { name: 'Bind' })).toBeEnabled();
        });

        it('closes on the panel close button', async () => {
            render(<EndUserGroupsTab />);
            await openPanel();
            await screen.findByText('Members (1)');
            fireEvent.click(screen.getByRole('button', { name: 'Close' }));
            expect(screen.queryByText('Members (1)')).not.toBeInTheDocument();
        });

        describe('members', () => {
            const BOB = { id: 2, username: 'bob', email: 'bob@acme.test' };
            const CAROL = { id: 3, username: 'carol', email: 'carol@acme.test' };

            async function openPicker() {
                gatewayClient.fetchEndUsers.mockResolvedValue({ users: [ALICE, BOB, CAROL] });
                render(<EndUserGroupsTab />);
                await openPanel();
                await screen.findByText('Members (1)');
                fireEvent.click(screen.getByRole('button', { name: 'Add' }));
                return dialogOf('Add members');
            }

            it('offers only non-members and filters by username or email', async () => {
                const box = await openPicker();

                expect(await within(box).findByRole('checkbox', { name: /bob/ })).toBeInTheDocument();
                expect(gatewayClient.fetchEndUsers).toHaveBeenCalledWith({ limit: 500 });
                expect(within(box).queryByRole('checkbox', { name: /alice/ })).not.toBeInTheDocument();

                fireEvent.change(within(box).getByPlaceholderText('Search users…'), {
                    target: { value: 'CAROL@' },
                });
                expect(within(box).getByRole('checkbox', { name: /carol/ })).toBeInTheDocument();
                expect(within(box).queryByRole('checkbox', { name: /bob/ })).not.toBeInTheDocument();
            });

            it('sends exactly the ticked ids and shows the roster the server returned', async () => {
                gatewayClient.addEndUserGroupMembers.mockResolvedValue(
                    detail({ members: [ALICE, { ...CAROL, is_active: true }] }),
                );
                const box = await openPicker();

                fireEvent.click(await within(box).findByRole('checkbox', { name: /bob/ }));
                fireEvent.click(within(box).getByRole('checkbox', { name: /carol/ }));
                fireEvent.click(within(box).getByRole('checkbox', { name: /bob/ }));
                const before = gatewayClient.fetchEndUserGroups.mock.calls.length;
                fireEvent.click(within(box).getByRole('button', { name: 'Add 1' }));

                await waitFor(() =>
                    expect(gatewayClient.addEndUserGroupMembers).toHaveBeenCalledWith('eug-1', [3]),
                );
                expect(await screen.findByText('Members (2)')).toBeInTheDocument();
                expect(screen.queryByRole('heading', { name: 'Add members' })).not.toBeInTheDocument();
                // The list's member_count has to follow.
                await waitFor(() =>
                    expect(gatewayClient.fetchEndUserGroups.mock.calls.length).toBeGreaterThan(before),
                );
            });

            it('cannot submit with nobody ticked', async () => {
                const box = await openPicker();
                await within(box).findByRole('checkbox', { name: /bob/ });
                const submit = within(box)
                    .getAllByRole('button')
                    .find((b) => b.textContent?.startsWith('Add'));
                expect(submit).toBeDisabled();
            });

            it('reports a failed add inside the picker and keeps the selection open', async () => {
                gatewayClient.addEndUserGroupMembers.mockRejectedValue(new Error('cross-tenant user'));
                const box = await openPicker();

                fireEvent.click(await within(box).findByRole('checkbox', { name: /bob/ }));
                fireEvent.click(within(box).getByRole('button', { name: 'Add 1' }));

                expect(await within(box).findByText('cross-tenant user')).toBeInTheDocument();
                expect(within(box).getByRole('checkbox', { name: /bob/ })).toBeChecked();
            });

            it('does not pass off a failed user lookup as "no users available" alone', async () => {
                gatewayClient.fetchEndUsers.mockRejectedValue(new Error('users unavailable'));
                render(<EndUserGroupsTab />);
                await openPanel();
                await screen.findByText('Members (1)');
                fireEvent.click(screen.getByRole('button', { name: 'Add' }));

                const box = dialogOf('Add members');
                expect(await within(box).findByText('users unavailable')).toBeInTheDocument();
            });

            it('removes one member by id', async () => {
                gatewayClient.removeEndUserGroupMembers.mockResolvedValue(detail({ members: [] }));
                render(<EndUserGroupsTab />);
                await openPanel();
                fireEvent.click(await screen.findByRole('button', { name: 'Remove alice' }));

                await waitFor(() =>
                    expect(gatewayClient.removeEndUserGroupMembers).toHaveBeenCalledWith('eug-1', [1]),
                );
                expect(await screen.findByText('Members (0)')).toBeInTheDocument();
            });

            it('reports a failed removal, then clears it once the next action succeeds', async () => {
                gatewayClient.removeEndUserGroupMembers.mockRejectedValueOnce(new Error('locked'));
                gatewayClient.removeEndUserGroupMembers.mockResolvedValueOnce(detail({ members: [] }));
                render(<EndUserGroupsTab />);
                await openPanel();
                fireEvent.click(await screen.findByRole('button', { name: 'Remove alice' }));

                expect(await screen.findByText('locked')).toBeInTheDocument();
                expect(screen.getByText('Members (1)')).toBeInTheDocument();

                fireEvent.click(screen.getByRole('button', { name: 'Remove alice' }));
                expect(await screen.findByText('Members (0)')).toBeInTheDocument();
                expect(screen.queryByText('locked')).not.toBeInTheDocument();
            });

            it.todo(
                'BUG: the member picker silently stops at 500 users. EndUserGroupsTab.tsx openAddMember fetches {limit: 500} and filters that array in the browser, so a user past row 500 reads as "No users available to add". Same defect class as the old EndUsersListTab cap: search must go to the server',
            );
        });

        describe('resource group bindings', () => {
            const TREE = {
                tree: [
                    {
                        id: 'rg-1',
                        name: 'prod',
                        children: [
                            {
                                id: 'rg-2',
                                name: 'payments',
                                display_name: 'Payments',
                                group_type: 'team',
                                children: [{ id: 'rg-3', name: 'fraud', children: [] }],
                            },
                        ],
                    },
                ],
            };

            async function openBind() {
                gatewayClient.fetchGroupHierarchyTree.mockResolvedValue(TREE);
                render(<EndUserGroupsTab />);
                await openPanel();
                await screen.findByText('Members (1)');
                fireEvent.click(screen.getByRole('button', { name: 'Bind' }));
                return dialogOf('Bind to Resource Groups');
            }

            it('flattens the hierarchy and hides what is already bound', async () => {
                const box = await openBind();

                expect(await within(box).findByRole('checkbox', { name: /fraud/ })).toBeInTheDocument();
                expect(within(box).getByRole('checkbox', { name: /Payments/ })).toBeInTheDocument();
                expect(within(box).queryByText('rg-1')).not.toBeInTheDocument();
            });

            it('binds exactly the ticked Resource Groups', async () => {
                gatewayClient.bindEndUserGroupToResourceGroups.mockResolvedValue(
                    detail({
                        resource_groups: [
                            { id: 'rg-1', name: 'prod' },
                            { id: 'rg-3', name: 'fraud' },
                        ],
                    }),
                );
                const box = await openBind();

                fireEvent.click(await within(box).findByRole('checkbox', { name: /fraud/ }));
                fireEvent.click(within(box).getByRole('checkbox', { name: /Payments/ }));
                fireEvent.click(within(box).getByRole('checkbox', { name: /Payments/ }));
                fireEvent.click(within(box).getByRole('button', { name: 'Bind 1' }));

                await waitFor(() =>
                    expect(gatewayClient.bindEndUserGroupToResourceGroups).toHaveBeenCalledWith('eug-1', ['rg-3']),
                );
                expect(await screen.findByText('Resource Group bindings (2)', { exact: false })).toBeInTheDocument();
                expect(screen.queryByRole('heading', { name: 'Bind to Resource Groups' })).not.toBeInTheDocument();
            });

            it('reports a failed bind inside the picker', async () => {
                gatewayClient.bindEndUserGroupToResourceGroups.mockRejectedValue(new Error('no permission'));
                const box = await openBind();

                fireEvent.click(await within(box).findByRole('checkbox', { name: /fraud/ }));
                fireEvent.click(within(box).getByRole('button', { name: 'Bind 1' }));

                expect(await within(box).findByText('no permission')).toBeInTheDocument();
                expect(within(box).getByRole('checkbox', { name: /fraud/ })).toBeChecked();
            });

            it('reports a failed tree lookup inside the picker', async () => {
                gatewayClient.fetchGroupHierarchyTree.mockRejectedValue(new Error('tree down'));
                render(<EndUserGroupsTab />);
                await openPanel();
                await screen.findByText('Members (1)');
                fireEvent.click(screen.getByRole('button', { name: 'Bind' }));

                const box = dialogOf('Bind to Resource Groups');
                expect(await within(box).findByText('tree down')).toBeInTheDocument();
            });

            it('unbinds one Resource Group by id', async () => {
                gatewayClient.unbindEndUserGroupFromResourceGroups.mockResolvedValue(detail({ resource_groups: [] }));
                render(<EndUserGroupsTab />);
                await openPanel();
                fireEvent.click(await screen.findByRole('button', { name: 'Unbind prod' }));

                await waitFor(() =>
                    expect(gatewayClient.unbindEndUserGroupFromResourceGroups).toHaveBeenCalledWith('eug-1', ['rg-1']),
                );
                expect(await screen.findByText(/not bound to any Resource Group/)).toBeInTheDocument();
            });

            it('reports a failed unbind and keeps the binding on screen', async () => {
                gatewayClient.unbindEndUserGroupFromResourceGroups.mockRejectedValue(new Error('cannot unbind'));
                render(<EndUserGroupsTab />);
                await openPanel();
                fireEvent.click(await screen.findByRole('button', { name: 'Unbind prod' }));

                expect(await screen.findByText('cannot unbind')).toBeInTheDocument();
                expect(screen.getByText('Production')).toBeInTheDocument();
            });
        });
    });
});
