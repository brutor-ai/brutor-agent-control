/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Default Models sub-tab of the tenant settings page: the embedding model
 * with its Knowledge Base volume and re-embed, and the judge model.
 *
 * The embedding model takes part in the page's Save (a changed model needs
 * Save pressed twice) and is contributed to the page (slot
 * `tenant-settings.model-sections`). The last block is the page with
 * nothing contributed.
 */

import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const doubles = vi.hoisted(() => () => import('../../../test/configuration/tenantSettingsTab'));
vi.mock('../../../hooks/useGateway', async () => (await doubles()).useGatewayModule());
vi.mock('../../common/ConfirmationDialog', async () => (await doubles()).confirmModule());
vi.mock('../../common/Toast', async () => (await doubles()).toastModule());
vi.mock('../TenantEvidencePanel', async () => (await doubles()).evidencePanelModule());

import TenantSettingsTab from '../TenantSettingsTab';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import {
    answer,
    calls,
    EMBEDDING_URL,
    goTo,
    judge,
    JUDGE_URL,
    patches,
    resetDefaultModels,
    saveButton,
    statsRequests,
    TENANT,
    type World,
} from '../../../test/configuration/tenantDefaultModels';
import { toast } from '../../../test/configuration/tenantSettingsTab';
import { SlotProvider } from '../../../app/SlotProvider';
import type { TenantModelField, TenantModelSectionProps, TenantModelSectionSlotItem } from '../../../app/slots';
import type { TenantModelOption } from '../../../lib/tenantModelOptions';

async function open(world: World = {}, { slots = true, initialSubTab = 'embedding' } = {}) {
    answer(world);
    render(<TenantSettingsTab initialSubTab={initialSubTab} />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByRole('button', { name: 'Profile' });
}

beforeEach(resetDefaultModels);

describe('TenantSettingsTab — Default Models, with the embedding model contributed', () => {

    it('the sub-tab is editable and sits where it did', async () => {
        await open({}, { initialSubTab: 'identity' });
        const strip = screen.getByRole('button', { name: 'Profile' }).parentElement as HTMLElement;
        const labels = within(strip)
            .getAllByRole('button')
            .map((b) => b.textContent);
        expect(labels.slice(0, 5)).toEqual(['Identity', 'Profile', 'Security', 'Default Models', 'Data Residency']);
        goTo('Default Models');
        expect(screen.queryByText('read-only')).not.toBeInTheDocument();
        expect(saveButton()).toBeInTheDocument();
    });
});

describe('TenantSettingsTab — Default Models, with nothing contributed', () => {
    it('is the judge model alone, with no divider above it', async () => {
        await open({}, { slots: false });
        const headings = screen.getAllByRole('heading', { level: 3 });
        expect(headings.map((h) => h.textContent?.trim())).toEqual(['Default Models', 'Judge model']);
        expect(headings[1].parentElement?.className).toBe('');
        expect(screen.queryByText('Default embedding model')).not.toBeInTheDocument();
        expect(screen.queryByText('Embedding model')).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Re-embed KB content' })).not.toBeInTheDocument();
        expect(judge().value).toBe('j1');
    });

    it('reads neither the embedding models nor the Knowledge Base volume', async () => {
        await open({}, { slots: false });
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(calls((path) => path === EMBEDDING_URL)).toEqual([]);
        expect(statsRequests()).toBe(0);
        expect(calls((path) => path === JUDGE_URL)).toHaveLength(1);
    });

    it('opens even when the embedding models could not be listed', async () => {
        await open({ embedding: new Error('models are down') }, { slots: false });
        expect(toast.error).not.toHaveBeenCalled();
        expect(judge().value).toBe('j1');
    });

    it('saves with one press and leaves the embedding model out of what is sent', async () => {
        await open({}, { slots: false });
        fireEvent.change(judge(), { target: { value: 'j2' } });
        expect(saveButton()).toHaveTextContent('Save changes');
        fireEvent.click(saveButton());
        await waitFor(() => expect(patches()).toHaveLength(1));
        expect(patches()[0].body).toEqual({
            display_name: 'Tenant One',
            organization_name: 'Org',
            domain: null,
            primary_contact_email: 'a@b.c',
            primary_contact_name: null,
            default_judge_model_id: 'j2',
        });
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Tenant settings saved', 'Your changes are live.'));
    });
});

// The page's own part of what `enterprise/src/host/.../TenantSettingsTab.defaultModels.test.tsx`
// shows with the embedding model on it: the load, the value it holds, the
// two presses of Save and the section kept mounted, here with a stand-in
// section the test provides itself. What the embedding model's section says
// and does (the volume, the warning, the re-embed) is the Enterprise half's.
describe('TenantSettingsTab — Default Models, with a stand-in model section contributed', () => {
    // A field of the tenant's settings the contract names and this page does
    // not edit itself.
    const FIELD: TenantModelField = 'default_embedding_model_id';
    const FAKE_MODELS: TenantModelOption[] = [
        { id: 'f1', model_name: 'fake-one', provider: 'acme', enabled: true },
        { id: 'f2', model_name: 'fake-two', provider: 'acme', enabled: true },
    ];
    /** What the section was last rendered with, and how often it was mounted. */
    let given: TenantModelSectionProps | undefined;
    let mounts = 0;
    const loadModels = vi.fn<TenantModelSectionSlotItem['loadModels']>();

    function FakeSection(props: TenantModelSectionProps) {
        // The initialiser runs once per mount.
        React.useState(() => (mounts += 1));
        given = props;
        return (
            <div>
                <label>
                    Fake model
                    <select aria-label="Fake model" value={props.value} onChange={(e) => props.onChange(e.target.value)}>
                        <option value="">none</option>
                        {props.models.map((m) => (
                            <option key={m.id} value={m.id}>
                                {m.model_name}
                            </option>
                        ))}
                    </select>
                </label>
                {props.changed && <p>fake: changed</p>}
                {props.confirming && <p>fake: confirming</p>}
            </div>
        );
    }
    const SECTION: TenantModelSectionSlotItem = {
        id: 'fake-model',
        title: 'Fake model section',
        icon: () => null,
        field: FIELD,
        loadModels,
        Section: FakeSection,
    };
    const fake = () => screen.getByRole<HTMLSelectElement>('combobox', { name: 'Fake model', hidden: true });
    const pick = (id: string) => fireEvent.change(fake(), { target: { value: id } });

    async function openWithSection(world: World = {}, initialSubTab = 'embedding') {
        answer({ tenant: { ...TENANT, [FIELD]: 'f1' }, ...world });
        render(
            <SlotProvider slots={{ 'tenant-settings.model-sections': [SECTION] }}>
                <TenantSettingsTab initialSubTab={initialSubTab} />
            </SlotProvider>,
        );
        await screen.findByRole('button', { name: 'Profile' });
    }

    beforeEach(() => {
        given = undefined;
        mounts = 0;
        loadModels.mockReset();
        loadModels.mockResolvedValue(FAKE_MODELS);
    });

    it('shows the section under its title above the judge model, given the stored model and the models it listed', async () => {
        await openWithSection();
        const headings = screen.getAllByRole('heading', { level: 3 });
        expect(headings.map((h) => h.textContent?.trim())).toEqual(['Default Models', 'Fake model section', 'Judge model']);
        // The judge model is divided from the sections above it.
        expect(headings[2].parentElement?.className).toBe('pt-6 border-t border-default');
        expect(loadModels).toHaveBeenCalledTimes(1);
        // Asked with the page's gateway client: a request through it is the page's request.
        const client = loadModels.mock.calls[0][0];
        await client.request('/through-the-client');
        expect(calls((path) => path === '/through-the-client')).toHaveLength(1);
        expect(Object.keys(given ?? {}).sort()).toEqual([
            'changed', 'confirming', 'models', 'onChange', 'stored', 'tenantId', 'value',
        ]);
        expect(given).toMatchObject({ tenantId: 't1', stored: 'f1', value: 'f1', models: FAKE_MODELS, changed: false, confirming: false });
        expect(fake().value).toBe('f1');
        expect(judge().value).toBe('j1');
        expect(saveButton()).toHaveTextContent('Save changes');
    });

    it('a changed model is told to the section, and is saved under its field on the second press of Save', async () => {
        await openWithSection();
        pick('f2');
        expect(given).toMatchObject({ stored: 'f1', value: 'f2', changed: true, confirming: false });
        expect(saveButton()).toHaveTextContent('Review changes…');

        fireEvent.click(saveButton());
        expect(await screen.findByText('fake: confirming')).toBeInTheDocument();
        expect(given).toMatchObject({ changed: true, confirming: true });
        expect(patches()).toEqual([]);
        expect(saveButton()).toHaveTextContent('Save changes');

        fireEvent.click(saveButton());
        await waitFor(() => expect(patches()).toHaveLength(1));
        expect(patches()[0].path).toBe('/v1/admin/tenants/current');
        expect(patches()[0].body).toEqual({
            display_name: 'Tenant One',
            organization_name: 'Org',
            domain: null,
            primary_contact_email: 'a@b.c',
            primary_contact_name: null,
            [FIELD]: 'f2',
            default_judge_model_id: 'j1',
        });
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Tenant settings saved', 'Your changes are live.'));
        // Saved: the new model is the stored one, so nothing is left to confirm.
        await waitFor(() => expect(given).toMatchObject({ stored: 'f2', value: 'f2', changed: false, confirming: false }));
        expect(saveButton()).toHaveTextContent('Save changes');
    });

    it('clearing the model is no change to confirm: saved at once, as null', async () => {
        await openWithSection();
        pick('');
        expect(given).toMatchObject({ value: '', changed: false });
        expect(saveButton()).toHaveTextContent('Save changes');
        fireEvent.click(saveButton());
        await waitFor(() => expect(patches()).toHaveLength(1));
        expect(patches()[0].body).toMatchObject({ [FIELD]: null, default_judge_model_id: 'j1' });
    });

    it('picking a first model is a change like any other, and is the stored one only once saved', async () => {
        await openWithSection({ tenant: { ...TENANT, [FIELD]: null } });
        expect(given).toMatchObject({ stored: '', value: '', changed: false });
        pick('f1');
        expect(given).toMatchObject({ stored: '', value: 'f1', changed: true });
        expect(saveButton()).toHaveTextContent('Review changes…');
        fireEvent.click(saveButton());
        fireEvent.click(saveButton());
        await waitFor(() => expect(patches()).toHaveLength(1));
        await waitFor(() => expect(given).toMatchObject({ stored: 'f1', value: 'f1', changed: false }));
    });

    it('a changed model is kept, and still needs two presses, on another sub-tab', async () => {
        await openWithSection();
        pick('f2');
        goTo('Profile');
        expect(saveButton()).toHaveTextContent('Review changes…');
        fireEvent.click(saveButton());
        expect(saveButton()).toHaveTextContent('Save changes');
        expect(given?.confirming).toBe(true);
        expect(patches()).toEqual([]);
        // Leaving the sub-tab withdraws the confirmation.
        goTo('Default Models');
        expect(fake().value).toBe('f2');
        expect(given).toMatchObject({ changed: true, confirming: false });
        expect(saveButton()).toHaveTextContent('Review changes…');
        expect(patches()).toEqual([]);
    });

    it('a save that fails withdraws the confirmation and keeps the change', async () => {
        await openWithSection({
            patch: () => {
                throw new Error('gateway said no');
            },
        });
        pick('f2');
        fireEvent.click(saveButton());
        await screen.findByText('fake: confirming');
        fireEvent.click(saveButton());
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Save failed', 'gateway said no'));
        await waitFor(() => expect(screen.queryByText('fake: confirming')).not.toBeInTheDocument());
        expect(given).toMatchObject({ stored: 'f1', value: 'f2', changed: true, confirming: false });
        expect(saveButton()).toHaveTextContent('Review changes…');
    });

    it('the judge model is saved with one press, the section\'s model as it stands', async () => {
        await openWithSection();
        fireEvent.change(judge(), { target: { value: 'j2' } });
        expect(saveButton()).toHaveTextContent('Save changes');
        fireEvent.click(saveButton());
        await waitFor(() => expect(patches()).toHaveLength(1));
        expect(patches()[0].body).toMatchObject({ [FIELD]: 'f1', default_judge_model_id: 'j2' });
    });

    it('the page does not open when the section cannot list its models', async () => {
        loadModels.mockRejectedValue(new Error('fake models are down'));
        answer({ tenant: { ...TENANT, [FIELD]: 'f1' } });
        render(
            <SlotProvider slots={{ 'tenant-settings.model-sections': [SECTION] }}>
                <TenantSettingsTab initialSubTab="embedding" />
            </SlotProvider>,
        );
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to load tenant settings', 'fake models are down'));
        expect(mounts).toBe(0);
    });

    it('off its sub-tab the section stays mounted and hidden, and is not loaded again on the way back', async () => {
        await openWithSection({}, 'profile');
        expect(mounts).toBe(1);
        expect(fake().closest('[hidden]')).not.toBeNull();
        expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent?.trim())).toEqual(['Profile']);
        goTo('Default Models');
        expect(fake().closest('[hidden]')).toBeNull();
        goTo('Profile');
        goTo('Default Models');
        // One mount throughout: what the section holds, or has in flight, is still there.
        expect(mounts).toBe(1);
        expect(loadModels).toHaveBeenCalledTimes(1);
    });
});
