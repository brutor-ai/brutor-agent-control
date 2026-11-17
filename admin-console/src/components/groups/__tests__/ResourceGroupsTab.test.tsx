/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Resource Groups pane — the shell between the sidebar tree and the
 * detail panel.
 *
 * It owns exactly four states and the transitions between them: nothing
 * selected (an empty state that can still create a group), loading, a load
 * failure that names the reason, and the detail panel. The failure state is
 * the one worth defending — a group that will not load must NOT fall back to
 * the "select a group" screen, or the operator reads a fetch error as "you
 * clicked nothing".
 *
 * It also owns the fan-out after a write: create / update / delete each have
 * to tell App the tree changed, or the sidebar keeps showing an estate that
 * no longer exists.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchResourceGroup = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ fetchResourceGroup: mockFetchResourceGroup }),
}));

vi.mock('../GroupDetailsPanel', () => ({
    default: ({ group, onUpdate, onDelete, initialTab }: {
        group: { name: string };
        onUpdate: () => void;
        onDelete: () => void;
        initialTab?: string;
    }) => (
        <div data-testid="details-panel">
            <span data-testid="details-group">{group.name}</span>
            <span data-testid="details-initial-tab">{initialTab ?? 'none'}</span>
            <button onClick={onUpdate}>panel-update</button>
            <button onClick={onDelete}>panel-delete</button>
        </div>
    ),
}));

vi.mock('../CreateGroupModal', () => ({
    default: ({ onClose, onSuccess }: { onClose: () => void; onSuccess: (id?: string) => void }) => (
        <div data-testid="create-modal">
            <button onClick={onClose}>modal-close</button>
            <button onClick={() => onSuccess('new-group')}>modal-created</button>
            <button onClick={() => onSuccess()}>modal-created-no-id</button>
        </div>
    ),
}));

import ResourceGroupsTab from '../ResourceGroupsTab';

const GROUP = { id: 'g-1', name: 'engineering' };

function renderTab(props: Partial<React.ComponentProps<typeof ResourceGroupsTab>> = {}) {
    const onGroupTreeChanged = vi.fn();
    const onNavigateToGroup = vi.fn();
    const view = render(
        <ResourceGroupsTab
            onGroupTreeChanged={onGroupTreeChanged}
            onNavigateToGroup={onNavigateToGroup}
            {...props}
        />,
    );
    return { onGroupTreeChanged, onNavigateToGroup, ...view };
}

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchResourceGroup.mockResolvedValue(GROUP);
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('ResourceGroupsTab — the four states', () => {
    it('with nothing selected, invites the operator to pick or create', () => {
        renderTab();
        expect(screen.getByText('Select a Resource Group')).toBeInTheDocument();
        expect(mockFetchResourceGroup).not.toHaveBeenCalled();
    });

    it('a selected group is fetched with its stats, ancestors and children', async () => {
        renderTab({ selectedGroupId: 'g-1' });
        await waitFor(() =>
            expect(mockFetchResourceGroup).toHaveBeenCalledWith('g-1', {
                stats: true,
                ancestors: true,
                children: true,
            }),
        );
        expect(await screen.findByTestId('details-panel')).toBeInTheDocument();
        expect(screen.getByTestId('details-group')).toHaveTextContent('engineering');
    });

    it('a failed load names the reason and does NOT fall back to the empty state', async () => {
        mockFetchResourceGroup.mockRejectedValue(new Error('group is gone'));
        renderTab({ selectedGroupId: 'g-1' });
        expect(await screen.findByText('Failed to Load Group')).toBeInTheDocument();
        expect(screen.getByText('group is gone')).toBeInTheDocument();
        expect(screen.queryByText('Select a Resource Group')).toBeNull();
    });

    it('deselecting drops back to the empty state without another fetch', async () => {
        const { rerender, onGroupTreeChanged, onNavigateToGroup } = renderTab({ selectedGroupId: 'g-1' });
        await screen.findByTestId('details-panel');
        mockFetchResourceGroup.mockClear();

        rerender(
            <ResourceGroupsTab
                onGroupTreeChanged={onGroupTreeChanged}
                onNavigateToGroup={onNavigateToGroup}
            />,
        );
        expect(screen.getByText('Select a Resource Group')).toBeInTheDocument();
        expect(mockFetchResourceGroup).not.toHaveBeenCalled();
    });

    it('navigating to another group re-fetches and swaps the panel', async () => {
        const { rerender, onGroupTreeChanged, onNavigateToGroup } = renderTab({ selectedGroupId: 'g-1' });
        await screen.findByTestId('details-panel');

        mockFetchResourceGroup.mockResolvedValue({ id: 'g-2', name: 'finance' });
        rerender(
            <ResourceGroupsTab
                selectedGroupId="g-2"
                onGroupTreeChanged={onGroupTreeChanged}
                onNavigateToGroup={onNavigateToGroup}
            />,
        );
        await waitFor(() => expect(screen.getByTestId('details-group')).toHaveTextContent('finance'));
    });
});

describe('ResourceGroupsTab — deep links into the detail panel', () => {
    it('passes a deep-linked tab straight through', async () => {
        renderTab({ selectedGroupId: 'g-1', initialDetailTab: 'usage' });
        await screen.findByTestId('details-panel');
        expect(screen.getByTestId('details-initial-tab')).toHaveTextContent('usage');
    });
});

describe('ResourceGroupsTab — writes fan out to the tree', () => {
    it('an update refreshes the tree and re-reads the group', async () => {
        const { onGroupTreeChanged } = renderTab({ selectedGroupId: 'g-1' });
        await screen.findByTestId('details-panel');
        mockFetchResourceGroup.mockClear();

        fireEvent.click(screen.getByText('panel-update'));
        await waitFor(() => expect(onGroupTreeChanged).toHaveBeenCalled());
        expect(mockFetchResourceGroup).toHaveBeenCalledWith('g-1', expect.any(Object));
    });

    it('a failed re-read after an update still leaves the tree refreshed', async () => {
        const { onGroupTreeChanged } = renderTab({ selectedGroupId: 'g-1' });
        await screen.findByTestId('details-panel');
        mockFetchResourceGroup.mockRejectedValue(new Error('flaky'));

        fireEvent.click(screen.getByText('panel-update'));
        await waitFor(() => expect(onGroupTreeChanged).toHaveBeenCalled());
        // The panel stays on the last good read rather than blanking.
        expect(screen.getByTestId('details-panel')).toBeInTheDocument();
    });

    it('a delete clears the panel and navigates away from the dead group', async () => {
        const { onGroupTreeChanged, onNavigateToGroup } = renderTab({ selectedGroupId: 'g-1' });
        await screen.findByTestId('details-panel');

        fireEvent.click(screen.getByText('panel-delete'));
        expect(onGroupTreeChanged).toHaveBeenCalled();
        expect(onNavigateToGroup).toHaveBeenCalledWith('');
        expect(screen.queryByTestId('details-panel')).toBeNull();
    });
});

describe('ResourceGroupsTab — the create modal', () => {
    it('opens from the empty state and closes again', () => {
        renderTab();
        expect(screen.queryByTestId('create-modal')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: /new group/i }));
        expect(screen.getByTestId('create-modal')).toBeInTheDocument();
        fireEvent.click(screen.getByText('modal-close'));
        expect(screen.queryByTestId('create-modal')).toBeNull();
    });

    it('opens on the external trigger from App', () => {
        renderTab({ showCreateModal: true });
        expect(screen.getByTestId('create-modal')).toBeInTheDocument();
    });

    it('creating a group closes the modal, refreshes the tree and selects the new group', () => {
        const { onGroupTreeChanged, onNavigateToGroup } = renderTab();
        fireEvent.click(screen.getByRole('button', { name: /new group/i }));
        fireEvent.click(screen.getByText('modal-created'));

        expect(screen.queryByTestId('create-modal')).toBeNull();
        expect(onGroupTreeChanged).toHaveBeenCalled();
        expect(onNavigateToGroup).toHaveBeenCalledWith('new-group');
    });

    it('a create that returns no id refreshes the tree without navigating anywhere', () => {
        const { onGroupTreeChanged, onNavigateToGroup } = renderTab();
        fireEvent.click(screen.getByRole('button', { name: /new group/i }));
        fireEvent.click(screen.getByText('modal-created-no-id'));

        expect(onGroupTreeChanged).toHaveBeenCalled();
        expect(onNavigateToGroup).not.toHaveBeenCalled();
    });

    it('is reachable over the detail panel too', async () => {
        renderTab({ selectedGroupId: 'g-1', showCreateModal: true });
        await screen.findByTestId('details-panel');
        expect(screen.getByTestId('create-modal')).toBeInTheDocument();
    });
});
