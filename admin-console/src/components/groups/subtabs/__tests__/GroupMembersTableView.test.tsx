/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The members table.
 *
 * Membership here is of two kinds and the table has to keep them apart in
 * every column that offers an action: a DIRECT member belongs to this group
 * and can be removed or made primary here; an INHERITED member belongs to an
 * ancestor, so offering "Remove" on that row would promise a write this
 * screen cannot make. Only usage drill-down spans both — a usage rollup
 * reads logs, which do not care which group surfaced the membership.
 *
 * The header checkbox follows from the same rule: "select all" means all
 * DIRECT members, so it must not appear satisfied by a table of inherited
 * rows.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import GroupMembersTableView from '../GroupMembersTableView';
import type { GroupMember } from '../../../../lib/groupTypes';

function member(overrides: Partial<GroupMember> = {}): GroupMember {
    return {
        user_id: 'u-1',
        username: 'alice',
        display_name: 'Alice Andersson',
        email: 'alice@acme.test',
        joined_at: '2026-07-01T10:00:00Z',
        ...overrides,
    };
}

const inherited = (overrides: Partial<GroupMember> = {}) =>
    member({
        user_id: 'u-2',
        username: 'bob',
        display_name: 'Bob Berg',
        is_inherited: true,
        source_group_name: 'ACME Corp',
        ...overrides,
    });

function renderTable(members: GroupMember[], props: Partial<React.ComponentProps<typeof GroupMembersTableView>> = {}) {
    const onSelectMember = vi.fn();
    const onSelectAll = vi.fn();
    const onRemove = vi.fn();
    const view = render(
        <GroupMembersTableView
            members={members}
            selectedMembers={new Set()}
            onSelectMember={onSelectMember}
            onSelectAll={onSelectAll}
            onRemove={onRemove}
            removingId={null}
            {...props}
        />,
    );
    return { onSelectMember, onSelectAll, onRemove, ...view };
}

const rowFor = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

describe('GroupMembersTableView — reading a row', () => {
    it('shows the display name over the handle, and the email', () => {
        renderTable([member()]);
        const row = rowFor('Alice Andersson');
        expect(within(row).getByText('@alice')).toBeInTheDocument();
        expect(within(row).getByText('alice@acme.test')).toBeInTheDocument();
    });

    it('a member with no email reads as a dash, never as blank', () => {
        renderTable([member({ email: undefined })]);
        expect(within(rowFor('Alice Andersson')).getByText('-')).toBeInTheDocument();
    });

    it('names the ancestor an inherited membership came from', () => {
        renderTable([inherited()]);
        const row = rowFor('Bob Berg');
        expect(within(row).getByText('Inherited')).toBeInTheDocument();
        expect(within(row).getByText('from ACME Corp')).toBeInTheDocument();
    });

    it('a direct membership says so', () => {
        renderTable([member()]);
        expect(within(rowFor('Alice Andersson')).getByText('Direct')).toBeInTheDocument();
    });

    it('flags the user whose primary group this is', () => {
        renderTable([member({ is_primary: true })]);
        expect(within(rowFor('Alice Andersson')).getByText('Primary')).toBeInTheDocument();
    });
});

describe('GroupMembersTableView — actions follow membership kind', () => {
    it('offers remove and make-primary on a direct row', () => {
        const onSetPrimary = vi.fn();
        const { onRemove } = renderTable([member()], { onSetPrimary });
        const row = rowFor('Alice Andersson');

        fireEvent.click(within(row).getByTitle(/primary \(active\) group/i));
        expect(onSetPrimary).toHaveBeenCalledWith('u-1', 'alice');

        fireEvent.click(within(row).getByTitle('Remove member'));
        expect(onRemove).toHaveBeenCalledWith('u-1', 'alice');
    });

    it('offers neither on an inherited row — this screen cannot make that write', () => {
        renderTable([inherited()], { onSetPrimary: vi.fn() });
        const row = rowFor('Bob Berg');
        expect(within(row).queryByTitle('Remove member')).toBeNull();
        expect(within(row).queryByTitle(/primary/i)).toBeNull();
        expect(within(row).queryByRole('checkbox')).toBeNull();
    });

    it('does not offer make-primary on the row that is already primary', () => {
        renderTable([member({ is_primary: true })], { onSetPrimary: vi.fn() });
        expect(within(rowFor('Alice Andersson')).queryByTitle(/primary \(active\) group/i)).toBeNull();
    });

    it('usage drill-down is offered on inherited rows too, and hands back the whole member', () => {
        const onShowUsage = vi.fn();
        const row = inherited();
        renderTable([row], { onShowUsage });
        fireEvent.click(within(rowFor('Bob Berg')).getByTitle(/usage in this group/i));
        expect(onShowUsage).toHaveBeenCalledWith(row);
    });

    it('drops the usage column entirely when the parent offers no handler', () => {
        renderTable([member()]);
        expect(within(rowFor('Alice Andersson')).queryByTitle(/usage in this group/i)).toBeNull();
    });

    it('a removal in flight disables that row’s button and no other', () => {
        renderTable([member(), member({ user_id: 'u-3', username: 'cara', display_name: 'Cara C' })], {
            removingId: 'u-1',
        });
        expect(within(rowFor('Alice Andersson')).queryByTitle('Remove member')).toBeDisabled();
        expect(within(rowFor('Cara C')).getByTitle('Remove member')).toBeEnabled();
    });

    it('a primary change in flight disables only that row’s star', () => {
        renderTable([member(), member({ user_id: 'u-3', username: 'cara', display_name: 'Cara C' })], {
            onSetPrimary: vi.fn(),
            settingPrimaryId: 'u-1',
        });
        expect(within(rowFor('Alice Andersson')).getByTitle(/primary \(active\) group/i)).toBeDisabled();
        expect(within(rowFor('Cara C')).getByTitle(/primary \(active\) group/i)).toBeEnabled();
    });
});

describe('GroupMembersTableView — selection', () => {
    const headerCheckbox = () => screen.getByTitle('Select all direct members');

    it('select-all is unchecked while a direct member is unselected', () => {
        renderTable([member(), inherited()]);
        expect(headerCheckbox()).not.toBeChecked();
    });

    it('select-all reads as checked once every DIRECT member is selected', () => {
        renderTable([member(), inherited()], { selectedMembers: new Set(['u-1']) });
        expect(headerCheckbox()).toBeChecked();
    });

    it('an empty table never reads as "all selected"', () => {
        renderTable([]);
        expect(headerCheckbox()).not.toBeChecked();
    });

    it('toggling a row reports that user', () => {
        const { onSelectMember } = renderTable([member()]);
        fireEvent.click(within(rowFor('Alice Andersson')).getByRole('checkbox'));
        expect(onSelectMember).toHaveBeenCalledWith('u-1');
    });

    it('the header checkbox reports the bulk toggle', () => {
        const { onSelectAll } = renderTable([member()]);
        fireEvent.click(headerCheckbox());
        expect(onSelectAll).toHaveBeenCalled();
    });
});

describe('GroupMembersTableView — the footer count', () => {
    it('splits the total into direct and inherited', () => {
        renderTable([member(), inherited(), inherited({ user_id: 'u-4', username: 'dan', display_name: 'Dan D' })]);
        expect(screen.getByText('3 members')).toBeInTheDocument();
        expect(screen.getByText('Direct:').nextElementSibling).toHaveTextContent('1');
        expect(screen.getByText('Inherited:').nextElementSibling).toHaveTextContent('2');
    });

    it('says "1 member", singular', () => {
        renderTable([member()]);
        expect(screen.getByText('1 member')).toBeInTheDocument();
    });

    it('shows a selected count only while something is selected', () => {
        const { rerender } = renderTable([member()]);
        expect(screen.queryByText('Selected:')).toBeNull();
        rerender(
            <GroupMembersTableView
                members={[member()]}
                selectedMembers={new Set(['u-1'])}
                onSelectMember={vi.fn()}
                onSelectAll={vi.fn()}
                onRemove={vi.fn()}
                removingId={null}
            />,
        );
        expect(screen.getByText('Selected:').nextElementSibling).toHaveTextContent('1');
    });
});
