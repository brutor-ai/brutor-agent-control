/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Saved log views.
 *
 * Pinned: a new view captures the parent's live filter envelope with the chosen
 * scope and sharing; applying hands the stored envelope back untouched; owner-only
 * actions are hidden on a colleague's view; and every failed mutation tells the
 * operator instead of pretending it worked.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gatewayClient = {
    listSavedLogViews: vi.fn(),
    createSavedLogView: vi.fn(),
    updateSavedLogView: vi.fn(),
    deleteSavedLogView: vi.fn(),
};
const gateway = { gatewayClient };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import SavedViewMenu from '../SavedViewMenu';

const MINE = { id: 'v1', name: 'My errors', owner_admin_id: 'me', is_shared: false, filters: { status: ['error'] } };
const THEIRS = { id: 'v2', name: 'Their view', owner_admin_id: 'other', is_shared: true, filters: { q: 'x' } };
const PRIVATE_OTHER = { id: 'v3', name: 'Hidden', owner_admin_id: 'other', is_shared: false, filters: {} };

const onApply = vi.fn();
const FILTERS = { time_range: '24h' };
const alertSpy = vi.fn();
const confirmSpy = vi.fn();

async function open(adminId: string | null = 'me') {
    render(<SavedViewMenu scope="proxy_logs" currentFilters={FILTERS} onApply={onApply} currentAdminId={adminId} />);
    await waitFor(() => expect(gatewayClient.listSavedLogViews).toHaveBeenCalledWith('proxy_logs'));
    await waitFor(() => expect(gatewayClient.listSavedLogViews.mock.results[0].type).toBe('return'));
    fireEvent.click(screen.getByTitle('Saved views'));
    await screen.findByText('Save current as new view…');
}

/** Opens the kebab on a row and returns it. */
function kebab(name: string) {
    // The trigger button repeats the active view's name, so look for the row.
    const li = screen.getAllByText(name).map((el) => el.closest('li')).find(Boolean) as HTMLElement;
    fireEvent.click(within(li).getByRole('button', { name: 'View actions' }));
    return within(li);
}

/** The dialog's Rename, which follows any Rename still in the kebab. */
function lastRename(): HTMLElement {
    const all = screen.getAllByRole('button', { name: 'Rename' });
    return all[all.length - 1];
}

describe('SavedViewMenu', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.stubGlobal('alert', alertSpy);
        vi.stubGlobal('confirm', confirmSpy);
        for (const fn of Object.values(gatewayClient)) fn.mockReset();
        gatewayClient.listSavedLogViews.mockResolvedValue({ items: [MINE, THEIRS, PRIVATE_OTHER] });
        gatewayClient.createSavedLogView.mockResolvedValue({ id: 'v9' });
        gatewayClient.updateSavedLogView.mockResolvedValue({});
        gatewayClient.deleteSavedLogView.mockResolvedValue(undefined);
    });

    afterEach(() => {
        cleanup();
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('splits Mine from Shared and hides a colleague\'s private view', async () => {
        await open();
        expect(await screen.findByText('My errors')).toBeInTheDocument();
        expect(screen.getByText('Their view')).toBeInTheDocument();
        expect(screen.queryByText('Hidden')).toBeNull();
    });

    it('with no views, says there are none', async () => {
        gatewayClient.listSavedLogViews.mockResolvedValue({ items: [] });
        render(<SavedViewMenu scope="proxy_logs" currentFilters={FILTERS} onApply={onApply} currentAdminId="me" />);
        fireEvent.click(screen.getByTitle('Saved views'));
        expect(await screen.findByText('No saved views yet.')).toBeInTheDocument();
        expect(screen.getByText('None shared in tenant.')).toBeInTheDocument();
    });

    it('a load that fails is not "no saved views": the menu says the views could not be loaded, with a Retry', async () => {
        gatewayClient.listSavedLogViews.mockRejectedValue(new Error('x'));
        render(<SavedViewMenu scope="proxy_logs" currentFilters={FILTERS} onApply={onApply} currentAdminId="me" />);
        fireEvent.click(screen.getByTitle('Saved views'));
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the saved views: x');
        expect(screen.queryByText('No saved views yet.')).toBeNull();
        expect(screen.queryByText('None shared in tenant.')).toBeNull();
        expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    });

    it('applying hands back the stored filters and names the active view', async () => {
        await open();
        fireEvent.click(await screen.findByText('My errors'));
        expect(onApply).toHaveBeenCalledWith({ status: ['error'] });
        expect(screen.queryByText('Save current as new view…')).toBeNull();
        expect(screen.getByTitle('Saved views')).toHaveTextContent('My errors');
    });

    it('saves the live filters under the trimmed name with the chosen sharing', async () => {
        await open();
        fireEvent.click(screen.getByText('Save current as new view…'));
        const name = screen.getByPlaceholderText('View name');
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        fireEvent.change(name, { target: { value: '  Prod  ' } });
        fireEvent.click(screen.getByRole('checkbox'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));

        await waitFor(() =>
            expect(gatewayClient.createSavedLogView).toHaveBeenCalledWith({
                scope: 'proxy_logs',
                name: 'Prod',
                filters: FILTERS,
                is_shared: true,
            }),
        );
        await waitFor(() => expect(gatewayClient.listSavedLogViews).toHaveBeenCalledTimes(2));
        expect(screen.queryByPlaceholderText('View name')).toBeNull();
    });

    it('Enter saves, Escape dismisses, and a blank name sends nothing', async () => {
        await open();
        fireEvent.click(screen.getByText('Save current as new view…'));
        const name = screen.getByPlaceholderText('View name');
        fireEvent.keyDown(name, { key: 'Enter' });
        expect(gatewayClient.createSavedLogView).not.toHaveBeenCalled();
        fireEvent.keyDown(name, { key: 'Escape' });
        expect(screen.queryByPlaceholderText('View name')).toBeNull();
    });

    it('a name collision is reported and the dialog stays', async () => {
        gatewayClient.createSavedLogView.mockRejectedValue({ detail: 'name already used' });
        await open();
        fireEvent.click(screen.getByText('Save current as new view…'));
        const name = screen.getByPlaceholderText('View name');
        fireEvent.change(name, { target: { value: 'Dup' } });
        fireEvent.keyDown(name, { key: 'Enter' });
        await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('name already used'));
        expect(screen.getByPlaceholderText('View name')).toBeInTheDocument();
    });

    it('offers mutations on your own view and only a note on a colleague\'s', async () => {
        await open();
        await screen.findByText('My errors');
        const theirs = kebab('Their view');
        expect(theirs.getByText('Read-only (owned by another admin)')).toBeInTheDocument();
        expect(theirs.queryByText('Delete')).toBeNull();

        const mine = kebab('My errors');
        expect(mine.getByText('Share with tenant')).toBeInTheDocument();
        expect(mine.getByText('Delete')).toBeInTheDocument();
    });

    it('renames with the new name only; an unchanged name sends nothing', async () => {
        await open();
        await screen.findByText('My errors');
        fireEvent.click(kebab('My errors').getByText('Rename'));
        fireEvent.click(lastRename());
        expect(gatewayClient.updateSavedLogView).not.toHaveBeenCalled();
        expect(screen.queryByText('Rename view')).toBeNull();

        fireEvent.click(kebab('My errors').getByText('Rename'));
        const input = screen.getByDisplayValue('My errors');
        fireEvent.change(input, { target: { value: 'Renamed' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(gatewayClient.updateSavedLogView).toHaveBeenCalledWith('v1', { name: 'Renamed' }));
        await waitFor(() => expect(gatewayClient.listSavedLogViews).toHaveBeenCalledTimes(2));
    });

    it('a failed rename is reported and the dialog stays', async () => {
        gatewayClient.updateSavedLogView.mockRejectedValue(new Error('taken'));
        await open();
        await screen.findByText('My errors');
        fireEvent.click(kebab('My errors').getByText('Rename'));
        fireEvent.change(screen.getByDisplayValue('My errors'), { target: { value: 'New' } });
        fireEvent.click(lastRename());
        await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('taken'));
        expect(screen.getByText('Rename view')).toBeInTheDocument();
    });

    it('toggles sharing to the opposite of the current state, and reports failure', async () => {
        await open();
        await screen.findByText('My errors');
        fireEvent.click(kebab('My errors').getByText('Share with tenant'));
        await waitFor(() => expect(gatewayClient.updateSavedLogView).toHaveBeenCalledWith('v1', { is_shared: true }));

        gatewayClient.updateSavedLogView.mockRejectedValueOnce({});
        await waitFor(() => expect(gatewayClient.listSavedLogViews).toHaveBeenCalledTimes(2));
        fireEvent.click(kebab('My errors').getByText('Share with tenant'));
        await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('Failed to update sharing'));
    });

    it('deletes only after confirmation and clears the active view', async () => {
        await open();
        fireEvent.click(await screen.findByText('My errors'));
        expect(screen.getByTitle('Saved views')).toHaveTextContent('My errors');

        fireEvent.click(screen.getByTitle('Saved views'));
        confirmSpy.mockReturnValue(false);
        fireEvent.click(kebab('My errors').getByText('Delete'));
        await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith('Delete saved view "My errors"?'));
        expect(gatewayClient.deleteSavedLogView).not.toHaveBeenCalled();

        confirmSpy.mockReturnValue(true);
        gatewayClient.listSavedLogViews.mockResolvedValue({ items: [THEIRS] });
        fireEvent.click(screen.getByText('Delete'));
        await waitFor(() => expect(gatewayClient.deleteSavedLogView).toHaveBeenCalledWith('v1'));
        await waitFor(() => expect(screen.getByTitle('Saved views')).not.toHaveTextContent('My errors'));
    });

    it('a failed delete is reported', async () => {
        confirmSpy.mockReturnValue(true);
        gatewayClient.deleteSavedLogView.mockRejectedValue(new Error('not yours'));
        await open();
        await screen.findByText('My errors');
        fireEvent.click(kebab('My errors').getByText('Delete'));
        await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('not yours'));
    });

    it('updates the active view with the live filters, for its owner only', async () => {
        await open();
        fireEvent.click(await screen.findByText('Their view'));
        fireEvent.click(screen.getByTitle('Saved views'));
        expect(screen.queryByText(/with current filters/)).toBeNull();

        fireEvent.click(screen.getByText('My errors'));
        fireEvent.click(screen.getByTitle('Saved views'));
        fireEvent.click(screen.getByText('Update "My errors" with current filters'));
        await waitFor(() => expect(gatewayClient.updateSavedLogView).toHaveBeenCalledWith('v1', { filters: FILTERS }));

        gatewayClient.updateSavedLogView.mockRejectedValue(new Error('nope'));
        await waitFor(() => expect(gatewayClient.listSavedLogViews).toHaveBeenCalledTimes(2));
        fireEvent.click(screen.getByTitle('Saved views'));
        fireEvent.click(await screen.findByText('Update "My errors" with current filters'));
        await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('nope'));
    });

    it('clicking outside closes the dropdown', async () => {
        await open();
        fireEvent.mouseDown(document.body);
        expect(screen.queryByText('Save current as new view…')).toBeNull();
    });
});
