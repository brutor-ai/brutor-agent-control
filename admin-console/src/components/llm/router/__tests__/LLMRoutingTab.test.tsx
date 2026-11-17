/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Routing section shell.
 *
 * Pinned: Refresh only claims success when the status really loaded (a failed
 * refresh must not show the error toast and the success toast together), and
 * auto-refresh stops while a child form is open, so the list does not reload
 * under someone editing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, act } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../../test/llm/llmRoutingTab'));
vi.mock('react-hot-toast', async () => (await shared()).toastModule());
vi.mock('../../../../hooks/useGateway.ts', async () => (await shared()).useGatewayModule());
vi.mock('../RoutingGroupsSubTab', async () => (await shared()).groupsSubTabModule());
vi.mock('../RouterStatusSubTab', async () => (await shared()).statusSubTabModule());
vi.mock('../RouterTestSubTab', async () => (await shared()).testSubTabModule());

import LLMRoutingTab from '../LLMRoutingTab';
import { WithBuiltinSlots } from '../../../../test/builtinSlots';
import { SlotProvider } from '../../../../app/SlotProvider';
import type { TabSlotItem } from '../../../../app/slots';
import { click, client, toastMock } from '../../../../test/llm/llmRoutingTab';

async function loadTab() {
    render(<LLMRoutingTab />, { wrapper: WithBuiltinSlots });
    await screen.findByText('groups view');
}

describe('LLMRoutingTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        client.getRouterStatus.mockReset();
        client.getRouterStatus.mockResolvedValue({ routing_strategy: 'least-busy' });
    });

    afterEach(() => {
        cleanup();
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('toasts a failed first load, and still shows the page', async () => {
        client.getRouterStatus.mockRejectedValue(new Error('down'));
        await loadTab();
        expect(toastMock.error).toHaveBeenCalledWith('Failed to load router status: down');
        click('Status');
        expect(screen.getByText('status view: none')).toBeTruthy();
    });

    it('confirms a refresh that worked', async () => {
        await loadTab();
        client.getRouterStatus.mockResolvedValue({ routing_strategy: 'cost-based-routing' });
        click('Refresh');
        await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Router data refreshed'));
        click('Status');
        expect(screen.getByText('status view: cost-based-routing')).toBeTruthy();
    });

    it('does not claim success for a refresh that failed', async () => {
        await loadTab();
        client.getRouterStatus.mockRejectedValue(new Error('down'));
        click('Refresh');
        await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('Failed to load router status: down'));
        await waitFor(() =>
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh' }).disabled).toBe(false)
        );
        expect(toastMock.success).not.toHaveBeenCalled();
    });

    it('reloads when a child asks, without a toast', async () => {
        await loadTab();
        click('child refresh');
        await waitFor(() => expect(client.getRouterStatus).toHaveBeenCalledTimes(2));
        expect(toastMock.success).not.toHaveBeenCalled();
    });

    it('refreshes every 30 s, pauses while a form is open, and resumes after', async () => {
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
        await loadTab();
        expect(client.getRouterStatus).toHaveBeenCalledTimes(1);

        act(() => { vi.advanceTimersByTime(30_000); });
        await waitFor(() => expect(client.getRouterStatus).toHaveBeenCalledTimes(2));

        click('open form');
        expect(screen.getByText('Auto-refresh paused')).toBeTruthy();
        act(() => { vi.advanceTimersByTime(90_000); });
        expect(client.getRouterStatus).toHaveBeenCalledTimes(2);

        // Closing reloads straight away, then the timer is back.
        click('close form');
        expect(screen.queryByText('Auto-refresh paused')).toBeNull();
        await waitFor(() => expect(client.getRouterStatus).toHaveBeenCalledTimes(3));
        act(() => { vi.advanceTimersByTime(30_000); });
        await waitFor(() => expect(client.getRouterStatus).toHaveBeenCalledTimes(4));
    });

    it('stops refreshing once unmounted', async () => {
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
        await loadTab();
        cleanup();
        act(() => { vi.advanceTimersByTime(60_000); });
        expect(client.getRouterStatus).toHaveBeenCalledTimes(1);
    });

    // ── The `llm-routing.tabs` slot ──────────────────────────────────────────
    const VIEWS = ['Routing Groups', 'Status', 'Test Router', 'Routing Group Cache'];
    const viewTabs = () =>
        screen.getAllByRole('button').map((b) => b.textContent ?? '').filter((label) => VIEWS.includes(label));

    it('with nothing contributed it shows its own views and works', async () => {
        render(<LLMRoutingTab />);
        await screen.findByText('groups view');
        expect(viewTabs()).toEqual(['Routing Groups', 'Status', 'Test Router']);
        click('Test Router');
        expect(screen.getByText('test view')).toBeTruthy();
        expect(screen.queryByText('cache view')).toBeNull();
    });

    // A view of the tab that something contributes: a fake one, not the
    // Enterprise extension's (those cases are under `enterprise/src/host/`).
    const fakeView = (id: string, after?: string): TabSlotItem<void> => ({
        id,
        label: `Fake ${id}`,
        icon: () => null,
        after,
        render: () => <div key={id}>{`fake view ${id}`}</div>,
    });
    const allTabs = () =>
        screen.getAllByRole('button').map((b) => b.textContent ?? '').filter((label) => VIEWS.includes(label) || label.startsWith('Fake '));

    async function loadWith(views: TabSlotItem<void>[]) {
        render(
            <SlotProvider slots={{ 'llm-routing.tabs': views }}>
                <LLMRoutingTab />
            </SlotProvider>,
        );
        await screen.findByText('groups view');
    }

    it('switches between its views, a contributed one included, and hands the loaded status to Status', async () => {
        await loadWith([fakeView('extra')]);
        click('Status');
        expect(screen.getByText('status view: least-busy')).toBeTruthy();
        expect(screen.queryByText('groups view')).toBeNull();
        click('Test Router');
        expect(screen.getByText('test view')).toBeTruthy();
        click('Fake extra');
        expect(screen.getByText('fake view extra')).toBeTruthy();
        expect(screen.queryByText('test view')).toBeNull();
        click('Routing Groups');
        expect(screen.getByText('groups view')).toBeTruthy();
        expect(screen.queryByText('fake view extra')).toBeNull();
    });

    it('places a contributed view after the view it names, and last when it names none', async () => {
        await loadWith([fakeView('last'), fakeView('second', 'groups')]);
        expect(allTabs()).toEqual(['Routing Groups', 'Fake second', 'Status', 'Test Router', 'Fake last']);
    });
});
