/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Settings page and its `settings.tabs` slot: the page's own tabs in
 * their order, a contributed tab placed after the tab it names, and a page
 * that works with nothing contributed.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../configuration/TenantSettingsTab.tsx', () => ({
    default: ({ initialSubTab }: { initialSubTab?: string }) => <div>panel: tenant {initialSubTab ?? ''}</div>,
}));
vi.mock('../configuration/SearchConfigurationTab.tsx', () => ({ default: () => <div>panel: search</div> }));
vi.mock('../configuration/NotificationSettingsTab.tsx', () => ({ default: () => <div>panel: notifications</div> }));
vi.mock('../configuration/AppearanceSettings.tsx', () => ({ default: () => <div>panel: appearance</div> }));

import ConfigurationTab from '../ConfigurationTab';
import { WithBuiltinSlots } from '../../test/builtinSlots';
import { SlotProvider } from '../../app/SlotProvider';
import type { TabSlotItem } from '../../app/slots';

const tabLabels = () => screen.getAllByRole('button').map((b) => b.textContent);

describe('ConfigurationTab — with the built-in slot items', () => {
    afterEach(cleanup);

    it.each([
        ['Search Configuration', 'panel: search'],
        ['Notifications', 'panel: notifications'],
        ['Appearance', 'panel: appearance'],
    ])('%s shows only its own panel', (tab, panel) => {
        render(<ConfigurationTab />, { wrapper: WithBuiltinSlots });
        fireEvent.click(screen.getByRole('button', { name: tab }));
        expect(screen.getByText(panel)).toBeInTheDocument();
        expect(screen.queryByText('panel: registry')).not.toBeInTheDocument();
    });

    it('an unknown deep link opens Tenant', () => {
        render(<ConfigurationTab initialTab="atlantis" />, { wrapper: WithBuiltinSlots });
        expect(screen.getByText(/panel: tenant/)).toBeInTheDocument();
    });
});

describe('ConfigurationTab — with nothing contributed', () => {
    afterEach(cleanup);

    it('shows its own tabs and works', () => {
        render(<ConfigurationTab />);
        expect(tabLabels()).toEqual(['Tenant', 'Search Configuration', 'Notifications', 'Appearance']);
        fireEvent.click(screen.getByRole('button', { name: 'Appearance' }));
        expect(screen.getByText('panel: appearance')).toBeInTheDocument();
    });

    it('a deep link to a tab that nothing contributes opens Tenant', () => {
        render(<ConfigurationTab initialTab="registry" />);
        expect(screen.getByText(/panel: tenant/)).toBeInTheDocument();
        expect(screen.queryByText('panel: registry')).not.toBeInTheDocument();
    });
});

// A tab that something contributes: a fake one, not the Enterprise
// extension's (those cases are under `enterprise/src/host/`).
describe('ConfigurationTab — with a fake tab contributed', () => {
    afterEach(cleanup);

    const fakeTab = (id: string, after?: string): TabSlotItem<void> => ({
        id,
        label: `Fake ${id}`,
        icon: () => null,
        after,
        render: () => <div key={id}>{`panel: fake ${id}`}</div>,
    });
    const renderWith = (tabs: TabSlotItem<void>[], props: React.ComponentProps<typeof ConfigurationTab> = {}) =>
        render(
            <SlotProvider slots={{ 'settings.tabs': tabs }}>
                <ConfigurationTab {...props} />
            </SlotProvider>,
        );

    it('places a contributed tab after the tab it names, and last when it names none; Tenant stays the tab it opens on', () => {
        renderWith([fakeTab('last'), fakeTab('second', 'tenant')]);
        expect(tabLabels()).toEqual(['Tenant', 'Fake second', 'Search Configuration', 'Notifications', 'Appearance', 'Fake last']);
        expect(screen.getByText(/panel: tenant/)).toBeInTheDocument();
    });

    it('a contributed tab shows what it renders, and none of the page\'s own panels', () => {
        renderWith([fakeTab('second', 'tenant')]);
        fireEvent.click(screen.getByRole('button', { name: 'Fake second' }));
        expect(screen.getByText('panel: fake second')).toBeInTheDocument();
        expect(screen.queryByText(/panel: tenant/)).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Appearance' }));
        expect(screen.getByText('panel: appearance')).toBeInTheDocument();
        expect(screen.queryByText('panel: fake second')).not.toBeInTheDocument();
    });

    it('a deep link opens a contributed tab by its id, and the tenant sub-tab is passed on with one contributed', () => {
        const first = renderWith([fakeTab('second', 'tenant')], { initialTab: 'second' });
        expect(screen.getByText('panel: fake second')).toBeInTheDocument();
        first.unmount();

        renderWith([fakeTab('second', 'tenant')], { initialTab: 'tenant', initialTenantSubTab: 'evidence' });
        expect(screen.getByText('panel: tenant evidence')).toBeInTheDocument();
    });
});
