/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Re-parenting a group in the estate tree.
 *
 * The rule this screen exists to enforce is that a group cannot be moved
 * under itself or under anything beneath it — that would make the hierarchy
 * a cycle, and every inheritance walk in the platform (limits, policy,
 * budget capacity) assumes it is a tree. So the group itself and its whole
 * subtree are struck out as targets, labelled with WHY, and cannot be
 * clicked. The server would refuse it anyway; refusing here means the
 * operator never composes an impossible move.
 *
 * Second rule: "move" to where it already lives is not a move. The button
 * stays disabled and the summary line stays hidden, so the modal never
 * promises a write it will not make.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchGroupHierarchyTree = vi.fn();
const mockUpdateResourceGroup = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchGroupHierarchyTree: mockFetchGroupHierarchyTree,
        updateResourceGroup: mockUpdateResourceGroup,
    }),
}));

import MoveGroupModal from '../MoveGroupModal';
import { GroupType, type ResourceGroup, type GroupHierarchyTree } from '../../../../lib/groupTypes';

function node(
    id: string,
    name: string,
    children: GroupHierarchyTree[] = [],
    group_type = GroupType.TEAM,
): GroupHierarchyTree {
    return {
        id,
        name,
        display_name: name,
        group_type,
        hierarchy_level: 0,
        is_active: true,
        inherit_resources: true,
        member_count: 0,
        server_config_count: 0,
        llm_model_count: 0,
        api_key_count: 0,
        children,
    };
}

/**
 *   acme (org)
 *     ├── engineering (dept)   ← the group being moved
 *     │     └── platform (team)
 *     └── finance (dept)
 */
const TREE = [
    node(
        'acme',
        'ACME',
        [
            node('engineering', 'Engineering', [node('platform', 'Platform')], GroupType.DEPARTMENT),
            node('finance', 'Finance', [], GroupType.DEPARTMENT),
        ],
        GroupType.ORGANIZATION,
    ),
];

function group(overrides: Partial<ResourceGroup> = {}): ResourceGroup {
    return {
        id: 'engineering',
        name: 'Engineering',
        display_name: 'Engineering',
        group_type: GroupType.DEPARTMENT,
        parent_group_id: 'acme',
        ancestors: [{ id: 'acme', name: 'ACME' }],
        ...overrides,
    } as unknown as ResourceGroup;
}

function renderModal(g = group()) {
    const onMove = vi.fn();
    const onClose = vi.fn();
    render(<MoveGroupModal group={g} onMove={onMove} onClose={onClose} />);
    return { onMove, onClose };
}

// The group's own label also appears in the modal header ("Select a new
// parent for …"), so tree rows are matched on the row's name span only.
const targetButton = (label: string) =>
    screen.getByText(label, { selector: 'span.truncate' }).closest('button') as HTMLButtonElement;

/** Wait for the tree to arrive, then hand back the row's button. */
const findTarget = async (label: string) => {
    await screen.findByText(label, { selector: 'span.truncate' });
    return targetButton(label);
};

const moveButton = () => screen.getByRole('button', { name: /move group/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchGroupHierarchyTree.mockResolvedValue({ tree: TREE });
    mockUpdateResourceGroup.mockResolvedValue({});
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('MoveGroupModal — the cycle guard', () => {
    it('strikes out the group itself, labelled as such', async () => {
        renderModal();
        const self = await findTarget('Engineering (Department)');
        expect(self).toBeDisabled();
        expect(self).toHaveTextContent('This group');
    });

    it('strikes out its descendants, labelled as descendants', async () => {
        renderModal();
        const self = await findTarget('Engineering (Department)');
        // Open the struck-out branch — an operator must still be able to LOOK
        // inside a group they cannot move into.
        fireEvent.click(self.querySelector('span.cursor-pointer') as HTMLElement);
        const child = await findTarget('Platform (Team)');
        expect(child).toBeDisabled();
        expect(child).toHaveTextContent('Descendant');
    });

    it('clicking a struck-out target selects nothing', async () => {
        renderModal();
        fireEvent.click(await findTarget('Engineering (Department)'));
        // Still parked on the current parent, so still not a move.
        expect(moveButton()).toBeDisabled();
    });

    it('a sibling outside the subtree is a legal target', async () => {
        renderModal();
        expect(await findTarget('Finance (Department)')).toBeEnabled();
    });
});

describe('MoveGroupModal — a move is only a move if it changes something', () => {
    it('opens parked on the current parent with the action disabled', async () => {
        renderModal();
        await screen.findByText('ACME (Organization)');
        expect(moveButton()).toBeDisabled();
        expect(screen.queryByText(/Move to:/)).toBeNull();
    });

    it('marks where the group lives today', async () => {
        renderModal();
        expect(await findTarget('ACME (Organization)')).toHaveTextContent('Current');
    });

    it('a root group treats "Root (no parent)" as its current home', async () => {
        renderModal(group({ parent_group_id: undefined, ancestors: [] }));
        const root = await screen.findByText('Root (no parent)');
        expect(root.closest('button')).toHaveTextContent('Current');
        expect(moveButton()).toBeDisabled();
    });

    it('picking a new parent names the destination and arms the action', async () => {
        renderModal();
        fireEvent.click(await findTarget('Finance (Department)'));
        expect(screen.getByText('Finance (Department)', { selector: 'strong' })).toBeInTheDocument();
        expect(moveButton()).toBeEnabled();
    });

    it('promoting to the top level is a legal move for a nested group', async () => {
        renderModal();
        fireEvent.click(await screen.findByText('Root (no parent)'));
        expect(screen.getByText('Root (top level)')).toBeInTheDocument();
        expect(moveButton()).toBeEnabled();
    });
});

describe('MoveGroupModal — committing', () => {
    it('PATCHes the new parent and tells the caller', async () => {
        const { onMove } = renderModal();
        fireEvent.click(await findTarget('Finance (Department)'));
        fireEvent.click(moveButton());

        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('engineering', {
                parent_group_id: 'finance',
            }),
        );
        await waitFor(() => expect(onMove).toHaveBeenCalled());
    });

    it('a promotion to root sends an explicit null parent', async () => {
        renderModal();
        fireEvent.click(await screen.findByText('Root (no parent)'));
        fireEvent.click(moveButton());
        await waitFor(() =>
            expect(mockUpdateResourceGroup).toHaveBeenCalledWith('engineering', {
                parent_group_id: null,
            }),
        );
    });

    it('a refused move surfaces the server’s reason and re-arms the button', async () => {
        mockUpdateResourceGroup.mockRejectedValue(new Error('would create a cycle'));
        const { onMove } = renderModal();
        fireEvent.click(await findTarget('Finance (Department)'));
        fireEvent.click(moveButton());

        expect(await screen.findByText('would create a cycle')).toBeInTheDocument();
        expect(moveButton()).toBeEnabled();
        expect(onMove).not.toHaveBeenCalled();
    });

    it('cancel closes without writing', async () => {
        const { onClose } = renderModal();
        await screen.findByText('ACME (Organization)');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onClose).toHaveBeenCalled();
        expect(mockUpdateResourceGroup).not.toHaveBeenCalled();
    });
});

describe('MoveGroupModal — loading the estate', () => {
    it('a tree that will not load says so instead of offering an empty picker', async () => {
        mockFetchGroupHierarchyTree.mockRejectedValue(new Error('hierarchy unavailable'));
        renderModal();
        expect(await screen.findByText('hierarchy unavailable')).toBeInTheDocument();
        expect(screen.queryByText('ACME (Organization)', { selector: 'span.truncate' })).toBeNull();
    });

    it('auto-expands the ancestors so the current location is on screen', async () => {
        renderModal();
        // ACME is an ancestor, so Engineering (its child) is visible without
        // the operator hunting for it.
        expect(await findTarget('Engineering (Department)')).toBeInTheDocument();
    });

    it('with no ancestors it falls back to expanding the roots', async () => {
        renderModal(group({ id: 'orphan', parent_group_id: undefined, ancestors: [] }));
        expect(await findTarget('Engineering (Department)')).toBeInTheDocument();
    });
});
