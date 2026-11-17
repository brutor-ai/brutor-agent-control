/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant settings sub-tabs, and the three the platform operator sets:
 * Quotas, Features and Billing.
 *
 * The three are items of the `tenant-settings.tabs` slot. The last block is
 * the page with nothing contributed.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const doubles = vi.hoisted(() => () => import('../../../test/configuration/tenantSettingsTab'));
vi.mock('../../../hooks/useGateway', async () => (await doubles()).useGatewayModule());
vi.mock('../../common/ConfirmationDialog', async () => (await doubles()).confirmModule());
vi.mock('../../common/Toast', async () => (await doubles()).toastModule());
vi.mock('../TenantEvidencePanel', async () => (await doubles()).evidencePanelModule());

import TenantSettingsTab from '../TenantSettingsTab';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { heading, resetOperatorTabs, strip } from '../../../test/configuration/tenantOperatorTabs';
import { request } from '../../../test/configuration/tenantSettingsTab';
import { SlotProvider } from '../../../app/SlotProvider';
import type { SlotRegistry, TenantSettingsSlotProps } from '../../../app/slots';

beforeEach(resetOperatorTabs);

/** `slots: false` renders the page with nothing contributed to its slots. */
async function open(initialSubTab?: string, { slots = true } = {}) {
    render(<TenantSettingsTab initialSubTab={initialSubTab} />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
    await screen.findByText('Profile');
}

describe('TenantSettingsTab — the sub-tabs', () => {

    it('an unknown deep link opens on Identity', async () => {
        await open('no-such-tab');
        expect(heading()).toBe('Identity');
    });

    it('an editable sub-tab keeps its save bar', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Profile' }));
        expect(screen.getByRole('button', { name: /Save changes/ })).toBeTruthy();
        expect(screen.queryByText('read-only')).toBeNull();
    });
});

describe('TenantSettingsTab — with nothing contributed', () => {
    it('shows its own sub-tabs only', async () => {
        await open(undefined, { slots: false });
        expect(strip()).toEqual([
            'Identity',
            'Profile',
            'Security',
            'Default Models',
            'Data Residency',
            'Data Retention',
            'AI System Assurance',
            'Evidence',
        ]);
        fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
        expect(screen.getByText('evidence panel')).toBeTruthy();
    });

    it('a deep link to a sub-tab nothing contributes opens on Identity', async () => {
        await open('billing', { slots: false });
        expect(heading()).toBe('Identity');
    });
});


// The page's own part of what `enterprise/src/host/.../TenantSettingsTab.operatorTabs.test.tsx`
// shows with the operator's sub-tabs (Quotas, Features, Billing) on it, here
// with stand-ins the test provides itself.
describe('TenantSettingsTab — with stand-in sub-tabs contributed', () => {
    const Icon = () => null;
    /** What each stand-in was last rendered with, by its id. */
    const given = new Map<string, TenantSettingsSlotProps>();
    const body = (id: string) => (props: TenantSettingsSlotProps) => {
        given.set(id, props);
        return <div data-testid={`body-${id}`}>{props.tenant.name}</div>;
    };
    const SLOTS: SlotRegistry = {
        'tenant-settings.tabs': [
            { id: 'fake-last', label: 'Fake last', icon: Icon, render: body('fake-last') },
            { id: 'fake-mid', label: 'Fake mid', icon: Icon, after: 'security', render: body('fake-mid') },
        ],
    };

    async function openWithStandIns(initialSubTab?: string) {
        given.clear();
        render(
            <SlotProvider slots={SLOTS}>
                <TenantSettingsTab initialSubTab={initialSubTab} />
            </SlotProvider>,
        );
        await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
        await screen.findByText('Profile');
    }

    it('places a contributed sub-tab after the sub-tab it names, or last', async () => {
        await openWithStandIns();
        expect(heading()).toBe('Identity');
        expect(strip()).toEqual([
            'Identity',
            'Profile',
            'Security',
            'Fake mid',
            'Default Models',
            'Data Residency',
            'Data Retention',
            'AI System Assurance',
            'Evidence',
            'Fake last',
        ]);
    });

    it('a contributed sub-tab is read-only, has no save bar, and is given the tenant the page loaded', async () => {
        await openWithStandIns();
        expect(given.size).toBe(0);
        fireEvent.click(screen.getByRole('button', { name: 'Fake last' }));
        expect(heading()).toBe('Fake last');
        expect(screen.getByText('read-only')).toBeTruthy();
        expect(screen.queryByRole('button', { name: /Save changes/ })).toBeNull();
        expect(screen.getByTestId('body-fake-last').textContent).toBe('t1');
        const props = given.get('fake-last')!;
        expect(Object.keys(props)).toEqual(['tenant']);
        expect(props.tenant).toMatchObject({
            id: 't1',
            quotas: { max_users: 25 },
            features: { knowledge_base: false },
            billing_plan: 'enterprise',
            billing_email: 'billing@example.com',
        });
        // Only the sub-tab shown is rendered.
        expect(given.has('fake-mid')).toBe(false);

        // Back on one of the page's own, the save bar is back.
        fireEvent.click(screen.getByRole('button', { name: 'Profile' }));
        expect(screen.getByRole('button', { name: /Save changes/ })).toBeTruthy();
        expect(screen.queryByText('read-only')).toBeNull();
        expect(screen.queryByTestId('body-fake-last')).toBeNull();
    });

    it('a deep link opens on a contributed sub-tab', async () => {
        await openWithStandIns('fake-mid');
        expect(heading()).toBe('Fake mid');
        expect(screen.getByTestId('body-fake-mid')).toBeTruthy();
    });
});
